"""Simple symbolic execution engine for Python."""

import sys
import os
import z3
import types
import collections

# Monkey-patch __nonzero__ on Z3 types to make sure we don't
# accidentally call it instead of our wrappers.
def z3_nonzero(self):
    raise RuntimeError("Cannot __nonzero__ a %s" % self.__class__)
z3.ExprRef.__nonzero__ = z3_nonzero
del z3_nonzero

# Patch in a __hash__ for SortRef, to make it possible to put
# sorts as dictionary keys.
def z3_sort_hash(self):
    return hash(str(self))
z3.SortRef.__hash__ = z3_sort_hash
del z3_sort_hash

anon_idx = 0
def anon_name(base = "anon"):
    global anon_idx
    anon_idx += 1
    return "%s%d" % (base, anon_idx)
            
class Symbolic(object):
    """Base class of symbolic types.  Symbolic types come in two
    groups: constant and mutable.  Symbolic constants are deeply
    immutable.  Generally they are primitive types, such as integers
    and booleans, but more complex types can also be constants (e.g.,
    an immutable symbolic tuple of constants).  Mutable symbolic
    values are used for compound and container types like maps and
    structs.  Mutable values act like lvalues except when assigned,
    where they are copied at the point of assignment.

    A subclass of Symbolic must have a __z3_sort__ class field giving
    the compound z3.SortRef for the value's type.  Subclasses must
    also implement the _z3_value and _wrap_lvalue methods.
    """

    def __init__(self):
        raise RuntimeError("%s cannot be constructed directly" % strtype(self))

    @classmethod
    def _z3_sort(cls):
        """Return the compound Z3 sort represented by this class."""
        return cls.__z3_sort__

    def _z3_value(self):
        """Return the compound Z3 value wrapped by this object.

        For mutable objects, this should be its current value.
        """
        raise NotImplementedError("_z3_value is abstract")

    @classmethod
    def _new_lvalue(cls, init, model):
        """Return a new instance of Symbolic with the given initial value,
        which must be a compound Z3 value.  The returned instance's
        state will not be shared with any existing instances.  model,
        if not None, provides the simsym.Model in which to evaluate
        concrete values.
        """
        val = [init]
        def setter(nval):
            val[0] = nval
        obj = cls._wrap_lvalue(lambda: val[0], setter, model)
        if model is None:
            obj._declare_assumptions(assume)
        return obj

    @classmethod
    def _wrap_lvalue(cls, getter, setter, model):
        """Return a new instance of this class.

        Return an instance of this class wrapping the compound Z3
        value returned by getter.  If this object is immutable, then
        it should call getter immediately to fetch the object's
        current value.  If this object is mutable, it should use
        getter on demand and it should reflect changes to its value by
        calling setter with its updated compound Z3 value.

        model, if not None, provides the simsym.Model in which to
        evaluate concrete values.  Immutable, non-compound objects
        should use this to evaluate their concrete Python value and
        return this concrete value.  Mutable, compound objects should
        pass this model down to their components.
        """
        raise NotImplementedError("_wrap_lvalue is abstract")

    @classmethod
    def var(cls, name=None, model=None):
        """Return a symbolic variable of this type.

        Initially, the variable's value will be unconstrained.  It may
        become constrained or even concrete as the program progresses.

        If this method is called multiple times with the same name,
        the returned instances will represent the same underlying
        symbolic value (though the instances themselves will be
        distinct).

        If model is provided, it must be a simsym.Model.  This
        symbolic variable will be interpreted in this model to get a
        concrete Python value.
        """

        if name is None:
            name = anon_name()
        elif model is None:
            var_constructors[name] = cls.var
        def mkValue(path, sort):
            if isinstance(sort, dict):
                return {k: mkValue(path + (k,), v)
                        for k, v in sort.iteritems()}
            strname = ".".join((name,) + path)
            # Record the simsym type of this constant
            constTypes[strname] = (cls, path)
            # Create the Z3 constant
            return z3.Const(strname, sort)
        return cls._new_lvalue(mkValue((), cls._z3_sort()), model)

    def _declare_assumptions(self, assume):
        """Declare assumptions for a new lvalue self.

        This is called for newly created lvalues and should be
        implemented by subclasses requiring assumption declarations.
        Implementations should call the argument 'assume' with boolean
        expressions involving self that must be true.  Usually
        'assume' is simply simsym.assume, but in situations involving
        extensional arrays (i.e., maps) it may bind quantifiers.

        Subclasses that override this method should always invoke the
        parent class' _declare_assumptions method.

        For compound types, since only the compound itself is a newly
        created lvalue, the implementation of _declare_assumptions
        should project the components of the compound and call each
        component's _declare_assumptions method.
        """
        pass

    def __ne__(self, o):
        r = self == o
        if r is NotImplemented:
            return NotImplemented
        return symnot(r)

class SymbolicConst(Symbolic):
    """The base class for symbolic constants.  Symbolic constants are
    deeply immutable values such as primitive types."""

    @classmethod
    def var(cls, name=None, model=None):
        # Const returns the most specific z3.*Ref type it can based on
        # the sort.  This is equivalent to Symbolic.var, but jumps
        # through fewer hoops.
        if name is None:
            name = anon_name()
        elif model is None:
            var_constructors[name] = cls.var
        constTypes[name] = (cls, ())
        return cls._wrap(z3.Const(name, cls._z3_sort()), model)

    @classmethod
    def _wrap_lvalue(cls, getter, setter, model):
        # Fetch the value immediately, rather than acting like an
        # lvalue.
        return cls._wrap(getter(), model)

#
# Compounds
#

# A "compound X" is either an X or a dictionary from component names
# (e.g., struct fields) to compound X's.

def compound_map(func, *compounds):
    if len(compounds) == 1:
        return compound_map1(func, compounds[0])
    else:
        return compound_mapN(func, compounds)

def compound_map1(func, compound):
    if isinstance(compound, dict):
        return {k: compound_map1(func, v) for k, v in compound.iteritems()}
    return func(compound)

def compound_mapN(func, compounds):
    if isinstance(compounds[0], dict):
        return {k: compound_mapN(func, [c[k] for c in compounds])
                for k in compounds[0].iterkeys()}
    return func(*compounds)

def flatten_compound(compound):
    res = []
    def rec(compound):
        if isinstance(compound, dict):
            for sub in compound.itervalues():
                rec(sub)
        else:
            res.append(compound)
    rec(compound)
    return res

#
# Z3 wrappers
#

# Below, we construct Symbolic subclasses that parallel Z3 sorts.
# These are organized into a functional class hierarchy so that, for
# example, all sorts supporting arithmetic operations can share a
# superclass that wraps the arithmetic operations.  (These functional
# superclasses are not strictly abstract; because our wrapping of Z3
# sorts is not complete, we may wrap a Z3 expression using one of
# these superclasses if we don't know its specific sort.)  Most of the
# methods in these classes simply wrap underlying Z3 methods
# (unwrapping the arguments and re-wrapping the result).

class MetaZ3Wrapper(type):
    """Metaclass to generate wrappers for Z3 ref object methods.

    The class must have a __ref_type__ field giving the Z3 ref type
    wrapped by the class.  The class may optionally have a
    __pass_type__ field, giving a Python type or tuple or types that
    should be passed through wrap untouched.

    The class must also have a __wrap__ field giving a list of method
    names to wrap.  For each method in __wrap__, the generated class
    will have a corresponding method with the same signature that
    unwraps all arguments, calls the Z3 method, and re-wraps the
    result.
    """

    def __new__(cls, classname, bases, classdict):
        if "__wrap__" in classdict:
            ref_type = classdict["__ref_type__"]
            for method in classdict.pop("__wrap__"):
                base_method = getattr(ref_type, method)
                nargs = base_method.__func__.__code__.co_argcount
                args = ["o%d" % i for i in range(nargs - 1)]
                code = "def %s(%s):\n" % (method, ",".join(["self"] + args))
                for o in args:
                    code += " if isinstance(%s, Symbolic): %s=%s._v\n" % \
                        (o, o, o)
                code += " return wrap(self._v.%s(%s))" % (method, ",".join(args))
                locals_dict = {}
                exec code in globals(), locals_dict
                classdict[method] = locals_dict[method]

        return type.__new__(cls, classname, bases, classdict)

    def _wrap(cls, z3ref, model):
        """Construct an instance of 'cls' wrapping the given Z3 ref
        object."""

        if not isinstance(z3ref, cls.__ref_type__):
            if hasattr(cls, "__pass_type__") and \
               isinstance(z3ref, cls.__pass_type__):
                return z3ref
            raise TypeError("%s expected %s, got %s" %
                            (cls.__name__, cls.__ref_type__.__name__,
                             strtype(z3ref)))
        obj = cls.__new__(cls)
        obj._v = z3ref
        if model:
            # Interpret this symbolic value into a concrete value
            # using the model
            return model._eval(obj)
        return obj

class SExpr(Symbolic):
    __metaclass__ = MetaZ3Wrapper
    __ref_type__ = z3.ExprRef
    __wrap__ = ["__eq__", "__ne__"]

    def __str__(self):
        return str(self._v)

    def __repr__(self):
        return repr(self._v)

    def _z3_value(self):
        return self._v

class SArith(SExpr):
    __ref_type__ = z3.ArithRef
    __wrap__ = ["__add__", "__div__", "__mod__", "__mul__", "__pow__",
                "__sub__", "__truediv__",
                "__radd__", "__rdiv__", "__rmod__", "__rmul__", "__rpow__",
                "__rsub__", "__rtruediv__",
                "__ge__", "__gt__", "__le__", "__lt__",
                "__neg__", "__pos__"]

class SInt(SArith, SymbolicConst):
    __pass_type__ = int
    __z3_sort__ = z3.IntSort()

    # Note that we're wrapping ArithRefs, not IntNumRefs.  Z3's Ref
    # hierarchy reflects AST structure, while our types reflect Z3
    # sorts (which Z3 can't do because its C implementation can't
    # construct new types on the fly like we can in Python).

class UncheckableConstraintError(RuntimeError):
    def __init__(self, expr, reason):
        RuntimeError.__init__(
            self, 'Uncheckable constraint %s:\n%s' % (reason, z3.simplify(expr)))

class UnsatisfiablePath(RuntimeError):
    pass

class SBool(SExpr, SymbolicConst):
    __ref_type__ = z3.BoolRef
    __pass_type__ = bool
    __z3_sort__ = z3.BoolSort()

    def __nonzero__(self):
        solver = get_solver()

        global cursched, curschedidx
        if len(cursched) == curschedidx:
            # We've reached the end of replay; extend the schedule
            solver.push()
            solver.add(self._v)
            canTrue = solver.check()
            canTrueReason = solver.reason_unknown()
            if canTrue == z3.unknown:
                # Stack operations change how Z3 "compiles" formulas,
                # so it's possible it can solve it in isolation.
                s2 = z3.Solver()
                s2.add(*solver.assertions())
                canTrue = s2.check()
                canTrueReason = s2.reason_unknown()
            solver.pop()

            solver.push()
            solver.add(z3.Not(self._v))
            canFalse = solver.check()
            canFalseReason = solver.reason_unknown()
            if canFalse == z3.unknown:
                s2 = z3.Solver()
                s2.add(*solver.assertions())
                canFalse = s2.check()
                canFalseReason = s2.reason_unknown()
            solver.pop()

            if canTrue == z3.unsat and canFalse == z3.unsat:
                raise RuntimeError("Branch contradiction")

            # Extend the schedule
            if canTrue == z3.sat and canFalse == z3.unsat:
                cursched.append((True, cursched[-1][1]))
            elif canTrue == z3.unsat and canFalse == z3.sat:
                cursched.append((False, cursched[-1][1]))
            else:
                # Both are possible; take both paths
                gnode = cursched[-1][1]
                gnode.set_label(self, get_caller())
                tnode, fnode = curgraph.new_node(), curgraph.new_node()
                curgraph.new_edge(gnode, tnode, "T")
                curgraph.new_edge(gnode, fnode, "F")

                newsched = list(cursched)
                if canTrue == z3.sat:
                    cursched.append((True, tnode))
                else:
                    cursched.append((UncheckableConstraintError(
                                self._v, canTrueReason), tnode))
                if canFalse == z3.sat:
                    newsched.append((False, fnode))
                else:
                    newsched.append((UncheckableConstraintError(
                                z3.Not(self._v), canFalseReason), fnode))
                queue_schedule(newsched)

        # Follow the schedule (which we may have just extended)
        rv = cursched[curschedidx][0]
        if rv == True:
            solver.add(self._v)
        elif rv == False:
            solver.add(z3.Not(self._v))
        elif isinstance(rv, UncheckableConstraintError):
            raise rv
        else:
            raise RuntimeError("Bad schedule entry %r" % cursched[curschedidx])
        #assert solver.check() == z3.sat
        curschedidx = curschedidx + 1
        return rv

class SUninterpretedBase(SExpr):
    pass

def tuninterpreted(name):
    """Return a new uninterpreted symbolic type.

    This type is inhabited by an unbounded number of distinct
    constants.
    """
    return type(name, (SUninterpretedBase, SymbolicConst),
                {"__z3_sort__": z3.DeclareSort(name)})

class SEnumBase(SExpr):
    __ref_type__ = z3.DatatypeRef

def tenum(name, vals):
    """Return a symbolic constant enumeration type called 'name' with
    the given values.  'vals' must be a list of strings or a string of
    space-separated names.  The returned type will have a class field
    corresponding to each concrete value and inherit from SEnumBase
    and SymbolicConst."""

    if isinstance(vals, basestring):
        vals = vals.split()
    sort, consts = z3.EnumSort(name, vals)
    fields = dict(zip(vals, consts))
    fields["__z3_sort__"] = sort
    return type(name, (SEnumBase, SymbolicConst), fields)

class STupleBase(SExpr):
    __ref_type__ = z3.DatatypeRef

def ttuple(name, *types):
    """Return a symbolic constant named tuple type with the given
    fields.  Each 'type' argument must be a pair of name and type.
    The returned type will inherit from STupleBase and SymbolicConst
    and will have properties for retrieving each component of the
    tuple."""

    # XXX Broken: synonym types, assumptions
    raise Exception("Sorry, ttuple is broken right now")

    sort = z3.Datatype(name)
    sort.declare(name, *[(fname, typ._z3_sort()) for fname, typ in types])
    sort = sort.create()
    fields = {"__z3_sort__" : sort}
    for fname, typ in types:
        code = """\
@property
def %s(self):
    return wrap(self.__z3_sort__.%s(self._v))""" % (fname, fname)
        locals_dict = {}
        exec code in globals(), locals_dict
        fields[fname] = locals_dict[fname]
    return type(name, (STupleBase, SymbolicConst), fields)

class SConstMapBase(SExpr):
    __ref_type__ = z3.ArrayRef
    __wrap__ = ["__getitem__"]

    @classmethod
    def constVal(cls, value):
        """Return a map where all keys map to 'value'."""
        return cls._wrap(z3.K(cls._z3_sort().domain(), unwrap(value)), None)

    def store(self, index, value):
        """Return a new map that is identical for this map except that
        'index' will map to 'value'."""
        return self._wrap(z3.Store(unwrap(self), unwrap(index), unwrap(value)),
                          None)

def tconstmap(indexType, valueType):
    """Return an immutable map type (a z3 "array") that maps from
    symbolic constants of 'indexType' to symbolic constants of
    'valueType'.  The returned type will inherit from SConstMapBase
    and SymbolicConst."""

    sort = z3.ArraySort(indexType._z3_sort(), valueType._z3_sort())
    name = "SConstMap_%s_%s" % (indexType.__name__, valueType.__name__)
    return type(name, (SConstMapBase, SymbolicConst), {"__z3_sort__" : sort})

#
# Type synonyms
#

def tsynonym(name, baseType):
    """Return a new type that's equivalent to baseType."""
    return type(name, (baseType,), {})

#
# Compound objects
#

class SMapBase(Symbolic):
    """The base type of symbolic mutable mapping types.  Objects of
    this type map from symbolic values to symbolic values.  Maps
    support slicing and slice assignment."""

    @classmethod
    def constVal(cls, value):
        """Return a map where all keys initially map to 'value'."""
        indexSort = cls._indexType._z3_sort()
        return cls._new_lvalue(
            compound_map(lambda val: z3.K(indexSort, val), unwrap(value)),
            None)

    @classmethod
    def _wrap_lvalue(cls, getter, setter, model):
        # XXX Make this generic?
        obj = cls.__new__(cls)
        obj._getter = getter
        obj._setter = setter
        obj._model = model
        return obj

    def _z3_value(self):
        # XXX Make this generic, too?
        return self._getter()

    def _declare_assumptions(self, assume):
        super(SMapBase, self)._declare_assumptions(assume)
        x = self._indexType.var()
        self[x]._declare_assumptions(lambda expr: assume(forall(x, expr)))

    def __eq__(self, o):
        if not isinstance(o, type(self)):
            return NotImplemented
        av, bv = flatten_compound(self._getter()), flatten_compound(o._getter())
        return symand([wrap(af == bf) for af, bf in zip(av, bv)])

    def __getitem__(self, idx):
        """Return the value at index 'idx'."""
        z3idx = unwrap(idx)
        return self._valueType._wrap_lvalue(
            lambda: compound_map(
                lambda z3val: z3.Select(z3val, z3idx), self._getter()),
            lambda val: self.__setitem__(idx, val),
            self._model)

    def __setitem__(self, idx, val):
        """Change the value at index 'idx'."""
        z3idx = unwrap(idx)
        self._setter(compound_map(
            lambda z3map, z3val: z3.Store(z3map, z3idx, z3val),
            self._getter(), unwrap(val)))

def tmap(indexType, valueType):
    """Return a subclass of SMapBase that maps from 'indexType' to
    'valueType', where both must be subclasses of Symbolic."""
    # XXX We could accept a size and check indexes if indexType is an
    # ordered sort
    name = "SMap_%s_%s" % (indexType.__name__, valueType.__name__)

    indexSort = indexType._z3_sort()
    if isinstance(indexSort, dict):
        raise TypeError("Map index may not be a compound type")
    sort = compound_map(lambda z3sort: z3.ArraySort(indexSort, z3sort),
                        valueType._z3_sort())
    return type(name, (SMapBase,),
                {"_indexType" : indexType, "_valueType" : valueType,
                 "__z3_sort__" : sort})

class SStructBase(Symbolic):
    """The base type of symbolic mutable structure types.  Structure
    types have a fixed set of named fields, where the fields may have
    different symbolic types."""

    @classmethod
    def var(cls, __name=None, __model=None, **fields):
        """Return a struct instance with specified field values.

        Any fields omitted from 'fields' will be unspecified.  If any
        fields are omitted, the first positional argument must be
        supplied to name the symbolic constants for the omitted
        fields.  If no field values are provided, this is equivalent
        to Symbolic.var.
        """

        if __name is not None and __model is None:
            # Field values may be mutable Symbolic values, but we want
            # to save their current value, so snapshot them by
            # unwrapping their values.
            fieldsSnapshot = {k: unwrap(v) for k,v in fields.items()}
            var_constructors[__name] \
                = lambda _, model: cls.var(__name, model, **fieldsSnapshot)

        def mkValue(path, sort):
            if len(path) == 1 and path[0] in fields:
                return unwrap(fields.pop(path[0]))
            if isinstance(sort, dict):
                return {k: mkValue(path + (k,), v)
                        for k, v in sort.iteritems()}
            if __name is None:
                raise ValueError(
                    "Name required for partially symbolic struct")
            strname = ".".join((__name,) + path)
            # Record the simsym type of this constant
            constTypes[strname] = (cls, path)
            # Create the Z3 constant
            return z3.Const(strname, sort)
        if fields:
            raise AttributeError("Unknown struct field %r" % fields.keys()[0])
        return cls._new_lvalue(mkValue((), cls._z3_sort()), __model)

    @classmethod
    def _wrap_lvalue(cls, getter, setter, model):
        obj = cls.__new__(cls)
        # Don't go through the overridden __setattr__.
        object.__setattr__(obj, "_getter", getter)
        object.__setattr__(obj, "_setter", setter)
        object.__setattr__(obj, "_model", model)
        return obj

    def _z3_value(self):
        return self._getter()

    def _declare_assumptions(self, assume):
        super(SStructBase, self)._declare_assumptions(assume)
        for fname in self._fields:
            getattr(self, fname)._declare_assumptions(assume)

    def __eq__(self, o):
        # XXX Duplicated with SMapBase.  Maybe have SymbolicCompound?
        if not isinstance(o, type(self)):
            return NotImplemented
        av, bv = flatten_compound(self._getter()), flatten_compound(o._getter())
        return symand([wrap(af == bf) for af, bf in zip(av, bv)])

    def __getattr__(self, name):
        if name not in self._fields:
            raise AttributeError(name)
        return self._fields[name]._wrap_lvalue(
            lambda: self._getter()[name],
            lambda val: self.__setattr__(name, val),
            self._model)

    def __setattr__(self, name, val):
        if name not in self._fields:
            raise AttributeError(name)
        cval = self._getter()
        cval[name] = unwrap(val)
        self._setter(cval)

def tstruct(**fields):
    """Return a subclass of SStructBase for a struct type with the
    given fields.  'fields' must be a dictionary mapping from names to
    symbolic types."""

    name = "SStruct_" + "_".join(fields.keys())
    sort = {fname: typ._z3_sort() for fname, typ in fields.items()}
    type_fields = {"__slots__": [], "_fields": fields, "__z3_sort__": sort}
    return type(name, (SStructBase,), type_fields)

#
# Constructors
#

def symand(exprlist):
    if any([isinstance(x, Symbolic) for x in exprlist]):
        return wrap(z3.And([unwrap(x) for x in exprlist]))
    else:
        return all(exprlist)

def symor(exprlist):
    if any([isinstance(x, Symbolic) for x in exprlist]):
        return wrap(z3.Or([unwrap(x) for x in exprlist]))
    else:
        return any(exprlist)

def symnot(e):
    if isinstance(e, Symbolic):
        return wrap(z3.Not(unwrap(e)))
    else:
        return not e

def symeq(a, b):
    if isinstance(a, tuple) and isinstance(b, tuple):
        if len(a) != len(b):
            return False
        return symand([symeq(aa, bb) for (aa, bb) in zip(a, b)])
    return a == b

def implies(a, b):
    return wrap(z3.Implies(unwrap(a), unwrap(b)))

def exists(vars, e, patterns=[]):
    if not isinstance(vars, (list, tuple)):
        vars = [vars]
    if not isinstance(e, (Symbolic, z3.ExprRef)):
        return e
    return wrap(z3.Exists([unwrap(v) for v in vars], unwrap(e),
                          patterns=map(unwrap, patterns)))

def forall(vars, e, patterns=[]):
    if not isinstance(vars, (list, tuple)):
        vars = [vars]
    if not isinstance(e, (Symbolic, z3.ExprRef)):
        return e
    return wrap(z3.ForAll([unwrap(v) for v in vars], unwrap(e),
                          patterns=map(unwrap, patterns)))

#
# Conversions to Z3 types and wrapper types
#

def unwrap(val):
    """Convert a value to a Z3 value.

    If val is a Symbolic, returns the wrapped Z3 value.
    Otherwise, simply returns val."""

    if isinstance(val, Symbolic):
        return val._z3_value()
    return val

def wrap(ref):
    """Convert a value to a simsym.Symbolic.

    If ref is a Z3 symbolic value, wraps it in a simsym.Symbolic.
    Otherwise, if ref is a concrete type supported by the symbolic
    engine, returns value.  Otherwise, raises TypeError."""

    if isinstance(ref, (bool, int, long, float)):
        # Concrete type supported by Z3
        return ref

    if not isinstance(ref, z3.ExprRef):
        raise TypeError("Not a bool, int, long, float, or z3.ExprRef")

    # XXX It's probably not correct to always pass None for the model
    # below.  For example, in SMapBase.__eq__, we should probably pass
    # in the map's model.
    if isinstance(ref, z3.ArithRef):
        if ref.is_int():
            return SInt._wrap(ref, None)
        return SArith._wrap(ref, None)
    if isinstance(ref, z3.BoolRef):
        return SBool._wrap(ref, None)
    return SExpr._wrap(ref, None)

def wraplist(reflist):
    """Convert a list of values to a list of simsym.Symbolic things,
    by calling wrap() on each item in the list."""
    return [wrap(ref) for ref in reflist]

#
# AST matching code.  Unused because the simplifier aggressively
# rewrites things back to its own preferred representation, and
# it is difficult to rewrite just one child in an AST (no generic
# AST node constructors).
#

def matchvar(n):
    return z3.Int('@match:%s' % n)

def ast_match2(a, pat, matchvars):
    if pat.decl().name().startswith('@match:'):
        varname = pat.decl().name()[7:]
        cur = matchvars.get(varname, None)
        if cur is None:
            matchvars[varname] = a
            return True
        return cur.eq(a)

    if a.__class__ != pat.__class__: return False
    if not a.decl().eq(pat.decl()): return False
    if a.num_args() != pat.num_args(): return False
    for (aa, pa) in zip(a.children(), pat.children()):
        if not ast_match2(aa, pa, matchvars): return False
    return True

def ast_match(a, pat, matchvars):
    matchvars.clear()
    return ast_match2(a, pat, matchvars)

def ast_cleanup(a):
    matchvars = {}
    if ast_match(a, z3.Not(matchvar('a') == matchvar('b')), matchvars):
        return matchvars['a'] != matchvars['b']
    return a

#
# Execution graph visualization
#

class Graph(object):
    def __init__(self):
        self.__nodes = []
        self.__edges = []

    def new_node(self):
        self.__nodes.append(GraphNode())
        return self.__nodes[-1]

    def new_edge(self, n1, n2, label):
        self.__edges.append((n1, n2, label))

    def show(self):
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            p = subprocess.Popen(["dot", "-Tpdf"], stdin=subprocess.PIPE,
                                 stdout=f)
            self.to_dot(p.stdin)
            p.stdin.close()
            p.wait()
            subprocess.check_call(["evince", f.name])

    def to_dot(self, fp=sys.stdout):
        print >>fp, "digraph G {"
        #print >>fp, "rankdir=LR;"
        for n in self.__nodes:
            print >>fp, "n%s [label=%s,color=%s,shape=box];" % \
                (id(n), self.__dot_quote(n.str_label()),
                 self.__dot_quote(n._color))
        for n1, n2, label in self.__edges:
            print >>fp, "n%s -> n%s [label=%s];" % \
                (id(n1), id(n2), self.__dot_quote(str(label)))
        print >>fp, "}"

    def __dot_quote(self, s):
        return '"' + (s.replace("\\", "\\\\")
                      .replace("\n", "\\l")
                      .replace("\"", "\\\"")) + '"'

class GraphNode(object):
    def __init__(self):
        self._label = ["?"]
        self._color = "black"

    def set_label(self, *parts):
        self._label = parts

    def set_color(self, color):
        self._color = color

    def str_label(self):
        res = []
        for part in self._label:
            if isinstance(part, Symbolic):
                res.append(str(z3.simplify(unwrap(part))))
            else:
                res.append(str(part))
        res = "\n".join(res).splitlines()
        if len(res) > 10:
            res = res[:5] + [".. %d more lines .." % (len(res) - 9)] + res[-4:]
        return "\n".join(res)

#
# Symbolic executor
#

solver = None
schedq = []
curgraph = None
cursched = None
curschedidx = None

# Map from user-provided Symbolic variable names to Symbolic instance
# constructors.  Each instance constructor must take two arguments:
# the user variable name and a simsym.Model object that binds the Z3
# environment.
var_constructors = {}

# Map from Z3 constant names to (outer Symbolic type, compound path)
constTypes = {}

def get_solver():
    """Return the current z3.Solver(), or raise RuntimeError if no
    solver is active."""
    if solver is None:
        raise RuntimeError("Symbolic execution attempted outside symbolic_apply")
    return solver

def queue_schedule(s):
    # print "currently in schedule", cursched
    # print "queueing schedule", s
    schedq.append(s)

def str_state():
    """Return the current path constraint as a string, or None if the
    path is unconstrained."""

    asserts = get_solver().assertions()
    if len(asserts) == 0:
        return None
    return str(z3.simplify(z3.And(*asserts),
                           expand_select_store=True))

def simplify(expr, try_harder=False):
    core_simplifier = 'ctx-simplify'
    if try_harder:
        ## ctx-solver-simplify is very slow; use the
        ## faster but less powerful ctx-simplify.
        core_simplifier = 'ctx-solver-simplify'
    t = z3.Repeat(z3.Then(z3.With('simplify', expand_select_store=True,
                                              ite_extra_rules=True,
                                              expand_store_eq=True),
                          'propagate-values',
                          'ctx-simplify',
                          core_simplifier,
                          ))
    subgoals = t(unwrap(expr))
    if len(subgoals[0]) == 0:
        s = wrap(z3.BoolVal(True))
    else:
        s = wrap(z3.simplify(unwrap(symand([symand(wraplist(g))
                                            for g in subgoals]))))
    return s

def assume(e):
    """Declare symbolic expression e to be True."""

    if e is True:
        return

    # Is this assumption already implied?  This isn't strictly
    # necessary, but it cleans up generated expressions and the
    # execution graph.  It also sometimes lets z3 decide a path
    # condition that it otherwise can't (which is probably a z3 bug).
    solver = get_solver()
    solver.push()
    solver.add(unwrap(symnot(e)))
    sat = solver.check()
    solver.pop()
    if sat == z3.unsat:
        return

    # Update the schedule and execution graph.  (We wouldn't need to
    # track assumptions in the schedule except that we want to avoid
    # duplicate nodes in the execution graph.)
    global cursched, curschedidx
    if len(cursched) == curschedidx:
        if len(cursched):
            gnode = cursched[-1][1]
        else:
            gnode = curgraph.new_node()
        gnode.set_label(e, get_caller())
        nextnode = curgraph.new_node()
        curgraph.new_edge(gnode, nextnode, "")

        cursched.append((None, nextnode))

    assert cursched[curschedidx][0] is None
    curschedidx = curschedidx + 1

    solver.add(unwrap(e))
    sat = solver.check()
    if sat == z3.unknown:
        s2 = z3.Solver()
        s2.add(*solver.assertions())
        sat = s2.check()
        reason = s2.reason_unknown()

    if sat == z3.unsat:
        raise UnsatisfiablePath()
    elif sat != z3.sat:
        raise UncheckableConstraintError(unwrap(e), reason)

class SymbolicApplyResult(object):
    """The result of a symbolic application."""

    def __init__(self, value, path_condition_list, var_constructors,
                 const_types):
        self.__value = value
        self.__path_condition_list = path_condition_list
        self.__var_constructors = var_constructors
        self.__const_types = const_types

    @property
    def value(self):
        """The value returned by the application (which may be Symbolic)."""
        return self.__value

    @property
    def path_condition_list(self):
        """The path condition of this result, as a list of SBools."""
        return self.__path_condition_list

    @property
    def path_condition(self):
        """The path condition as a single SBool conjunction."""
        return symand(self.__path_condition_list)

    def get_model(self, z3_model=None):
        """Return a Model interpreting the variables declared by this code path.

        The returned Model represents satisfying, concrete assignments
        consistent with self's path condition for all of the variables
        created by self's code path.

        By default, this will construct a model from the path
        condition, but the caller can also provide a specific Z3 model
        (which must be consistent with the path condition).
        """

        if z3_model is None:
            sat, z3_model = check(self.path_condition)
            assert sat == z3.sat

        return Model(self.__var_constructors, z3_model)

    def symbolic_type(self, const):
        """Return a representation of the Symbolic type of a Z3 constant.

        const must satisfy z3.is_const.  This will map its type back
        to a pseudo-Symbolic type.  Because the Z3 type hierarchy does
        not match the Symbolic type hierarchy, const may not have a
        direct mapping to a Symbolic type, so this pseudo-Symbolic
        type will have one of the following forms:

        * Symbolic subclass - An exact Symbolic type.  This will be a
          synonym type if possible (this allows additional information
          to be associated with the Symbolic type that would otherwise
          be lost inside Z3).

        * (index pseudo-Symbolic type, value pseudo-Symbolic type) - A
          Z3 array from the index to the value type.
        """

        # XXX The result of this will only give Symbolic types for
        # primitives.  Should this perhaps represent the whole type
        # hierarchy?  Like
        #  ("struct", SStruct | SSynonym, field type) |
        #  ("map", SMap | SSynonym, index type, value type) |
        #  ("primitive", type)

        outer_type, path = self.__const_types[str(const)]
        def rec(outer_type, path):
            if issubclass(outer_type, SStructBase) and len(path):
                return rec(outer_type._fields[path[0]], path[1:])
            elif issubclass(outer_type, SMapBase):
                return (rec(outer_type._indexType, ()),
                        rec(outer_type._valueType, path))
            elif issubclass(outer_type, SymbolicConst) and not len(path):
                return outer_type
            else:
                raise Exception("Failed to resolve type of %s" % const)
        return rec(outer_type, path)

def symbolic_apply(fn, *args):
    """Evaluate fn(*args) under symbolic execution.

    This yields a series of SymbolicApplyResult objects; one for each
    distinct code path.  If a code path leads to an uncheckable
    constraint, this prints a warning and terminates that path.
    """

    global curgraph
    curgraph = Graph()

    # Prime the schedule with a root node.  We skip this during
    # execution, but it means we can always use graph node
    # cursched[-1][1].
    queue_schedule([(None, curgraph.new_node())])

    global schedq
    if len(schedq) != 1:
        # XXX Given that this is a generator, it would be nice if you
        # could have more than one symbolic_apply going at once.  We
        # probably still need global state, but if we bundled it all
        # into one object, it would be easy to swap in and out.
        raise Exception("Recursive symbolic_apply?")

    global var_constructors
    init_var_constructors = var_constructors.copy()

    try:
        for sar in __symbolic_apply_loop(fn, *args):
            yield sar
#        curgraph.show()
    finally:
        var_constructors = init_var_constructors
        # Don't permanently block symbolic_apply.  This is
        # particularly important if the caller terminated its
        # iteration early and we're exiting because of a
        # GeneratorExit.
        schedq = []

def __symbolic_apply_loop(fn, *args):
    while len(schedq) > 0:
        global cursched
        cursched = schedq.pop()

        global curschedidx
        curschedidx = 1

        # XXX Some variables are declared outside the symbolic_apply
        # environment.  Do we need to keep track of those, too?
        global constTypes
        constTypes = {}

        global solver
        solver = z3.Solver()
        try:
            rv = fn(*args)
            cursched[-1][1].set_label(rv)
            yield SymbolicApplyResult(rv, wraplist(solver.assertions()),
                                      var_constructors.copy(),
                                      constTypes.copy())
        except UnsatisfiablePath:
            cursched[-1][1].set_label("Unsatisfiable path")
            cursched[-1][1].set_color("blue")
        except UncheckableConstraintError as e:
            cursched[-1][1].set_label("Exception: " + str(e))
            cursched[-1][1].set_color("red")
            print str(e)
        except Exception as e:
            cursched[-1][1].set_label("Exception: " + str(e))
            cursched[-1][1].set_color("red")
            if len(e.args) == 1:
                e.args = ('%s in symbolic state:\n%s' % (e.args[0], str_state()),)
            else:
                e.args = e.args + (str_state(),)
            curgraph.show()
            raise
        finally:
            solver = None

def check(e):
    solver = z3.Solver()
    solver.add(unwrap(e))
    c = solver.check()
    if c == z3.sat:
        m = solver.model()
    else:
        if c == z3.unknown:
            m = solver.reason_unknown()
        else:
            m = None
    return (c, m)

class Model(object):
    """A Model interprets symbolic expressions into concrete values.

    A Model object is indexed using the variable names that were
    provided by the user for calls to Symbolic.var and constVal and
    their ilk.  Indexing into the Model will return concrete Python
    values or compound values that support indexing and field
    selection just like during symbolic execution.
    """

    # The main complication here is that the Z3 constant names are
    # derived from user-provided names, but may not be identical in
    # the presence of compound types.  Hence, we actually use the same
    # Symbolic types as the original variables were declared as to get
    # the exact same expression transformations that we got during
    # symbolic execution.  The difference is that we pass the model in
    # to this instance hierarchy and when we reach a base value, we
    # evaluate the expression used to reach the value in the model to
    # get a concrete value.

    def __init__(self, var_constructors, z3_model):
        self.__var_constructors = var_constructors
        self.__z3_model = z3_model
        self.__track = False
        self.__asignments = []

    def __getitem__(self, name):
        return self.__var_constructors[name](name, self)

    def track_assignments(self, enable=True):
        """Enable or disable assignment tracking."""
        self.__track = enable

    def assignments(self):
        """Return a list of expressions evaluated by the model.

        The returned list consists of pairs of (expression, value)
        where both expression and value are instances of Symbolic.
        The list will be in the order the expressions were evaluated,
        with duplicate expressions suppressed.
        """
        return self.__asignments

    def _eval(self, expr):
        """Evaluate a Symbolic expression to a concrete Python value."""

        # model_completion asks Z3 to make up concrete values if they
        # are not interpreted in the model.
        z3val = self.__z3_model.evaluate(unwrap(expr), model_completion=True)
        if z3.is_int_value(z3val):
            res = z3val.as_long()
        elif z3.is_true(z3val):
            res = True
        elif z3.is_false(z3val):
            res = False
        elif z3val.sort_kind() == z3.Z3_UNINTERPRETED_SORT:
            res = type(expr)._wrap(z3val, None)
        else:
            # Either expr is not a concrete value, or we don't know
            # how to extract its concrete value (it could be, e.g., an
            # enum value)
            raise Exception("Expression %s is not a concrete value" % expr)

        if self.__track:
            for aexpr, _ in self.__asignments:
                if unwrap(expr).eq(unwrap(aexpr)):
                    break
            else:
                val = type(expr)._wrap(z3val, None)
                self.__asignments.append((expr, val))

        return res

#
# Helpers for tracking "internal" variables
#

internal_vars = {None: SInt.var('__dummy')}

def add_internal(v):
    for sub in flatten_compound(v._z3_value()):
        internal_vars[str(sub)] = sub

def internals():
    return [v for _, v in internal_vars.iteritems()]

#
# Utilities
#

def strtype(x):
    if type(x) == types.InstanceType:
        return x.__class__.__name__
    else:
        return type(x).__name__

def get_caller():
    import inspect
    cur = inspect.currentframe()
    caller = inspect.getouterframes(cur, 3)
    return "%s:%s" % (os.path.basename(caller[2][1]), caller[2][2])
