from __future__ import division, print_function

import types
import inspect
from collections import Counter

import numpy as np

from cache import Cache
from utils import (LabeledArray, ExplainTypeError, safe_take, IrregularNDArray,
                   FullArgSpec, englishenum, make_hashable)
from context import EntityContext, EvaluationContext


try:
    import numexpr
#    numexpr.set_num_threads(1)
    evaluate = numexpr.evaluate
except ImportError:
    numexpr = None

    def make_global_context():
        context = dict((name, getattr(np, name))
                       for name in ('where', 'exp', 'log', 'abs'))
        context.update([('False', False), ('True', True)])
        return context
    eval_context = make_global_context()

    #noinspection PyUnusedLocal
    def evaluate(expr, globals_dict, locals_dict=None, **kwargs):
        complete_globals = {}
        complete_globals.update(globals_dict)
        if locals_dict is not None:
            if isinstance(locals_dict, np.ndarray):
                for fname in locals_dict.dtype.fields:
                    complete_globals[fname] = locals_dict[fname]
            else:
                complete_globals.update(locals_dict)
        complete_globals.update(eval_context)
        return eval(expr, complete_globals, {})

expr_cache = Cache()
num_tmp = 0
timings = Counter()


def get_tmp_varname():
    global num_tmp

    tmp_varname = "temp_%d" % num_tmp
    num_tmp += 1
    return tmp_varname

type_to_idx = {bool: 0, np.bool_: 0,
               int: 1, np.int32: 1, np.intc: 1, np.int64: 1, np.longlong: 1,
               float: 2, np.float64: 2}
idx_to_type = [bool, int, float]

missing_values = {
    # int: -2147483648,
    # for links, we need to have abs(missing_int) < len(a) !
    #XXX: we might want to use different missing values for links and for
    #     "normal" ints
    int: -1,
    float: float('nan'),
    # bool: -1
    bool: False
}


def normalize_type(type_):
    return idx_to_type[type_to_idx[type_]]


def get_missing_value(column):
    return missing_values[normalize_type(column.dtype.type)]


def get_missing_vector(num, dtype):
    res = np.empty(num, dtype=dtype)
    res.fill(missing_values[normalize_type(dtype.type)])
    return res


def get_missing_record(array):
    row = np.empty(1, dtype=array.dtype)
    for fname in array.dtype.names:
        row[fname] = get_missing_value(row[fname])
    return row


def hasvalue(column):
    missing_value = get_missing_value(column)
    if np.isnan(missing_value):
        return ~np.isnan(column)
    else:
        return column != missing_value


def coerce_types(context, *args):
    dtype_indices = [type_to_idx[getdtype(arg, context)] for arg in args]
    return idx_to_type[max(dtype_indices)]


def as_simple_expr(expr, context):
    if isinstance(expr, Expr):
        return expr.as_simple_expr(context)
    elif isinstance(expr, list):
        return [as_simple_expr(e, context) for e in expr]
    elif isinstance(expr, tuple):
        return tuple([as_simple_expr(e, context) for e in expr])
    else:
        return expr


def as_string(expr):
    if isinstance(expr, Expr):
        return expr.as_string()
    elif isinstance(expr, list):
        return [as_string(e) for e in expr]
    elif isinstance(expr, tuple):
        return tuple([as_string(e) for e in expr])
    else:
        return str(expr)


def traverse_expr(expr, context):
    if isinstance(expr, Expr):
        for node in expr.traverse(context):
            yield node
    elif isinstance(expr, (tuple, list)):
        for e in expr:
            for node in traverse_expr(e, context):
                yield node
    else:
        yield expr


def gettype(value):
    if isinstance(value, np.ndarray):
        type_ = value.dtype.type
    elif isinstance(value, (tuple, list)):
        type_ = type(value[0])
    else:
        type_ = type(value)
    return normalize_type(type_)


def getdtype(expr, context):
    if isinstance(expr, Expr):
        return expr.dtype(context)
    else:
        return gettype(expr)


def always(type_):
    def dtype(self, context):
        return type_
    return dtype


def firstarg_dtype(self, context):
    return getdtype(self.args[0], context)


def coerce_child_dtypes(expr, context):
    expr1, expr2 = expr.children
    return coerce_types(context, expr1, expr2)


def ispresent(values):
    dt = values.dtype
    if np.issubdtype(dt, float):
        return np.isfinite(values)
    elif np.issubdtype(dt, int):
        return values != missing_values[int]
    elif np.issubdtype(dt, bool):
        # return values != missing_values[bool]
        return True
    else:
        raise Exception('%s is not a supported type for ispresent' % dt)


# context is needed because in LinkGet we need to know what is the current
# entity (so that we can resolve links)
#TODO: we shouldn't resolve links during the simulation but
# rather in a "compilation" phase
def collect_variables(expr, context):
    if isinstance(expr, Expr):
        return expr.collect_variables(context)
    elif isinstance(expr, (tuple, list)):
        all_vars = [collect_variables(e, context) for e in expr]
        return set.union(*all_vars) if all_vars else set()
    else:
        return set()


def expr_eval(expr, context):
    if isinstance(expr, Expr):
        # assert isinstance(expr.__fields__, tuple)

        globals_data = context.global_tables
        if globals_data is not None:
            globals_names = set(globals_data.keys())
            if 'periodic' in globals_data:
                globals_names |= set(globals_data['periodic'].dtype.names)
        else:
            globals_names = set()

        for var_name in expr.collect_variables(context):
            if var_name not in globals_names and var_name not in context:
                raise Exception("variable '%s' is unknown (it is either not "
                                "defined or not computed yet)" % var_name)
        return expr.evaluate(context)

        # there are several flaws with this approach:
        # 1) I don't get action times (csv et al)
        # 2) these are cumulative times (they include child expr/processes)
        #    we might want to store the timings in a tree (based on call stack
        #    depth???) so that I could rebuild both cumulative and "real"
        #    timings.
        # 3) the sum of timings is wrong since children/nested expr times count
        #    both for themselves and for all their parents
#        time, res = gettime(expr.evaluate, context)
#        timings[expr.__class__.__name__] += time
#        return res
    elif isinstance(expr, list):
        return [expr_eval(e, context) for e in expr]
    elif isinstance(expr, tuple):
        return tuple([expr_eval(e, context) for e in expr])
    elif isinstance(expr, slice):
        return slice(expr_eval(expr.start, context),
                     expr_eval(expr.stop, context),
                     expr_eval(expr.step, context))
    else:
        return expr


def binop(opname, kind='binary', reversed=False):
    def op(self, other):
        classes = {'binary': BinaryOp,
                   'division': DivisionOp,
                   'logical': LogicalOp,
                   'comparison': ComparisonOp}
        class_ = classes[kind]
        return class_(opname, other, self) if reversed \
                                           else class_(opname, self, other)
    return op


class Expr(object):
    # we cannot do this in __new__ (args are verified in metaclass.__call__)
    # __metaclass__ = ExplainTypeError

    kind = 'generic'

    def __init__(self, value=None, children=None):
        object.__init__(self)
        self.value = value
        self.children = tuple(children) if children is not None else ()

    # makes sure we do not use "normal" python logical operators
    # (and, or, not)
    def __nonzero__(self):
        raise Exception("Improper use of boolean operators, you probably "
                        "forgot parenthesis around operands of an 'and' or "
                        "'or' expression. The complete expression cannot be "
                        "displayed but it contains: '%s'." % str(self))

    def evaluate(self, context):
        period = context.period

        if isinstance(period, np.ndarray):
            assert np.isscalar(period) or not period.shape
            period = int(period)

        cache_key = (self, period, context.entity_name, context.filter_expr)
        try:
            cached_result = expr_cache.get(cache_key, None)
            #FIXME: lifecycle functions should invalidate all variables!
            if cached_result is not None:
                return cached_result
        except TypeError:
            # The cache_key failed to hash properly, so the expr is not
            # cacheable. It *should* be because of a not_hashable expr
            # somewhere within cache_key[3].
            cache_key = None

        simple_expr = self.as_simple_expr(context)
        if isinstance(simple_expr, Variable) and simple_expr.name in context:
            return context[simple_expr.name]

        # check for labeled arrays, to work around the fact that numexpr
        # does not preserve ndarray subclasses.

        # avoid checking for arrays types in the past, because that is a
        # costly operation (context[var_name] fetches the column from disk
        # in that case). This probably prevents us from doing stuff like
        # align(lag(groupby() / groupby())), but it is a limitation I can
        # live with to avoid hitting the disk twice for each disk access.

        #TODO: I should rewrite this whole mess when my "dtype" method
        # supports ndarrays and LabeledArray so that I can get the dtype from
        # the expression instead of from actual values.
        labels = None
        assert isinstance(context, EvaluationContext)
        local_ctx = context.entity_data
        if isinstance(local_ctx, EntityContext) and local_ctx.is_array_period:
            for var_name in simple_expr.collect_variables(context):
                # var_name should always be in the context at this point
                # because missing temporaries should have been already caught
                # in expr_eval
                value = context[var_name]
                if isinstance(value, LabeledArray):
                    if labels is None:
                        labels = (value.dim_names, value.pvalues)
                    else:
                        if labels[0] != value.dim_names:
                            raise Exception('several arrays with inconsistent '
                                            'labels (dimension names) in the '
                                            'same expression: %s vs %s'
                                            % (labels[0], value.dim_names))
                        if not np.array_equal(labels[1], value.pvalues):
                            raise Exception('several arrays with inconsistent '
                                            'axis values in the same '
                                            'expression: \n%s\n\nvs\n\n%s'
                                            % (labels[1], value.pvalues))

        s = simple_expr.as_string()
        try:
            res = evaluate(s, local_ctx, {'nan': float('nan')}, truediv='auto')
            if labels is not None:
                # This is a hack which relies on the fact that currently
                # all the expression we evaluate through numexpr preserve
                # array shapes, but if we ever use numexpr reduction
                # capabilities, we will be in trouble
                res = LabeledArray(res, labels[0], labels[1])

            if cache_key is not None:
                expr_cache[cache_key] = res
                if cached_result is not None:
                    assert np.array_equal(res, cached_result), \
                        "%s != %s" % (res, cached_result)
            return res
        # except KeyError, e:
        #     raise add_context(e, s)
        except Exception:
            raise

    def as_simple_expr(self, context):
        """
        evaluate any construct that is not supported by numexpr and
        create temporary variables for them
        """
        raise NotImplementedError()

    def as_string(self):
        raise NotImplementedError()

    def __getitem__(self, key):
        #TODO: we should be able to know at "compile" time if this is a
        # scalar or a vector and disallow getitem in case of a scalar
        return SubscriptedExpr(self, key)

    def __getattr__(self, key):
        if key in {'shape', 'ndim',
                   'dim_names', 'pvalues', 'row_totals', 'col_totals',
                   '__len__',
                   'sum', 'prod', 'std', 'max', 'min'}:
            return ExprAttribute(self, key)
        else:
            raise AttributeError(key)

    def traverse(self, context=None):
        for child in self.children:
            for node in traverse_expr(child, context):
                yield node
        yield self

    def all_of(self, node_type, context=None):
        for node in self.traverse(context):
            if isinstance(node, node_type):
                yield node

    def collect_variables(self, context):
        allvars = list(self.all_of(Variable, context))
        #FIXME: this is a quick hack to make "othertable" work.
        # We should return prefixed variable instead.
        badvar = lambda v: isinstance(v, ShortLivedVariable) or \
                           (isinstance(v, GlobalVariable) and
                            v.tablename != 'periodic')
        return set(v.name for v in allvars if not badvar(v))
        # child_vars = [collect_variables(c, context) for c in self.children]
        # return set.union(*child_vars) if child_vars else set()

    #TODO: make equivalent/commutative expressions compare equal and hash to the
    # same thing.
    def __eq__(self, other):
        if not isinstance(other, Expr):
            return False

        if not isinstance(other, self.__class__):
            return False

        def strict_equal(a, b):
            return isinstance(b, a.__class__) and a == b

        def strict_equal_tuple(t1, t2):
            return all(strict_equal(e1, e2) for e1, e2 in zip(t1, t2))

        res = self.value == other.value and \
            strict_equal_tuple(self.children, other.children)
        if res:
            if str(self) != str(other):
                print()
                print(type(self), len(self.children), len(other.children))
                print([(x, type(x)) for x in self.children])
                print([(x, type(x)) for x in other.children])
                print('SHOULD NOT COMPARE EQUAL!')
                print(str(self).ljust(40), '>>>', self.value, self.children)
                print(str(other).ljust(40), '>>>', other.value, other.children)
                raise Exception("should not compare equal")
        return res

    def __hash__(self):
        return hash((self.__class__.__name__, self.value,
                     make_hashable(self.children)))

    def __contains__(self, expr):
        for node in self.traverse(None):
            if expr == node:
                return True
        return False


class EvaluableExpression(Expr):
    def evaluate(self, context):
        raise NotImplementedError()

    def as_simple_expr(self, context):
        tmp_varname = get_tmp_varname()
        result = self.evaluate(context)
        context[tmp_varname] = result
        return Variable(tmp_varname, gettype(result))


def non_scalar_array(a):
    return isinstance(a, np.ndarray) and a.shape


class SubscriptedExpr(EvaluableExpression):
    def __init__(self, expr, key):
        Expr.__init__(self, 'subscript', children=(expr, key))

    def __str__(self):
        expr, key = self.children
        if isinstance(key, slice):
            key_str = '%s:%s' % (key.start, key.stop)
            if key.step is not None:
                key_str += ':%s' % key.step
        else:
            key_str = str(key)
        return '%s[%s]' % (expr, key_str)
    __repr__ = __str__

    def evaluate(self, context):
        expr_value, key = [expr_eval(c, context) for c in self.children]
        filter_expr = context.filter_expr

        # When there is a contextual filter, we modify the key to avoid
        # crashes (IndexError).

        # The value returned for individuals outside the filter is
        # *undefined* ! We sometimes return missing and sometimes return the
        # value of another individual (index -1). This should not pose a
        # problem because those values should not be used anyway.
        if filter_expr is not None:
            # We need a context without filter to evaluate the filter
            # (to avoid an infinite recursion)
            sub_context = context.clone(filter_expr=None)
            # filter_value should be a bool scalar or a bool array
            filter_value = expr_eval(filter_expr, sub_context)
            assert isinstance(filter_value, (bool, np.bool_)) or \
                   np.issubdtype(filter_value.dtype, bool)

            def fixkey(orig_key, filter_value):
                if non_scalar_array(orig_key):
                    newkey = orig_key.copy()
                else:
                    newkey = np.empty(len(filter_value), dtype=int)
                    newkey.fill(orig_key)
                newkey[~filter_value] = -1
                return newkey

            if non_scalar_array(filter_value):
                if isinstance(key, tuple):
                    # nd-key
                    key = tuple(fixkey(k, filter_value) for k in key)
                elif isinstance(key, slice):
                    raise NotImplementedError()
                else:
                    # scalar or array key
                    key = fixkey(key, filter_value)
            else:
                if not filter_value:
                    missing_value = get_missing_value(expr_value)
                    if (non_scalar_array(key) or
                        (isinstance(key, tuple) and
                         any(non_scalar_array(k) for k in key))):
                        # scalar filter, array or tuple key
                        res = np.empty_like(expr_value)
                        res.fill(missing_value)
                        return res
                    elif isinstance(key, slice):
                        raise NotImplementedError()
                    else:
                        # scalar (or tuple of scalars) key
                        return missing_value
        return expr_value[key]


class ExprAttribute(EvaluableExpression):
    def __init__(self, expr, key):
        Expr.__init__(self, 'attr', children=(expr, key))

    def __str__(self):
        return '%s.%s' % self.children
    __repr__ = __str__

    def evaluate(self, context):
        expr, key = expr_eval(self.children, context)
        return getattr(expr, key)

    def __call__(self, *args, **kwargs):
        return DynamicFunctionCall(self, *args, **kwargs)


# we need to inherit from ExplainTypeError, so that TypeError exceptions are
# also "explained" for functions using FillFuncNameMeta
class FillFuncNameMeta(ExplainTypeError):
    def __init__(cls, name, bases, dct):
        ExplainTypeError.__init__(cls, name, bases, dct)

        funcname = dct.get('funcname')
        if funcname is None:
            funcname = cls.__name__.lower()
            cls.funcname = funcname


#XXX: it might be a good idea to merge both
class FillArgSpecMeta(FillFuncNameMeta):
    def __init__(cls, name, bases, dct):
        FillFuncNameMeta.__init__(cls, name, bases, dct)

        compute = cls.get_compute_func()

        # make sure we are not on one of the Abstract base class
        if compute is None:
            return

        # funcname = dct.get('funcname')
        # if funcname is None:
        #     funcname = cls.__name__.lower()
        #     cls.funcname = funcname

        argspec = dct.get('argspec')
        if argspec is None:
            try:
                # >>> def a(a, b, c=1, *d, **e):
                # ...     pass
                #
                # >>> inspect.getargspec(a)
                # ArgSpec(args=['a', 'b', 'c'], varargs='d', keywords='e',
                #         defaults=(1,))
                spec = inspect.getargspec(compute)
            except TypeError:
                raise Exception('%s is not a pure-Python function so its '
                                'signature needs to be specified '
                                'explicitly. See exprmisc.Uniform for an '
                                'example' % compute.__name__)
            if isinstance(compute, types.MethodType):
                # for methods, strip "self" and "context" args
                args = [arg for arg in spec.args
                        if arg not in {'self', 'context'}]
                spec = (args,) + spec[1:]
            kwonly = cls.kwonlyargs
            # if we have a varkw variable but it was only needed because of
            # kwonly args
            if spec[2] is not None and kwonly and not cls.kwonlyandvarkw:
                # we set varkw to None
                spec = spec[:2] + (None,) + spec[3:]
            extra = (kwonly.keys(), kwonly, {})
            cls.argspec = FullArgSpec._make(spec + extra)

    def get_compute_func(cls):
        raise NotImplementedError()


class AbstractFunction(Expr):
    __metaclass__ = FillFuncNameMeta

    funcname = None
    argspec = None

    def __init__(self, *args, **kwargs):
        # The behavior/error messages match Python 3.4 (and probably other 3.x)
        argnames = self.argspec.args
        maxargs = len(argnames)
        defaults = self.argspec.defaults
        nreqargs = maxargs - (len(defaults) if defaults is not None else 0)
        reqargnames = argnames[:nreqargs]
        allowed_kwargs = set(argnames) | set(self.argspec.kwonlyargs)
        funcname = self.funcname
        assert funcname is not None

        nargs = len(args)
        availposargnames = set(argnames[:nargs])
        availkwargnames = set(kwargs.keys())
        dupeargnames = availposargnames & availkwargnames
        if dupeargnames:
            raise TypeError("%s() got multiple values for argument '%s'"
                            % (funcname, dupeargnames.pop()))

        # Check that we do not have invalid kwargs
        extra_kwargs = availkwargnames - allowed_kwargs
        # def f(**kwargs) => argspec.varkw = 'kwargs'
        if extra_kwargs and self.argspec.varkw is None:
            raise TypeError("%s() got an unexpected keyword argument '%s'"
                            % (funcname, extra_kwargs.pop()))

        # Check that we do not have too many args
        if self.argspec.varargs is None and nargs > maxargs:
            # f() takes 3 positional arguments but 4 were given
            # f() takes from 1 to 3 positional arguments but 4 were given
            # + 1 to be consistent with Python (to account for self) but
            # those will be modified again (-1) in ExplainTypeError
            posargs = str(nreqargs + 1) if nreqargs == maxargs \
                else "from %d to %d" % (nreqargs + 1, maxargs + 1)

            msg = "%s() takes %s positional argument%s but %d were given"
            raise TypeError(msg % (funcname, posargs,
                                   's' if maxargs > 1 else '', nargs + 1))

        # Check that we have all required args (passed either as args or kwargs)
        missing = [name for name in reqargnames
                   if name not in (availposargnames | availkwargnames)]
        if missing:
            nmissing = len(missing)
            # f() missing 1 required positional argument: 'a'
            # f() missing 2 required positional arguments: 'a' and 'b'
            # f() missing 3 required positional arguments: 'a', 'b', and 'c'
            # + 1 to be consistent with Python (to account for self) but
            # those will be modified again (-1) in ExplainTypeError
            raise TypeError("%s() missing %d positional argument%s: %s"
                            % (funcname,
                               nmissing + 1,
                               's' if nmissing > 1 else '',
                               englishenum(repr(a) for a in missing)))

        # save original arguments before we mess with them
        self.original_args = args, sorted(kwargs.iteritems())

        # move all "non-kwonly" kwargs to args
        # def func(a, b, c, d, e=1, f=1):
        #     pass
        # nreqargs = 4, maxargs = 6
        # >>> func(1, 2, c=3, d=4, f=5)
        # nargs = 2
        # >>> func(1, 2, 3, 4, 5)
        # nargs = 5
        # 1) required arguments (without a default value) passed as kwargs
        #    pop() should not raise otherwise the "if missing" test above would
        #    have triggered an exception)
        extra_args = [kwargs.pop(name) for name in argnames[nargs:nreqargs]]

        # 2) optional args (with a default value) not passed as positional args
        if defaults is not None:
            # number of optional args passed as positional args
            nposopt = max(nargs - nreqargs, 0)
            extra_args.extend([kwargs.pop(argname, default)
                               for argname, default
                               in zip(argnames[nreqargs + nposopt:],
                                      defaults[nposopt:])])

        args = args + tuple(extra_args)
        kwargs = tuple(sorted(kwargs.items()))
        Expr.__init__(self, 'call', children=(args, kwargs))

    @property
    def args(self):
        return self.children[0]

    @args.setter
    def args(self, value):
        self.children = (value, self.kwargs)

    @property
    def kwargs(self):
        return self.children[1]

    @staticmethod
    def format_args_str(args, kwargs):
        """
        :param args: list of strings
        :param kwargs: list of (k, v) where both k and v are strings
        :return: a single string
        """
        return ', '.join(list(args) + ['%s=%s' % (k, v) for k, v in kwargs])

    @staticmethod
    def args_str(args, kwargs):
        args = [repr(a) for a in args]
        kwargs = [(str(k), repr(v)) for k, v in kwargs]
        return AbstractFunction.format_args_str(args, kwargs)

    def __repr__(self):
        return '%s(%s)' % (self.funcname, self.args_str(*self.original_args))


# this needs to stay in the expr module because of ExprAttribute, which uses
# DynamicFunctionCall -> GenericFunctionCall -> FunctionExpr
class FunctionExpr(EvaluableExpression, AbstractFunction):
    """
    Base class for defining (python-level) functions. That is, if you want to
    make a new function available in LIAM2 models, you should inherit from this
    class. In most cases, overriding the compute and dtype methods is
    enough, but your mileage may vary.
    """
    __metaclass__ = FillArgSpecMeta

    # argspec is set automatically for pure-python functions, but needs to
    # be set manually for builtin/C functions.
    argspec = None
    kwonlyargs = {}
    kwonlyandvarkw = False
    no_eval = ()

    @classmethod
    def get_compute_func(cls):
        return cls.compute

    def __init__(self, *args, **kwargs):
        AbstractFunction.__init__(self, *args, **kwargs)
        self.post_init()

    def post_init(self):
        pass

    def _eval_args(self, context):
        """
        evaluates arguments to the function except those in no_eval
        returns args, {kwargs}

        At this point "normal" args passed as kwargs have already been
        transferred to positional args by AbstractFunction.__init__, so kwargs
        are either kwonlyargs or varkwargs
        """
        if self.no_eval:
            no_eval = self.no_eval
            assert isinstance(no_eval, tuple) and \
                all(isinstance(f, basestring) for f in no_eval), \
                "no_eval should be a tuple of strings but %r is a %s" \
                % (no_eval, type(no_eval))
            no_eval = set(no_eval)

            argspec = self.argspec
            args, kwargs = self.children
            varargs = args[len(argspec.args):]

            # evaluate positional args
            args = [expr_eval(arg, context) if name not in no_eval else arg
                    for name, arg in zip(argspec.args, args)]

            # evaluate *args
            if varargs:
                assert argspec.varargs is not None
                if argspec.varargs not in no_eval:
                    varargs = [expr_eval(arg, context) for arg in varargs]
                args.extend(varargs)

            # check whether extra kwargs (from **kwargs) should be evaluated
            if argspec.varkw is not None and argspec.varkw in no_eval:
                allkwnames = set(name for name, _ in kwargs)
                # "normal" args passed as kwargs have been transferred to
                # positional args, so all remaining kwargs are either kwonlyargs
                # or varkwargs
                varkwnames = allkwnames - set(argspec.kwonlyargs)
                no_eval |= varkwnames

            # evaluate all kwargs
            kwargs = [(name, expr_eval(arg, context))
                      if name not in no_eval else (name, arg)
                      for name, arg in kwargs]
        else:
            args, kwargs = expr_eval(self.children, context)

        return args, dict(kwargs)

    def compute(self, context, *args, **kwargs):
        raise NotImplementedError()

    def evaluate(self, context):
        args, kwargs = self._eval_args(context)
        return self.compute(context, *args, **kwargs)


class GenericFunctionCall(FunctionExpr):
    """
    GenericFunctionCall handles calling expressions where the function to run is
    passed as the first argument.
    """
    @property
    def funcname(self):
        return str(self.children[0][0])

    @property
    def args(self):
        return self.children[0][1:]

    @property
    def kwargs(self):
        return self.children[1]

    def compute(self, context, func, *args, **kwargs):
        return func(*args, **kwargs)


class DynamicFunctionCall(GenericFunctionCall):
    """
    DynamicFunctionCall handles calling expressions where the function to run is
    determined at runtime (it should be passed as the first argument).
    """
    # DynamicFunctionCall is (currently) only used for calling ndarray methods,
    # which are all builtin methods for which we do not have signatures,
    # so we cannot (at this point) check arguments nor convert kwargs to args,
    # so we deliberately do not call FunctionExpr.__init__ which does both
    def __init__(self, *args, **kwargs):
        Expr.__init__(self, 'call', children=(args, sorted(kwargs.items())))

    @property
    def original_args(self):
        return self.args, self.kwargs

    def __str__(self):
        #FIXME
        r = super(DynamicFunctionCall, self).__str__()
        return '**DFC** // %s' % r


#############
# Operators #
#############

class UnaryOp(Expr):
    kind = 'op'

    def __init__(self, op, expr):
        Expr.__init__(self, op, children=(expr,))

    def as_simple_expr(self, context):
        child = self.children[0].as_simple_expr(context)
        return self.__class__(self.value, child)

    def as_string(self):
        return "(%s%s)" % (self.value, self.children[0].as_string())

    def dtype(self, context):
        return getdtype(self.children[0], context)

    #FIXME: only add parentheses if necessary
    def __str__(self):
        nicerop = {'~': 'not '}
        niceop = nicerop.get(self.value, self.value)
        return "(%s%s)" % (niceop, self.children[0])
    __repr__ = __str__


class BinaryOp(Expr):
    kind = 'op'

    def __init__(self, op, expr1, expr2):
        Expr.__init__(self, op, children=(expr1, expr2))

    def as_simple_expr(self, context):
        expr1, expr2 = self.children
        expr1 = as_simple_expr(expr1, context)
        expr2 = as_simple_expr(expr2, context)
        return self.__class__(self.value, expr1, expr2)

    # We can't simply use __str__ because of where vs if
    def as_string(self):
        expr1, expr2 = [as_string(c) for c in self.children]
        return "(%s %s %s)" % (expr1, self.value, expr2)

    dtype = coerce_child_dtypes

    #FIXME: only add parentheses if necessary
    def __str__(self):
        expr1, expr2 = self.children
        nicerop = {'&': 'and', '|': 'or'}
        niceop = nicerop.get(self.value, self.value)
        return "(%s %s %s)" % (expr1, niceop, expr2)
    __repr__ = __str__


class DivisionOp(BinaryOp):
    dtype = always(float)


class LogicalOp(BinaryOp):
    def assertbool(self, expr, context):
        dt = getdtype(expr, context)
        if dt is not bool:
            raise Exception("operands to logical operators need to be "
                            "boolean but %s is %s" % (expr, dt))

    #TODO: move the tests to a typecheck phase and use dtype = always(bool)
    def dtype(self, context):
        expr1, expr2 = self.children
        self.assertbool(expr1, context)
        self.assertbool(expr2, context)
        return bool


class ComparisonOp(BinaryOp):
    #TODO: move the test to a typecheck phase and use dtype = always(bool)
    def dtype(self, context):
        expr1, expr2 = self.children
        if coerce_types(context, expr1, expr2) is None:
            raise TypeError("operands to comparison operators need to be of "
                            "compatible types")
        return bool


#############
# Variables #
#############

class Variable(Expr):
    kind = 'variable'

    def __init__(self, name, dtype=None):
        Expr.__init__(self, name)

        # this would be more efficient but we risk being inconsistent
        # self.name = self.value
        self._dtype = dtype
        self.version = 0
        self.used = 0

    @property
    def name(self):
        return self.value

    def __str__(self):
        return self.name
    __repr__ = __str__
    as_string = __str__

    def as_simple_expr(self, context):
        return self

    def dtype(self, context):
        if self._dtype is None and self.name in context:
            type_ = context[self.name].dtype.type
            return normalize_type(type_)
        else:
            return self._dtype


class ShortLivedVariable(Variable):
    pass


#TODO: document in the "migration guide" and "gotcha" that subclasses MUST
# NOT call their parent __init__ but rather Expr.__init__
# class GlobalVariable(Variable):
class GlobalVariable(Expr):
    def __init__(self, tablename, name, dtype=None):
        Expr.__init__(self, (tablename, name))
        self._dtype = dtype

    @property
    def tablename(self):
        return self.value[0]

    @property
    def name(self):
        return self.value[1]

    def __str__(self):
        if self.tablename == "globals":
            return self.name
        else:
            return "%s.%s" % (self.tablename, self.name)
    __repr__ = __str__

    #XXX: inherit from EvaluableExpression?
    def as_simple_expr(self, context):
        result = self.evaluate(context)
        period = self._eval_key(context)
        if isinstance(period, int):
            tmp_varname = '__%s_%s' % (self.name, period)
            if tmp_varname in context:
                # should be consistent but nan != nan
                assert result != result or context[tmp_varname] == result
            else:
                context[tmp_varname] = result
        else:
            tmp_varname = get_tmp_varname()
            context[tmp_varname] = result
        return Variable(tmp_varname)

    def _eval_key(self, context):
        return context.period

    def evaluate(self, context):
        key = self._eval_key(context)
        globals_data = context.global_tables
        globals_table = globals_data[self.tablename]

        #TODO: this row computation should be encapsulated in the
        # globals_table object and the index column should be configurable
        colnames = globals_table.dtype.names
        if 'period' in colnames or 'PERIOD' in colnames:
            try:
                globals_periods = globals_table['PERIOD']
            except ValueError:
                globals_periods = globals_table['period']
            base_period = globals_periods[0]
            if isinstance(key, slice):
                translated_key = slice(key.start - base_period,
                                       key.stop - base_period,
                                       key.step)
            else:
                translated_key = key - base_period
        else:
            translated_key = key
        if self.name not in globals_table.dtype.fields:
            print(self.name)
            raise Exception("Unknown global: %s" % self.name)
        column = globals_table[self.name]
        numrows = len(column)
        missing_value = get_missing_value(column)

        if isinstance(translated_key, np.ndarray) and translated_key.shape:
            return safe_take(column, translated_key, missing_value)
        elif isinstance(translated_key, slice):
            start, stop = translated_key.start, translated_key.stop
            step = translated_key.step
            if step is not None and step != 1:
                raise NotImplementedError("step != 1 (%d)" % step)
            if (isinstance(start, np.ndarray) and start.shape or isinstance(
                    stop, np.ndarray) and stop.shape):
                lengths = stop - start
                length0 = lengths[0]
                if not isinstance(start, np.ndarray) or not start.shape:
                    start = np.repeat(start, len(lengths))
                if np.all(lengths == length0):
                    # constant length => result is a 2D array:
                    # num_individuals x slice_length
                    result = np.empty((len(lengths), length0),
                                      dtype=column.dtype)
                    # we assume there are more individuals than there are
                    # "periods" (or other ticks) in the table.
                    #XXX: We might want to actually test that it is true and
                    # loop on the individuals instead if that is not the case
                    for i in range(length0):
                        result[:, i] = safe_take(column, start + i,
                                                 missing_value)
                    return result
                else:
                    # varying length => result is an array (num_individuals) of
                    # 1D arrays (slice lengths)
                    # each "item" of the result is a view, so we pay "only" for
                    # all the arrays overhead, not for the data itself.
                    result = np.empty(len(lengths), dtype=list)
                    if not isinstance(stop, np.ndarray) or not stop.shape:
                        stop = np.repeat(stop, len(lengths))
                    for i in range(len(lengths)):
                        result[i] = column[start[i]:stop[i]]
                    return IrregularNDArray(result)
            else:
                # out of bounds slices bounds are "dropped" silently (like in
                # python) -- ie the length of the slice returned can be
                # smaller than the one asked. We could return "missing_value"
                # for indices out of bounds but I do not know if it would be
                # better. Since this version is easier to implement, lets go for
                # it for now.
                return column[translated_key]
        else:
            out_of_bounds = (translated_key < 0) or (translated_key >= numrows)
            return column[translated_key] if not out_of_bounds \
                else missing_value

    def __getitem__(self, key):
        return SubscriptedGlobal(self.tablename, self.name, key, self._dtype)


class SubscriptedGlobal(GlobalVariable):
    def __init__(self, tablename, name, key, dtype):
        Expr.__init__(self, (tablename, name, key))
        self._dtype = dtype
        # GlobalVariable.__init__(self, tablename, name, dtype)
        # self.key = key

    @property
    def key(self):
        return self.value[2]

    def __str__(self):
        return '%s[%s]' % (self.name, self.key)
    __repr__ = __str__

    def _eval_key(self, context):
        return expr_eval(self.key, context)


#TODO: this class shouldn't be needed. GlobalArray should be handled in the
# context
class GlobalArray(Variable):
    def __init__(self, name, dtype=None):
        Variable.__init__(self, name, dtype)

    def as_simple_expr(self, context):
        globals_data = context.global_tables
        result = globals_data[self.name]
        #XXX: maybe I should just use self.name?
        tmp_varname = '__%s' % self.name
        if tmp_varname in context:
            assert context[tmp_varname] is result
        context[tmp_varname] = result
        return Variable(tmp_varname)


class GlobalTable(object):
    def __init__(self, name, fields):
        """fields is a list of tuples (name, type)"""

        self.name = name
        self.fields = fields
        self.fields_map = dict(fields)

    def __getattr__(self, key):
        return GlobalVariable(self.name, key, self.fields_map[key])

    #noinspection PyUnusedLocal
    def traverse(self, context):
        yield self

    def __str__(self):
        #XXX: print (a subset of) data instead?
        return 'Table(%s)' % ', '.join([name for name, _ in self.fields])
    __repr__ = __str__


#XXX: can we factorise this with FunctionExpr et al.?
class MethodCall(EvaluableExpression):
    def __init__(self, entity, name, args, kwargs):
        Expr.__init__(self, (entity, name), children=(args, kwargs))

    @property
    def entity(self):
        return self.value[0]

    @property
    def name(self):
        return self.value[1]

    @property
    def args(self):
        return self.children[0]

    @property
    def kwargs(self):
        return self.children[1]

    def evaluate(self, context):
        from process import Assignment, Function
        entity_processes = self.entity.processes
        method = entity_processes[self.name]
        # hybrid (method & variable) assignment can be called
        assert isinstance(method, (Assignment, Function))
        args = [expr_eval(arg, context) for arg in self.args]
        kwargs = dict((k, expr_eval(v, context))
                      for k, v in self.kwargs.iteritems())
        return method.run_guarded(context, *args, **kwargs)

    #TODO: use AbstractFunction?
    def __str__(self):
        args = [repr(a) for a in self.args]
        kwargs = ['%s=%r' % (k, v) for k, v in self.kwargs.iteritems()]
        return '%s(%s)' % (self.name, ', '.join(args + kwargs))
    __repr__ = __str__


class VariableMethodHybrid(Variable):
    def __init__(self, name, entity, dtype=None):
        Expr.__init__(self, (name, entity))
        self._dtype = dtype

    @property
    def name(self):
        return self.value[0]

    @property
    def entity(self):
        return self.value[1]

    def __call__(self, *args, **kwargs):
        return MethodCall(self.entity, self.name, args, kwargs)


# class MethodCallToResolve(Expr):
#     def __init__(self, name, entity, args, kwargs):
#         self.name = name
#         self.entity = entity
#         self.args = args
#         self.kwargs = kwargs
#
#     def resolve(self):
#         entity_processes = self.entity.processes
#         method = entity_processes[self.name]
#         # hybrid (method & variable) assignment can be called
#         assert isinstance(method, (Assignment, Function))
#         return GenericFunctionCall(method, *self.args, **self.kwargs)


class MethodSymbol(object):
    def __init__(self, name, entity):
        self.name = name
        self.entity = entity

    def __call__(self, *args, **kwargs):
        # we cannot use self.entity.processes as they are not defined yet (we
        # are probably currently building them), so we cannot return a
        # GenericFunctionCall now like we should and instead must either
        # return an intermediary object (MethodCallToResolve) which we will
        # "resolve" later, or use DynamicFunctionCall (for which we cannot
        # have dtype yet). However that "resolve" step is currently hard to
        # do because we need ast.NodeTransformer-like machinery which we do not
        # have yet.
        # return MethodCallToResolve(self.entity, self.name, args, kwargs)
        return MethodCall(self.entity, self.name, args, kwargs)


class NotHashable(Expr):
    __hash__ = None
not_hashable = NotHashable()
