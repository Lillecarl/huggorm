"""
The evaluation binding: EvalState is the affine SERVICE exemplar.

`nix::EvalState` is documented as not thread-safe, one per thread;
here that fact becomes `threading="affine"`. Values live on the
state's thread: they are produced by state methods and attach to its
runner in the async layer. `force` mutates a value in place, which is
the affine-value-as-parameter case.

Two facts about libexpr shape everything below, and both are LIFETIME.

A value lives in the collector's heap and belongs to nobody: it dies
when the collector can no longer see a pointer to it, even while its
EvalState lives on. Python's heap is not scanned, so a wrapper
holding a bare pointer roots nothing.

And a value is not self-describing. An attribute name is a `Symbol`,
a `uint32_t` index into the PRODUCING state's symbol table, so
rendering one needs that state in hand.

`huggorm::Bridge` answers both: it holds an upstream `RootValue` and
a share of the state that made it, so the state cannot die under a
value that still points into its memory. That is producer pinning as
a C++ fact, beside the server's `parents=[self]`. It is the only C++
in this binding a declaration could not have written, and
`cpp/eval.hpp` says why line by line.
"""

from huggorm_decl.decl.flakeref import FlakeRef
from huggorm_decl.decl.path import ErrorInfo, StorePath
from huggorm_decl.decl.store import Store
from huggorm_dsl.declare import (
    F64,
    I64,
    NIX_2_35,
    Bint,
    Cxx,
    PyFunc,
    Str,
    StrView,
    binding,
    binds,
    blocks,
    cxx_name,
    fills,
    gc_slots,
    guard,
    header,
    instant,
    names,
    needs,
    produced,
    produces,
    reads,
    startup,
    tagged,
    threading,
    tree,
    wire_value,
)


@produced(by="EvalState")
@header("huggorm_decl/cpp/eval.hpp")
@binding(
    # One state per thread, and its values belong to that thread. The
    # async layer inherits the runner rather than making a new one -
    # a value operation touches the state's own memory, so running it
    # anywhere else is a data race.
    threading="affine",
    # Reading a forced value is a memory read. Forcing is what waits,
    # and it says so for itself.
    blocking=False,
    cxx="huggorm::Bridge",
)
# Bridge is a HANDLE over a TAGGED UNION. Reach the value with
# `get()`, ask which arm it holds with `type<true>()`, and here is
# every arm nix::ValueType has.
#
# ONE table, read two ways. `type_name` answers a caller with the
# name; every `@guard` below checks against the enumerator. Adding an
# arm is one line here rather than a switch case and a guard that
# have to agree.
@tagged(
    "get()", "type<true>()",
    thunk="nix::nThunk",
    int="nix::nInt",
    float="nix::nFloat",
    bool="nix::nBool",
    string="nix::nString",
    path="nix::nPath",
    null="nix::nNull",
    attrs="nix::nAttrs",
    list="nix::nList",
    function="nix::nFunction",
    external="nix::nExternal",
    failed="nix::nFailed",
)
# How a value TREE is walked, read by the RPC layer so that no layer
# above this declaration knows what a Value is or which of its methods
# do what (tasks/030). `kind` names the accessor that says what this
# node is; its answer selects one of the branches below. A kind named
# nowhere here crosses as a proxy - and real Nix has five of those:
# thunk, function, external, failed and path. Laziness the wire cannot
# serialize, plus three things that are not data at all.
@tree(
    kind="type_name",
    # What makes two nodes THE SAME node. A fresh wrapper is built for
    # every access, so Python identity says nothing: two wrappers over
    # one value differ, and a wrapper that dies hands its id() to the
    # next one. The underlying value's address is the identity.
    identity="_identity",
    # kind reported by `kind` -> [wire type, accessor]. The wire type
    # is what picks the arm, so the layer above reads a declared type
    # name rather than a label this file invented.
    scalars={"int": ["int", "integer"],
             "float": ["float", "floating"],
             "string": ["str", "string_value"],
             "bool": ["bool", "boolean"]},
    list={"size": "size", "item": "at"},
    attrs={"size": "size", "name": "name_at", "value": "value_at"},
)
class Value:
    """One GC-resident nix::Value, rooted for as long as Python holds
    it.

    Wire-proxy despite being "just data": a thunk must force on its
    home thread and forcing mutates in place. A future refinement may
    serialize forced scalars; until then, proxy.

    Produced, never constructed. A value comes from an EvalState -
    parsed, evaluated or built - and there is nothing a caller could
    correctly make one from.

    EVERY ACCESSOR GUARDS, and that is not politeness. `nix::Value` is
    a tagged union whose readers are `noexcept` and undefined on the
    wrong tag: reading `integer` off a string is not an error, it is a
    reinterpretation of the payload. So no method here binds a
    pointer-to-member on `nix::Value`; each one is a Bridge method
    that checks the tag first."""

    @cxx_name("identity")
    def _identity(self) -> I64:
        """The underlying value's address, as a number.

        Private: it is not surface, so the codegen leaves it out of
        every generated form. The tree walk uses it to visit a shared
        value once - values are immutable and shared freely, so
        without it a diamond is copied and a cycle never ends."""

    def is_gc_managed(self) -> Bint:
        """True when this value lives inside a GC-allocated block.

        Bound straight from gc.h: a no-op integration cannot fake
        it."""

    @names
    def type_name(self) -> Str:
        """What this value is: "thunk", "int", "float", "bool",
        "string", "path", "null", "attrs", "list", "function",
        "external" or "failed".

        The full `nix::ValueType`, not a subset. A kind the `@tree`
        map above does not name crosses as a proxy, so naming all of
        them here costs nothing and hides nothing."""

    @guard("int")
    def integer(self) -> I64:
        """This value as an integer. Raises on a thunk, or on a value
        of another kind.

        nix::Value::integer answers a NixInt, which is a checked
        int64 with an explicit conversion - so the cast the emitter
        already writes for an I64 return is the whole of it."""

    @guard("float")
    @cxx_name("fpoint")
    def floating(self) -> F64:
        """This value as a float. Raises as `integer` does.

        `1.0` is a float and `1` is an int in Nix, so neither accessor
        converts the other's arm."""

    @guard("string")
    @cxx_name("string_view")
    def string_value(self) -> StrView:
        """This value as a string. Raises as `integer` does.

        The string CONTEXT is dropped. A Nix string can carry store
        paths it depends on, and nothing above this layer can act on
        them yet; a declaration that carried them would be inventing
        a surface rather than binding one."""

    @guard("bool")
    def boolean(self) -> Bint:
        """This value as a bool. Raises as `integer` does."""

    # Collections. Reading is by index, which is also how the
    # alphabetical order of an attribute set reaches Python.
    #
    # That order is not free. nix::Bindings is sorted by Symbol ID,
    # which is INTERNING order - the order a name was first seen
    # anywhere in the process - so an attribute set comes back in
    # whatever order its names happened to be interned.
    # `lexicographicOrder` is the accessor that hides it, and the
    # Bridge caches the result because the walk reads every index
    # against one object.
    #
    # A list[Value] or dict[str, Value] accessor is deliberately
    # absent. It needs a collection of PROXIES, which is the recursive
    # value message (tasks/030), not another loop here.

    def size(self) -> I64:
        """Elements in a list, or attributes in an attribute set.

        No `@guard`: it takes ONE arm and this accepts two. Anything
        else raises the `TypeError` a guard raises, naming both."""
        Cxx("""
if (self.get()->type<true>() == nix::nList)
    return static_cast<std::int64_t>(self.get()->listSize());
if (self.get()->type<true>() == nix::nAttrs)
    return static_cast<std::int64_t>(self.get()->attrs()->size());
throw nix::TypeError(self.state(), "expected %s or %s but found %s",
                     nix::showType(nix::nList), nix::showType(nix::nAttrs),
                     nix::showType(*self.get()));
        """)

    @guard("list")
    def length(self) -> I64:
        """Elements in a list. `size` for a caller that must refuse
        an attribute set."""
        Cxx("return static_cast<std::int64_t>(self.get()->listSize());")

    @guard("attrs")
    def names(self) -> list[Str]:
        """Every attribute name, in alphabetical order. `name_at` for
        each index, in one call."""
        Cxx("""
std::vector<std::string> names;
for (const auto * attr : self.sorted())
    names.push_back(self.symbol(attr->name));
return names;
        """)

    @guard("list")
    @needs("huggorm_decl/cpp/eval_errors.hpp")
    def at(self, index: I64) -> Value:
        """One element of a list.

        It may still be a thunk: forcing a list forces the list, not
        what is in it. An index past either end raises `ListIndex`,
        which names the index and the size, as `builtins.elemAt`
        does."""
        Cxx("""
auto items = self.get()->listView();
if (index < 0 || static_cast<std::size_t>(index) >= items.size())
    throw huggorm::ListIndex(
        self.state(), "list index %d is out of bounds for a list of size %d",
        index, items.size());
return self.wrap(items[static_cast<std::size_t>(index)]);
        """)

    @guard("attrs")
    def name_at(self, index: I64) -> Str:
        """One attribute name, in alphabetical order."""
        Cxx("""
const auto & by_name = self.sorted();
if (index < 0 || static_cast<std::size_t>(index) >= by_name.size())
    throw std::runtime_error("attribute index out of range");
return self.symbol(by_name[static_cast<std::size_t>(index)]->name);
        """)

    @guard("attrs")
    def value_at(self, index: I64) -> Value:
        """One attribute value, in alphabetical order of name."""
        Cxx("""
const auto & by_name = self.sorted();
if (index < 0 || static_cast<std::size_t>(index) >= by_name.size())
    throw std::runtime_error("attribute index out of range");
return self.wrap(by_name[static_cast<std::size_t>(index)]->value);
        """)

    @guard("attrs")
    def has(self, name: Str) -> Bint:
        """Whether this attribute set carries that name."""
        Cxx("return self.get()->attrs()->get(self.intern(name)) != nullptr;")

    @guard("attrs")
    @needs("huggorm_decl/cpp/eval_errors.hpp")
    def get(self, name: Str) -> Value:
        """One attribute by name.

        A missing one raises `MissingAttribute`, an `EvalError` with
        the words and the suggestions `{ ... }.name` gives
        (eval.cc:1438)."""
        Cxx("""
const auto * attr = self.get()->attrs()->get(self.intern(name));
if (attr == nullptr) {
    nix::StringSet names;
    for (const auto & each : *self.get()->attrs())
        names.insert(self.symbol(each.name));
    huggorm::MissingAttribute missing(
        self.state(), "attribute '%s' missing", name);
    missing.with_suggestions(nix::Suggestions::bestMatches(names, name));
    throw missing;
}
return self.wrap(attr->value);
        """)

    @blocks
    @needs("nix/expr/value-to-json.hh", "nlohmann/json.hpp")
    def to_json(self, copy_to_store: Bint = False) -> Str:
        """This value as JSON text, forced all the way down.

        What `nix eval --json` prints: strict, and a path stays a
        path rather than being copied into the store. `copy_to_store`
        copies it and answers the store path, as `builtins.toJSON`
        does.

        BLOCKS: it forces every value it reaches. Raises for a
        function, which JSON cannot hold."""
        Cxx("""
huggorm::gc_register_thread();
nix::NixStringContext context;
return nix::printValueAsJSON(
    self.state(), true, *self.get(), nix::noPos, context, copy_to_store).dump();
        """)

    @blocks
    @needs("nix/expr/value-to-json.hh", "nlohmann/json.hpp")
    def realise_json(self, copy_to_store: Bint = False) -> Str:
        """`to_json`, with every store path its strings name built.

        The paths a string's context names may not exist yet; this
        builds or substitutes them, as `realise_string` does, and
        rewrites a content-addressed placeholder to the path it
        became. So a caller can read what the JSON names.

        BLOCKS: it forces every value it reaches, and it may build."""
        Cxx("""
huggorm::gc_register_thread();
nix::NixStringContext context;
auto text = nix::printValueAsJSON(
    self.state(), true, *self.get(), nix::noPos, context, copy_to_store).dump();
auto rewrites = self.state().realiseContext(context, nullptr, false);
return nix::rewriteStrings(text, rewrites);
        """)

    @blocks
    @needs("functional")
    def string_context(self) -> list[Str]:
        """The string context of every string this value holds, each
        element in Nix's own encoding (`NixStringContextElem`), sorted.

        One set for the whole value, forced all the way down. The
        context is what a string owes the store: a derivation output
        it names, or a path it was built from.

        BLOCKS: it forces every value it reaches."""
        Cxx("""
huggorm::gc_register_thread();
nix::NixStringContext context;
std::function<void(nix::Value &)> walk = [&](nix::Value & value) {
    self.state().forceValue(value, nix::noPos);
    switch (value.type()) {
    case nix::nString:
        nix::copyContext(value, context);
        break;
    case nix::nList:
        for (auto * element : value.listView())
            walk(*element);
        break;
    case nix::nAttrs:
        for (auto & attr : *value.attrs())
            walk(*attr.value);
        break;
    default:
        break;
    }
};
walk(*self.get());
std::vector<std::string> out;
for (auto & element : context)
    out.push_back(element.to_string());
return out;
        """)

    @blocks
    def realise_string(self) -> Str:
        """This value as a string, with everything it names built.

        A string's context names the store paths it refers to, and a
        derivation output in it may not exist yet. This builds or
        substitutes each one first, so the answer names paths that
        are there: `"${pkgs.hello}/bin/hello"` becomes a program to
        run.

        Not an import from a derivation: the caller holds the value,
        and evaluation is over. So `allow-import-from-derivation =
        false` does not refuse it, as it does not refuse `nix build`.

        BLOCKS: it may build."""
        Cxx("""
huggorm::gc_register_thread();
return self.state().realiseString(*self.get(), nullptr, false, nix::noPos);
        """)

    @guard("list")
    @blocks
    def realise_argv(self) -> list[Str]:
        """A list of strings, with everything they name built.

        `realise_string` for each element, with ONE build for the
        whole list, so an argument vector costs one round of building
        rather than one per argument."""
        Cxx("""
huggorm::gc_register_thread();
nix::NixStringContext context;
std::vector<std::string> argv;
for (auto * element : self.get()->listView())
    argv.emplace_back(self.state().coerceToString(
        nix::noPos, *element, context,
        "while evaluating an element of an argument vector",
        false, false).toOwned());
auto rewrites = self.state().realiseContext(context, nullptr, false);
for (auto & argument : argv)
    argument = nix::rewriteStrings(argument, rewrites);
return argv;
        """)

    # -- functions -------------------------------------------------------
    #
    # `nFunction` is ONE type name over THREE payloads - a lambda, a
    # primop, and a primop that already holds some of its arguments -
    # told apart by `isLambda()`, `isPrimOp()` and `isPrimOpApp()`
    # (value.hh:1111). So `@guard("function")` is NECESSARY AND NOT
    # SUFFICIENT here: it proves the arm and says nothing about which
    # of the three, and `lambda()` on a primop is exactly the payload
    # reinterpretation this class exists to prevent.
    #
    # Every body below therefore checks its own shape first. The guard
    # cannot: it is generated from `@tagged`, which names the twelve
    # `nix::ValueType` arms, and the three function shapes are not
    # types - they are storage tags under one type.
    #
    # CURRIED, so there is no arity. `x: y: body` is a function
    # returning a function, and nothing can say how many arguments it
    # takes without applying it. Only a primop declares one, and
    # `getDoc` leaves a lambda's `arity` at 0 with upstream's own
    # FIXME beside it (eval.cc:622). No accessor here offers a
    # lambda an arity, because any that did would be lying.

    @guard("function")
    def is_lambda(self) -> Bint:
        """Whether this function is a `x:` or `{ a, b }:` lambda."""
        Cxx("return self.get()->isLambda();")

    @guard("function")
    def is_primop(self) -> Bint:
        """Whether this function is a builtin, with none of its
        arguments applied yet."""
        Cxx("return self.get()->isPrimOp();")

    @guard("function")
    def is_primop_app(self) -> Bint:
        """Whether this is a builtin holding SOME of its arguments.

        The third shape, and the one with no introspection at all:
        `getDoc` has no branch for it and returns nothing
        (eval.cc:571-648), and nothing in libexpr says how many
        arguments are still wanted. Counting them means walking the
        application chain, which is work this declaration does not do
        and a caller cannot ask for."""
        Cxx("return self.get()->isPrimOpApp();")

    @blocks
    def apply(self, arg: Value) -> Value:
        """Apply one argument. `f x`, and the answer may be a function.

        The curried form, so this is how every Nix function is called
        and the by-name form below is the special case. Applying `x:
        y: body` once answers another function.

        BLOCKS: it runs the evaluator. The argument is not forced
        first, and the RESULT comes back in WHNF - `callFunction`
        evaluates the lambda's body with `Expr::eval`, which produces
        an evaluated value rather than a thunk (eval.cc:1600). So the
        top level is forced and everything inside it stays lazy: `x: {
        a = x; }` answers an attribute set whose `a` is still a
        thunk.

        Measured, because the opposite was written here first. A
        docstring claiming the result was a thunk failed its own gate
        with `assert 'int' == 'thunk'`.

        `noPos`, because the call site is Python and there is no Nix
        position to name. The trace on a failure therefore starts
        inside the function rather than at a caller.

        No `@guard`: `callFunction` reads no payload before it checks
        the type, and it also calls a set with `__functor`, as `f x`
        does. Anything else raises Nix's own `TypeError`."""
        Cxx("""
huggorm::gc_register_thread();
auto * out = self.state().allocValue();
self.state().callFunction(*self.get(), *arg.get(), *out, nix::noPos);
return self.wrap(out);
        """)

    @guard("function")
    @blocks
    def apply_auto(self, args: Value) -> Value:
        """Apply an attribute set BY NAME, filling defaults.

        `autoCallFunction`, which is what `--arg` reaches. One round
        trip for a whole argument set, where `apply` is one per
        argument - and it fills each formal the set does not mention
        from that formal's own default.

        It REFUSES a function that declares no formals, and that
        refusal is the reason this is a separate method rather than a
        convenience. Upstream returns the function UNAPPLIED in that
        case (eval.cc:1795-1798, `res = fun`), so a caller who passed
        arguments to `x: x` would get back the function and no
        indication that nothing happened. That is this repo's named
        failure mode sitting in libexpr, and a binding may not be
        more permissive than the C++ it binds.

        It forces `self`, unlike everything else here, because
        `autoCallFunction` does (eval.cc:1783) - so a thunk that
        evaluates to a function is accepted where `apply` would
        refuse it. `@guard` still rejects a thunk before we get here,
        so that reachability belongs to a caller who forced first.

        An attribute set with a `__functor` attribute is CALLABLE in
        Nix and is refused here, because `@guard("function")` sees
        `nAttrs`. Upstream follows the functor (eval.cc:1786-1792);
        this does not, and a caller reaches it by applying the
        `__functor` attribute itself.

        A required formal with no value and no default raises
        MissingArgumentError, which crosses as a declared Nix
        error."""
        Cxx("""
if (!self.get()->isLambda()
    || !self.get()->lambda().fun->getFormals().has_value())
    throw std::invalid_argument(
        "apply_auto needs a lambda that declares formals; nix's "
        "autoCallFunction answers the function UNAPPLIED for anything "
        "else, which is indistinguishable from a call that did nothing");
if (args.get()->type<true>() != nix::nAttrs)
    throw std::invalid_argument(
        "apply_auto needs an attribute set of arguments");
huggorm::gc_register_thread();
auto * out = self.state().allocValue();
self.state().autoCallFunction(*args.get()->attrs(), *self.get(), *out);
return self.wrap(out);
        """)

    @guard("attrs")
    @blocks
    @needs("nix/expr/get-drvs.hh")
    def drv_path(self) -> StorePath:
        """The `.drv` this derivation value names.

        What joins evaluation to building: `DerivedPathBuilt` takes
        this and an output spec, and `Store.build_paths` takes that.
        libexpr's `getDerivation` decides what counts as a derivation,
        the same test `nix build` uses on an installable.

        BLOCKS: reading `drvPath` forces it, and that instantiates the
        derivation, so the `.drv` is written to the state's store.

        Raises for an attribute set that is not a derivation, and for
        a derivation with no `drvPath`."""
        Cxx("""
huggorm::gc_register_thread();
auto info = nix::getDerivation(self.state(), *self.get(), false);
if (!info)
    throw nix::EvalError(self.state(), "selected value is not a derivation");
auto path = info->queryDrvPath();
if (!path)
    throw nix::EvalError(self.state(), "the derivation has no drvPath");
return *path;
        """)

    @guard("attrs")
    @blocks
    @needs("nix/expr/get-drvs.hh")
    def output_paths(self) -> dict[str, StorePath | None]:
        """Each output this derivation value names, and its path.

        `PackageInfo::queryOutputs`, every output rather than only
        `meta.outputsToInstall`. A path is None only for a set with
        no `outputs` list and no `outPath`. A floating
        content-addressed output raises: its `outPath` is a
        placeholder, not a store path.

        BLOCKS for the reason `drv_path` does. Raises for an attribute
        set that is not a derivation."""
        Cxx("""
huggorm::gc_register_thread();
auto info = nix::getDerivation(self.state(), *self.get(), false);
if (!info)
    throw nix::EvalError(self.state(), "selected value is not a derivation");
std::map<std::string, std::optional<nix::StorePath>> out;
for (auto & [name, path] : info->queryOutputs(true, false))
    out.emplace(name, path);
return out;
        """)

    @guard("function")
    def lambda_name(self) -> Str:
        """The name this lambda was bound to, or "" for an anonymous
        one.

        `ExprLambda::name` (nixexpr.hh:517), which the parser sets
        when a lambda is the right-hand side of an attribute or a
        `let`. It is for a human: two bindings of one lambda do not
        make two functions, and this reports whichever name the
        expression carried."""
        Cxx("""
if (!self.get()->isLambda())
    throw std::invalid_argument("value is not a lambda");
const auto & name = self.get()->lambda().fun->name;
return name ? self.symbol(name) : std::string();
        """)

    @guard("function")
    def lambda_arg(self) -> Str:
        """The name bound to the WHOLE argument, or "".

        Two spellings reach it, which is why this is not "the
        parameter name". `x: body` binds the argument to `x` and
        declares no formals. `{ a, b } @ rest: body` declares formals
        AND binds the whole set to `rest`, so both this and
        `formal_names` answer.

        "" means the lambda takes formals and named no binding for the
        set itself."""
        Cxx("""
if (!self.get()->isLambda())
    throw std::invalid_argument("value is not a lambda");
const auto & arg = self.get()->lambda().fun->arg;
return arg ? self.symbol(arg) : std::string();
        """)

    @guard("function")
    def has_formals(self) -> Bint:
        """Whether this lambda declares `{ a, b }` style formals.

        The one thing that separates the two calling conventions:
        `apply_auto` needs it and `apply` does not care."""
        Cxx("""
if (!self.get()->isLambda())
    throw std::invalid_argument("value is not a lambda");
return self.get()->lambda().fun->getFormals().has_value();
        """)

    @guard("function")
    def accepts_extra(self) -> Bint:
        """Whether the formals end in `...`.

        It changes what `apply_auto` PASSES, not just what it
        accepts: with an ellipsis upstream forwards every argument it
        was given, and without one it forwards only the declared
        formals (eval.cc:1803-1813)."""
        Cxx("""
if (!self.get()->isLambda())
    throw std::invalid_argument("value is not a lambda");
auto formals = self.get()->lambda().fun->getFormals();
return formals.has_value() && formals->ellipsis;
        """)

    # ALPHABETICAL, and the sort is OURS. Both accessors below sort
    # by name in the body, which needs saying because upstream looks
    # like it already did.
    #
    # `validateFormals` sorts by `std::tie(a.name, a.pos)`
    # (parser-state.hh:302), and `name` is a `Symbol` - an interning
    # ID. So upstream's order is the order each name was first seen
    # ANYWHERE in the process, which is neither source order nor
    # alphabetical and is not reproducible between two runs.
    #
    # That is the same fact this class already records for attribute
    # sets, where `Bridge::sorted()` pays a sort to hide it. Measured
    # the same way too: a docstring claiming name order failed with
    # `assert ['zebra', 'apple', 'mango'] == ['apple', 'mango',
    # 'zebra']`, which is the interning order of a test that had just
    # mentioned zebra first.
    #
    # Source order would be the other defensible answer and is not
    # available: `Formal::pos` survives, but sorting by it would need
    # positions this declaration does not carry.

    @guard("function")
    def formal_names(self) -> list[Str]:
        """Every formal this lambda declares, alphabetically.

        Empty for a `x:` lambda, which declares none.

        A LIST rather than an index and a count, because the whole
        point of reading formals is to build one signature - and over
        RPC an indexed accessor would be one round trip per
        parameter."""
        Cxx("""
if (!self.get()->isLambda())
    throw std::invalid_argument("value is not a lambda");
std::vector<std::string> names;
auto formals = self.get()->lambda().fun->getFormals();
if (formals.has_value())
    for (const auto & formal : formals->formals)
        names.push_back(self.symbol(formal.name));
std::sort(names.begin(), names.end());
return names;
        """)

    @guard("function")
    def defaulted_formals(self) -> list[Str]:
        """The formals that have a default, alphabetically.

        A SUBSET of `formal_names`, not the defaults themselves. A
        default is an unevaluated expression in the lambda's own
        environment, so it could only cross as a proxy - and
        `inspect.Parameter` needs to know that a default EXISTS
        rather than what it is, which is the whole use for this.

        `Formal::def` non-null is the fact (nixexpr.hh:467)."""
        Cxx("""
if (!self.get()->isLambda())
    throw std::invalid_argument("value is not a lambda");
std::vector<std::string> names;
auto formals = self.get()->lambda().fun->getFormals();
if (formals.has_value())
    for (const auto & formal : formals->formals)
        if (formal.def != nullptr)
            names.push_back(self.symbol(formal.name));
std::sort(names.begin(), names.end());
return names;
        """)

    @guard("function")
    def primop_name(self) -> Str:
        """This builtin's name.

        Read off `PrimOp` directly rather than through `getDoc`, and
        the difference matters: `getDoc` returns NOTHING for a primop
        with no documentation, because the whole branch is behind `if
        (primOp.doc)` (eval.cc:578). So a doc-less primop has a name
        and an arity that `getDoc` will not tell you."""
        Cxx("""
if (!self.get()->isPrimOp())
    throw std::invalid_argument("value is not a builtin");
return self.get()->primOp()->name;
        """)

    @guard("function")
    def primop_arity(self) -> I64:
        """How many arguments this builtin wants.

        The ONE place an arity is honest, for the reason in the
        comment above this block: a primop declares one and a lambda
        cannot. It is the FULL arity - a primop that already holds
        arguments is `is_primop_app`, which this refuses."""
        Cxx("""
if (!self.get()->isPrimOp())
    throw std::invalid_argument("value is not a builtin");
return static_cast<std::int64_t>(self.get()->primOp()->arity);
        """)

    @guard("function")
    def primop_args(self) -> list[Str]:
        """This builtin's argument names, in declaration order.

        Real order, unlike `formal_names`: these are a vector the
        primop declared rather than a set something searches."""
        Cxx("""
if (!self.get()->isPrimOp())
    throw std::invalid_argument("value is not a builtin");
return self.get()->primOp()->args;
        """)

    @blocks
    def doc(self) -> Doc | None:
        """Documentation for this value, or None.

        `EvalState::getDoc`, which is what the REPL's `:doc` shows -
        and it answers a DIFFERENT shape per value kind rather than
        one thing:

        - a primop with documentation gives its own doc string;
        - a primop WITHOUT gives None, because the branch is behind
          `if (primOp.doc)` (eval.cc:578);
        - a lambda gives PROSE built for the REPL - "Function `name`
          defined at ..." followed by its doc comment, if any
          (eval.cc:585-625);
        - a functor set gives the documentation of `__functor` applied
          to the set, which RUNS that function;
        - anything else, a partially-applied primop included, gives
          None.

        Forces the value first, as `:doc` does.

        BLOCKS, and this is the surprise worth the marker. A lambda's
        doc comment is not stored: `getInnerText` resolves two
        positions and calls `getSnippetUpTo`, which calls
        `Pos::getSource`, which calls `path.readFile()`
        (position.cc:49). So reading a lambda's documentation OPENS
        ITS SOURCE FILE. An accessor that looks cheap and does I/O is
        `tasks/067`'s class of surprise, so it says so.

        A source file that has GONE AWAY raises, and the message
        says so rather than letting upstream's own out-of-range
        escape. `getSource` catches its read error and answers
        nothing, and `getInnerText` then does
        `substr(3, size() - 3 - 2)` on that empty string
        (nixexpr.cc:644-648) - so a doc comment whose file has moved
        throws `std::out_of_range` from inside libexpr. Measured: a
        gate written to expect "" failed with
        `basic_string::substr: __pos (which is 3) > this->size()`.

        Reported rather than swallowed, and that is the choice worth
        stating. None would mean "no documentation" where the truth is
        "cannot read the documentation", and conflating those two is
        this repo's named failure mode - an absence standing in for a
        failure. A caller who does not care can catch it; one who
        gets None cannot un-lose the difference."""
        Cxx("""
huggorm::gc_register_thread();
self.state().forceValue(*self.get(), nix::noPos);
try {
    auto doc = self.state().getDoc(*self.get());
    if (!doc.has_value())
        return std::nullopt;
    return huggorm::Doc{doc->name, static_cast<std::int64_t>(doc->arity), doc->args,
                        doc->doc,
                        doc->pos ? std::optional(huggorm::position_file(doc->pos)) : std::nullopt,
                        doc->pos.line};
} catch (const std::out_of_range &) {
    // Upstream's own bug, surfaced with its cause rather than
    // reported as an empty answer. Narrow on purpose: this is the
    // exact exception measured, and anything else is a real failure
    // that belongs to the caller.
    throw std::runtime_error(
        "cannot read this function's documentation: its source file is "
        "no longer readable, and nix's own doc-comment reader does not "
        "handle that");
}
        """)

    @blocks
    def attr_doc(self, name: Str) -> AttrDoc | None:
        """Where this set defines `name`, and the doc comment there.

        What the REPL's `:doc a.b` shows for an attribute. None when
        the set has no such attribute, or holds one with no position,
        such as one `builtins` made. Forces the set, and reads the
        source file for the comment, as `doc` does."""
        Cxx("""
huggorm::gc_register_thread();
auto & state = self.state();
state.forceAttrs(*self.get(), nix::noPos, "while looking for documentation of a Nix attribute");
const auto * attr = self.get()->attrs()->get(self.intern(name));
if (attr == nullptr || !attr->pos)
    return std::nullopt;
auto pos = state.positions[attr->pos];
std::optional<std::string> comment;
if (auto found = state.getDocCommentForPos(attr->pos))
    comment = found.getInnerText(state.positions);
return huggorm::AttrDoc{huggorm::position_file(pos), pos.line, comment};
        """)

    @blocks
    @needs("nix/expr/attr-path.hh")
    def edit_location(self) -> SourceLocation:
        """The file and line `nix edit` and the REPL's `:edit` open.

        A path or a string names the file itself, at line 0. A lambda
        is where it is defined. Anything else is taken as a
        derivation, and its `meta.position` answers. A location with
        no file on disk raises: no editor can open it."""
        Cxx("""
huggorm::gc_register_thread();
auto & state = self.state();
auto & value = *self.get();
state.forceValue(value, nix::noPos);
std::optional<nix::SourcePath> source;
std::uint32_t line = 0;
if (value.type() == nix::nPath || value.type() == nix::nString) {
    nix::NixStringContext context;
    source = state.coerceToPath(
        nix::noPos, value, context, "while evaluating the filename to edit");
} else if (value.isLambda()) {
    auto pos = state.positions[value.lambda().fun->pos];
    auto path = std::get_if<nix::SourcePath>(&pos.origin);
    if (path == nullptr)
        throw nix::EvalError(state, "'%s' cannot be shown in an editor", pos);
    source = *path;
    line = pos.line;
} else {
    auto [path, found] = nix::findPackageFilename(state, value, "selected value");
    source = std::move(path);
    line = found;
}
auto physical = source->getPhysicalPath();
if (!physical)
    throw nix::EvalError(
        state, "cannot open '%s' in an editor because it has no physical path", *source);
return huggorm::SourceLocation{physical->string(), line};
        """)


@produced(by="Value.doc")
@binding(threading="pool", blocking=False)
@wire_value()
class Doc:
    """A value's documentation, as `EvalState::getDoc` answers it."""

    def name(self) -> Str | None:
        """The function's name. An anonymous lambda's is "".

        Optional because upstream's field is. In 2.34 both `getDoc`
        branches set it, the lambda one to an empty name
        (eval.cc:630), so None does not occur there."""

    def arity(self) -> I64:
        """How many arguments a primop wants. 0 for a lambda."""

    def args(self) -> list[Str]:
        """A primop's argument names. Empty for a lambda."""

    def doc(self) -> Str:
        """The text. For a lambda, prose built for the REPL."""

    def path(self) -> Str | None:
        """The file that defines it, or None for a primop, which has
        no position. A string or stdin is named as Nix names it."""

    def line(self) -> I64:
        """The line in that file, or 0 with no position."""


@produced(by="Value.attr_doc")
@binding(threading="pool", blocking=False)
@wire_value()
class AttrDoc:
    """Where a set defines an attribute, and the comment there."""

    def path(self) -> Str:
        """The file, or Nix's name for a string or stdin."""

    def line(self) -> I64:
        """The line of the definition."""

    def doc(self) -> Str | None:
        """The doc comment before the definition, or None."""


@produced(by="Value.edit_location")
@binding(threading="pool", blocking=False)
@wire_value()
class SourceLocation:
    """A file on disk, and a line in it."""

    def path(self) -> Str:
        """The file."""

    def line(self) -> I64:
        """The line, or 0 for the whole file."""


@header("huggorm_decl/cpp/logging.hpp")
@binding(
    cxx="huggorm::LogField",
    threading="pool",
    blocking=False,
)
@produced(by="LogRecord.fields")
@wire_value()
class LogField:
    """One field of one log record: an integer or a string.

    Nix's own `Logger::Field` is a hand-rolled variant - a two-valued
    enum, a `uint64_t` and a `std::string` - with upstream's FIXME
    asking for a `std::variant` (logging.hh:76). This mirrors it
    rather than picking one of the two, because a field that carried
    only its rendering would lose the difference between the number
    42 and the string "42", and a progress result is made of numbers.
    """

    @reads("is_int")
    def is_int(self) -> Bint:
        """Which of the two `integer` and `text` this field is."""

    @reads("integer")
    def integer(self) -> I64:
        """The number, when `is_int`. Zero otherwise."""

    @reads("text")
    def text(self) -> Str:
        """The string, when not `is_int`. Empty otherwise."""


@header("huggorm_decl/cpp/logging.hpp")
@binding(
    cxx="huggorm::LogRecord",
    threading="pool",
    blocking=False,
)
@produced(by="LogStream.drain")
@wire_value()
class LogRecord:
    """One thing Nix said while it worked.

    NOT a line of text. `nix::Logger` is a tree of ACTIVITIES carrying
    typed progress results, and the progress bar every Nix user sees
    is built from that tree rather than from messages. A binding that
    handed back only strings would throw most of it away.

    The names are Nix's own, from the `internal-json` log format
    (`logging.cc:272`), so a client that already reads that format
    reads this one. The mechanism is different and `tasks/032` says
    why.

    `level`, `type` and `id` are integers because that is what they
    are: `Verbosity`, `ActivityType` and `ResultType` are int-valued
    C++ enums and nothing upstream parses one from a string.
    `decl/words.py` is for vocabularies whose member IS the string
    libstore parses, and these are not that.
    """

    @reads("action")
    def action(self) -> Str:
        """Which of the five kinds this is.

        `"msg"` is a message, and the only kind `level` filters.
        `"start"` and `"stop"` open and close an activity. `"result"`
        reports progress inside one.

        `"finalized"` is the odd one and comes from this binding
        rather than from Nix. It carries only `request`, and it says
        that the call named there raised its last record. Nothing
        else on it means anything.
        """

    @reads("level")
    def level(self) -> I64:
        """`nix::Verbosity`: 0 error, 1 warn, 2 notice, 3 info, 4
        talkative, 5 chatty, 6 debug, 7 vomit.

        A field of the record, not a gate. `Activity::Activity` calls
        `startActivity` with no test (`logging.cc:196`), so a `start`
        arrives whatever its level says."""

    @reads("id")
    def id(self) -> I64:
        """The activity this belongs to, or 0 for a plain message."""

    @reads("parent")
    def parent(self) -> I64:
        """The activity this one runs inside, for a `"start"`.

        Nix tracks it per THREAD - `curActivity` is a `thread_local`
        (`logging.cc:23`) - so the tree is already per-thread before
        this binding sees it."""

    @reads("type")
    def type(self) -> I64:
        """`nix::ActivityType` for a `"start"`, `nix::ResultType` for
        a `"result"`, and 0 otherwise."""

    @reads("request")
    def request(self) -> I64:
        """The call this was raised inside, or 0.

        A reader GROUPS by this and learns the group is closed when a
        `"finalized"` carrying the same number arrives. It cannot name
        a call in advance: the number is allocated per call, by the
        runtime, and no caller is told which one it got.

        ZERO is an answer, not a gap. A fetcher thread, a
        file-transfer thread and a build all raise records on threads
        no wrapped call owns, so nothing about them belongs to a
        call."""

    @reads("text")
    def text(self) -> Str:
        """The message, or the activity's description.

        An error arrives RENDERED, the way `JSONLogger` renders one
        (`logging.cc:283`), and `info` holds its parts."""

    @reads("fields")
    def fields(self) -> list[LogField]:
        """The typed payload, for a `"start"` or a `"result"`.

        What it MEANS depends on `type`. A `resProgress` carries done,
        expected, running and failed; a `resBuildLogLine` carries the
        line. Upstream documents the pairing in `logging.hh` and this
        binding does not restate it."""

    @reads("info")
    def info(self) -> ErrorInfo | None:
        """The parts of an error or a warning `logEI` raised: the
        position, the trace and the suggestions. The same record a
        failed call's `NixError.info` holds.

        None for every other record, and for a message Nix raised as
        text."""


@header("huggorm_decl/cpp/logging.hpp")
@binding(
    cxx="huggorm::LogQueue",
    # A share, because the queue outlives the subscribe call and the
    # tap holds one too: a reader that drops its LogStream must not
    # leave the logger writing into freed memory.
    holder="shared_ptr",
    # NOT affine, and that is the whole point. Every EvalState method
    # runs on that state's own thread, one at a time, so a drain
    # declared there would queue BEHIND the evaluation whose progress
    # it wants to report - and the records would arrive only once the
    # work they describe had finished. A queue with its own mutex is
    # what makes the backchannel a backchannel.
    threading="pool",
    # It takes a mutex and moves a deque. Nothing waits.
    blocking=False,
)
@produced(by="EvalState.subscribe_logs")
class LogStream:
    """The records one subscriber has not read yet.

    BOUNDED, and the bound is the interesting part. A full queue
    refuses a `"msg"` and a `"result"` and never a `"start"` or a
    `"stop"`: a dropped stop leaves a node in the reader's activity
    tree that nothing later closes, and an unclosed node is worse than
    a missing log line. `dropped` counts what the bound refused.
    """

    def drain(self) -> list[LogRecord]:
        """Everything waiting, and the queue is empty afterwards.

        Empty when nothing happened. It does not block and it does not
        wait for a record, because the thread that would wait is the
        one that has to keep draining."""

    def dropped(self) -> I64:
        """How many records the bound refused, over this queue's life.

        CUMULATIVE, so a reader that misses a drain still sees the
        number grow. A reader wanting the per-drain figure
        subtracts."""

    def close(self) -> None:
        """Stop recording and let go of what is waiting.

        The subscription on the state's thread is a separate fact:
        `EvalState.unsubscribe_logs` is what clears that. Closing here
        stops this queue filling; it does not stop the next `drain`
        from being callable."""


@produced(by="LockedFlake.find_input")
@binding(threading="pool", blocking=False)
@wire_value()
class LockedInput:
    """One input of a lock file, as `flake.lock` records it."""

    def locked_ref(self) -> FlakeRef:
        """What the input is pinned to."""

    def original_ref(self) -> FlakeRef:
        """What `flake.nix` asked for."""

    def is_flake(self) -> Bint:
        """False for an input with `flake = false`."""


@produced(by="EvalState.lock_flake")
@header("huggorm_decl/cpp/eval.hpp")
@binding(
    # Locked for one state, and handed back to that state to call. The
    # async layer keeps it on the state's runner, as it keeps a Value.
    threading="affine",
    blocking=False,
    cxx="huggorm::LockedFlake",
)
class LockedFlake:
    """A flake with its lock file resolved, for the state that locked it."""

    def description(self) -> Str | None:
        """The `description` in `flake.nix`, or None without one."""
        Cxx("return self.locked.flake.description;")

    def find_input(self, path: list[Str]) -> LockedInput | None:
        """The input at `path`, `["nixpkgs"]` or `["a", "b"]`, found as
        Nix finds it: through `follows`, raising on a cycle.

        None when the path names nothing, and for the root, which
        records no locked reference."""
        Cxx("""
auto node = std::dynamic_pointer_cast<const nix::flake::LockedNode>(
    self.locked.lockFile.findInput(nix::flake::InputAttrPath(path.begin(), path.end())));
if (!node)
    return std::nullopt;
return huggorm::LockedInput{node->lockedRef, node->originalRef, node->isFlake};
        """)

    # Nix 2.35 drops the cache of the whole accessor, not of a path.
    if NIX_2_35:
        @blocks
        def write_lock_file(self) -> None:
            """Write `flake.lock` beside `flake.nix`, as `nix flake
            lock` does, whatever `lock_flake` was told."""
            Cxx("""
auto & flake = self.locked.flake;
auto [text, keys] = self.locked.lockFile.to_string();
auto & subdir = flake.originalRef.subdir;
auto relative = (subdir.empty() ? "" : subdir + "/") + "flake.lock";
flake.originalRef.input.putFile(nix::CanonPath(relative), text + "\\n", std::nullopt);
flake.lockFilePath().accessor->invalidateCache();
            """)
    else:
        @blocks
        def write_lock_file(self) -> None:
            """Write `flake.lock` beside `flake.nix`, as `nix flake
            lock` does, whatever `lock_flake` was told."""
            Cxx("""
auto & flake = self.locked.flake;
auto [text, keys] = self.locked.lockFile.to_string();
auto & subdir = flake.originalRef.subdir;
auto relative = (subdir.empty() ? "" : subdir + "/") + "flake.lock";
flake.originalRef.input.putFile(nix::CanonPath(relative), text + "\\n", std::nullopt);
flake.lockFilePath().invalidateCache();
            """)


@produced(by="Repl.select")
@header("huggorm_decl/cpp/eval.hpp")
@binding(threading="affine", blocking=False, cxx="huggorm::ReplSelection")
class ReplSelection:
    """`a.b.c` split before its last select, as `nix repl` splits it
    to complete a name and to find the documentation of one."""

    @reads("name")
    def name(self) -> Str:
        """The last attribute name, `c`. Evaluated when it is `${...}`."""

    @reads("attrs")
    def attrs(self) -> Value:
        """`a.b`, evaluated, and not checked to be a set."""


@produced(by="EvalState.repl")
@header("huggorm_decl/cpp/eval.hpp")
@binding(
    # The environment belongs to one state, and its thunks evaluate
    # there. The async layer keeps it on the state's runner.
    threading="affine",
    blocking=True,
    cxx="huggorm::Repl",
)
class Repl:
    """One `nix repl` scope: a binding made here is visible to every
    later expression evaluated here, and to nothing else.

    It holds 32768 bindings, as `nix repl` does. A rebinding takes a
    new slot too, because an earlier thunk still refers to the old
    one.

    `nix repl` refuses a set of attributes that exactly fills the free
    slots, which is one slot short of the allocation. This takes it."""

    def process_line(self, line: Str, base: Str | None = None) -> Value | None:
        """One line, as `nix repl` reads a line that is not a command.

        Bindings such as `x = 1` or `inherit (a) b` are added lazily,
        and answer None. Any other line is an expression, and answers
        its value, forced. `base` is as `eval_expr` takes it."""
        Cxx("""
auto & state = self.state();
auto base_path = state.rootPath(std::string_view(base ? *base : "."));
nix::ExprAttrs * bindings = nullptr;
try {
    bindings = state.parseReplBindings(line, base_path, self.static_env);
} catch (nix::ParseError &) {
    try {
        bindings = state.parseReplBindings(line + ";", line, base_path, self.static_env);
    } catch (nix::ParseError &) {
    }
}
nix::Env * env = *self.env;
if (!bindings) {
    auto * made = state.allocValue();
    state.parseExprFromString(line, base_path, self.static_env)->eval(state, *env, *made);
    state.forceValue(*made, made->determinePos(nix::noPos));
    return self.wrap(made);
}
nix::Env * inherit_env = bindings->inheritFromExprs
    ? bindings->buildInheritFromEnv(state, *env) : nullptr;
for (auto & [symbol, def] : *bindings->attrs) {
    if (self.displ >= huggorm::Repl::env_size)
        throw nix::Error("environment full; cannot add more variables");
    auto * made = state.allocValue();
    made->mkThunk(def.chooseByKind(env, env, inherit_env), def.e);
    if (auto old = self.static_env->find(symbol); old != self.static_env->vars.end())
        self.static_env->vars.erase(old);
    self.static_env->vars.emplace_back(symbol, self.displ);
    self.static_env->sort();
    env->values[self.displ++] = made;
}
return std::nullopt;
        """)

    def eval_expr(self, expr: Str, base: Str | None = None) -> Value:
        """`EvalState.eval_expr`, in this scope."""
        Cxx("""
auto & state = self.state();
auto * made = state.allocValue();
state.parseExprFromString(
    expr, state.rootPath(std::string_view(base ? *base : ".")), self.static_env)
    ->eval(state, **self.env, *made);
state.forceValue(*made, made->determinePos(nix::noPos));
return self.wrap(made);
        """)

    @needs("nix/cmd/common-eval-args.hh")
    def eval_file(self, path: Str) -> Value:
        """`EvalState.eval_file`, in this scope: the file sees the
        bindings, as an expression does. Not cached, because the
        answer depends on the scope."""
        Cxx("""
auto & state = self.state();
auto * made = state.allocValue();
state.parseExprFromFile(nix::resolveExprPath(nix::lookupFileArg(state, path)), self.static_env)
    ->eval(state, **self.env, *made);
state.forceValue(*made, made->determinePos(nix::noPos));
return self.wrap(made);
        """)

    @needs("nix/cmd/common-eval-args.hh")
    def load_file(self, path: Str) -> Value:
        """What `:load` adds: the file, called with no arguments when
        it is a function. Nothing is added; `add_attrs` adds it."""
        Cxx("""
auto & state = self.state();
nix::Value loaded;
state.evalFile(nix::lookupFileArg(state, path), loaded);
auto * made = state.allocValue();
state.autoCallFunction(*state.buildBindings(0).finish(), loaded, *made);
return self.wrap(made);
        """)

    def add_attrs(self, attrs: Value) -> list[Str]:
        """Bind every attribute of a set, as `:load` does, and answer
        the names added."""
        Cxx("""
auto & state = self.state();
auto & value = *attrs.get();
state.forceAttrs(value, [&]() { return value.determinePos(nix::noPos); },
                 "while evaluating an attribute set to be merged in the global scope");
if (self.displ + value.attrs()->size() > huggorm::Repl::env_size)
    throw nix::Error("environment full; cannot add more variables");
std::vector<std::string> names;
names.reserve(value.attrs()->size());
nix::Env * env = *self.env;
for (auto & attr : *value.attrs()) {
    self.static_env->vars.emplace_back(attr.name, self.displ);
    env->values[self.displ++] = attr.value;
    names.emplace_back(state.symbols[attr.name]);
}
self.static_env->sort();
self.static_env->deduplicate();
return names;
        """)

    def names(self) -> list[Str]:
        """Every name an expression here can see, sorted: the bindings
        and the base scope, `builtins` and `true` among them."""
        Cxx("""
auto & state = self.state();
std::set<std::string> seen;
for (std::shared_ptr<const nix::StaticEnv> scope = self.static_env; scope; scope = scope->up)
    for (auto & [symbol, displ] : scope->vars)
        seen.emplace(state.symbols[symbol]);
return std::vector<std::string>(seen.begin(), seen.end());
        """)

    def select(self, expr: Str, base: Str | None = None) -> ReplSelection | None:
        """`a.b.c` split before its last select, or None when `expr`
        is not a select."""
        Cxx("""
auto & state = self.state();
auto * parsed = state.parseExprFromString(
    expr, state.rootPath(std::string_view(base ? *base : ".")), self.static_env);
auto * select = dynamic_cast<nix::ExprSelect *>(parsed);
if (!select)
    return std::nullopt;
auto * attrs = state.allocValue();
auto name = select->evalExceptFinalSelect(state, **self.env, *attrs);
return huggorm::ReplSelection{std::string(state.symbols[name]), self.wrap(attrs)};
        """)


@header("huggorm_decl/cpp/eval.hpp")
# A state HOLDS Python callables - `register_primop` gives it one -
# and the natural way to write one closes over the state itself,
# because the result comes from `state.make_int`. That is a cycle
# Python's collector cannot see through, so the class says how to
# traverse it (`tasks/093`).
@gc_slots("huggorm::evaluator_slots")
@binding(
    cxx="huggorm::Evaluator",
    # Not thread-safe, one per thread. libexpr says so and this is
    # where that sentence becomes a policy.
    threading="affine",
    # Parsing and evaluating are slow and pure C++ after the string
    # crosses. The accessors are not, and say so.
    blocking=True,
)
class EvalState:
    """One evaluator, and the thread it belongs to.

    ONE STATE, ONE THREAD, and that is a decision rather than a
    consequence. A state is the most granular parallelism
    `nix::EvalState` offers, so it is the unit this project isolates
    on - and it isolates completely: a value belonging to one state
    is not valid in another.

    The reason is not a rule somebody chose. A `nix::Value` is not
    self-describing - an attribute name is a `Symbol`, an index into
    the producing state's own table - so handing one to a second
    state reads whatever that state's table holds at the same index.
    A wrong answer, not a failure.

    The exception is DATA. A value forced and read out is a Python
    object, and a wire value crosses as a copy; neither carries a tie
    to the state that made it.

    WHERE the rule is enforced is the other half. The async layer is
    the lowest one that manages threads for a caller, so it is where
    a library user meets it: an `AffineRunner` gives every state its
    own thread, and a foreign argument is refused before the call
    hops. The rpc goes through those same wrappers, so a remote
    caller meets it too.

    A caller holding THIS binding directly is on their own. Nothing
    below the async layer checks, and nothing should: the C++ here is
    exactly as permissive as libexpr, which has no such rule of its
    own, and goal 1 asks a binding not to be MORE permissive than
    what it binds. Carl decided this; `tasks/085` records it.
    """

    def __init__(self, store: Store,
                 settings: dict[str, Str] | None = None,
                 build_store: Store | None = None) -> None:
        """Open a state against a store.

        REQUIRED, with no default. A state is bound to a store and a
        thread, and neither is a thing to guess at.

        The state SHARES `store`: it does not open a second one from
        the store's URI. `dummy://` opened twice is two empty stores,
        so a path added through `store` would be missing to the state.

        `nix::EvalState` takes a `ref<Store>` and two settings objects
        that must outlive it, so `huggorm::Evaluator` owns all four
        and these parameters are the ones a caller can answer.

        `settings` are this state's own evaluator and fetcher
        settings, spelled as in nix.conf, applied over what the
        process has (`set_setting`, nix.conf). A name that neither
        object holds raises `UsageError`, store settings included:
        the state has no store settings of its own.

        `build_store` is a second store to BUILD in, as `nix
        --eval-store A --store B` splits them: evaluation writes
        `.drv` files to the first, and a realise builds in the second
        and copies the outputs back. None builds where it evaluates."""

    def set_setting(self, name: Str, value: Str) -> None:
        """Change one of this state's own settings, as the constructor's
        `settings` spell them.

        The evaluator's settings first, then the fetcher's, as the
        constructor tries them. A name neither holds raises
        `UsageError`.

        A live state reads most names at the point of use, such as
        `max-call-depth` and `allow-dirty`. It reads a few only in its
        constructor, such as `pure-eval`: this changes the object and
        not the state, so pass those to the constructor."""
        Cxx("""
if (!self.eval_settings().set(name, value) && !self.fetch_settings().set(name, value))
    throw nix::UsageError("'%s' is not an evaluator or fetcher setting", name);
        """)

    def get_store_uri(self) -> Str:
        """How this state's store describes itself, as `Store.get_uri`
        does. For logging only: it does not round-trip."""
        Cxx("return self.store().config.getHumanReadableURI();")

    def parse_expr(self, expr: Str, base: Str | None = None) -> Value:
        """Parse without evaluating: the result is an unforced thunk.

        `base` is the directory a relative path such as `./foo` names
        from, as `eval_expr` takes it.

        Not a `@produces`: that marker is for a value made by ONE
        initialiser, and this parses first and then builds a thunk
        over the state's base environment. Stretching the marker to
        cover it would make it a description of structure."""
        Cxx("""
if (expr.empty())
    throw std::invalid_argument("empty expression");
auto * e = self.state().parseExprFromString(
    expr, self.state().rootPath(std::string_view(base ? *base : ".")));
auto * made = self.alloc();
made->mkThunk(&self.state().baseEnv, e);
return self.wrap(made);
        """)

    def eval_expr(self, expr: Str, base: Str | None = None) -> Value:
        """Parse and evaluate: slow, fully forced result.

        `base` is the directory a relative path such as `./foo` names
        from. Without it, that is this process's working directory, as
        `nix eval --expr` has it. A remote caller wants its own: the
        server's working directory is not the client's."""
        Cxx("""
if (expr.empty())
    throw std::invalid_argument("empty expression");
auto * e = self.state().parseExprFromString(
    expr, self.state().rootPath(std::string_view(base ? *base : ".")));
auto * made = self.alloc();
self.state().eval(e, *made);
self.state().forceValue(*made, nix::noPos);
return self.wrap(made);
        """)

    @needs("nix/cmd/common-eval-args.hh")
    def eval_file(self, path: Str) -> Value:
        """Evaluate a file, and remember it.

        The one call that is CHEAP the second time. `evalFile` keeps a
        `fileEvalCache` keyed by resolved path, and a hit forces the
        value it already has and copies it - no open, no parse, no
        evaluation (`eval.cc:1118`). `eval_expr` has no such cache: a
        string is not a key, so the same text evaluated twice is
        evaluated twice.

        That cache is what "warm" means for this project, and it is
        why the state has to outlive the client that filled it.

        A `str`, not a `pathlib.Path`, and the reason is
        `add_path_to_store`'s: the file is read on the machine the
        EVALUATOR runs on. In process that is here; over RPC it is the
        server's filesystem, and no client path crosses - the argument
        goes as the string it is.

        `path` is what `nix eval --file` takes, read by libcmd's
        `lookupFileArg`: `<nixpkgs>` from the lookup path, `flake:x`,
        a tarball URL, or a path. A relative path resolves against the
        process's own directory.

        Forced to WHNF, like every other `evalFile` caller: upstream
        forces the thunk before it hands the value back. Not deeply -
        an attribute set comes back with its members unforced, which
        is what makes the call cheap enough to be worth caching."""
        Cxx("""
if (path.empty())
    throw std::invalid_argument("empty path");
auto * made = self.alloc();
self.state().evalFile(nix::lookupFileArg(self.state(), path), *made);
return self.wrap(made);
        """)

    def cached_files(self) -> list[Str]:
        """Every file whose evaluation this state has cached.

        What a watcher watches. `tasks/016` wants a change to a file an
        evaluation read to invalidate the warm state rather than throw
        it away, and this is the set that change would touch.

        libexpr keeps it and does not offer it: `fileEvalCache` is
        private, and the accessor every read goes through is built
        inside the constructor from the settings alone, so there is
        nothing to substitute either. `huggorm::cached_files` reaches
        it the one way the standard allows without patching nixpkgs -
        the whole argument is in `cpp/eval.hpp`, beside the code.

        `import` goes through `evalFile`, so a file reached from
        INSIDE an expression is here as surely as the one the caller
        named. That is the reason this reads libexpr's cache rather
        than counting what `eval_file` was handed: our own boundary
        sees one file and an evaluation reads many.

        Resolved paths, so `/foo` appears as `/foo/default.nix`. That
        is what the cache is keyed by and what a watch has to name.

        NOT every file read. `builtins.readFile` and `builtins.path`
        do not go through this cache, so a caller who wants those
        watched needs something else - and would find out here rather
        than from a stale answer.

        NOT every entry is a file either. libexpr evaluates its own
        `derivation-internal.nix` out of an in-memory accessor, and it
        renders as `«nix-internal»/derivation-internal.nix`. A watcher
        has to skip what it cannot stat; the list is what the cache
        holds, not a promise that each entry is on disk."""
        Cxx("return huggorm::cached_files(self.state());")

    def forget_file(self, path: Str) -> None:
        """Forget one cached file, so the next evaluation reads disk.

        The other half of `cached_files`. A watcher that sees a file
        change needs to drop that file's warm evaluation and KEEP the
        rest - `resetFileCache()` is the only public way to do it, and
        it also clears the fetched flake inputs, so one edited local
        file costs a re-download.

        FORGET THE CLOSURE, NOT THE FILE. Measured, and written up in
        `tasks/016`: an importer does not notice its import changing.
        `outer.nix` that says `import ./inner.nix` stays cached at its
        old answer after `inner.nix` is edited and forgotten, because
        the cache holds no edge between the two - both files are
        listed, and nothing says one read the other.

        The DIFF is not that closure, and this docstring said it was.
        `cached_files` before and after one `eval_file` differ by what
        the evaluation newly CACHED, not by what it read - so the
        second root to import a shared file gets a diff that does not
        mention it, and forgetting that diff leaves the root stale.
        Measured in `tasks/083`.

        What is sound is the SNAPSHOT: everything cached when a root
        finished is a superset of what that root read. `huggorm.Watcher`
        keeps one per root and does this bookkeeping, so a caller who
        wants live reloading should use it rather than pair this call
        with a diff of their own.

        Erases both spellings. The cache is keyed by the RESOLVED
        path, so forgetting `/foo` erases `/foo/default.nix` too - the
        argument is in `cpp/eval.hpp`, beside the code.

        A path that was never cached forgets nothing, which is what
        makes feeding a whole closure in safe."""
        Cxx("""
if (path.empty())
    throw std::invalid_argument("empty path");
huggorm::forget_file(self.state(), self.state().rootPath(path));
        """)

    @instant
    def register_primop(self, name: Str, arity: I64,
                        fn: PyFunc) -> None:
        """Publish a Python callable as `builtins.<name>`.

        The direction every other method here runs the other way.
        Everything else is Python calling the evaluator; this hands
        the evaluator a function it calls back, in the middle of an
        evaluation, on this state's own thread.

        `fn` takes `arity` Values and returns a Value. Its arguments
        arrive FORCED - a primop receives thunks, and a Python
        function given an unforced one could do nothing with it and
        had no way to say so.

        ARITY 0 IS A LAZY CONSTANT, as Nix makes it. `addPrimOp`
        registers the primop with arity 1 and binds the name to its
        application to itself (eval.cc:523). So `fn` takes no
        arguments, and runs when an evaluation first reads the name,
        not when this call registers it.

        SYNCHRONOUS, and there is no way to make it otherwise. It runs
        inside evaluation, so it must not await and must not hop
        threads. That is the exact inverse of every wrapper this repo
        emits, which exist to get OFF the calling thread - and it is
        why `tasks/034`, a Nix function called FROM Python, cannot
        borrow this shape.

        IN-PROCESS ONLY. No rpc surface exists and the manifest
        refuses to build one, because a remote registration would make
        the evaluator call back over the socket once per invocation,
        on its evaluation thread. A decision, in `tasks/033`, rather
        than something not written yet.

        PERMANENT for this STATE's life, and not for the process's.
        Upstream stores a primop with `new PrimOp(...)` in GC memory
        and the collector runs no destructors, so a registration lasts
        as long as the state that took it. There is no unregister to
        add later; upstream has nowhere to put one.

        CLOSING OVER THE STATE IS FINE, and this paragraph said the
        opposite until `tasks/093`. It said a callable capturing the
        state pinned it forever, and told a caller to capture what the
        callable needs instead - which is not advice anybody can take,
        because a primop builds its result with `state.make_int`.

            state.register_primop(
                "f", 1, lambda v: state.make_int(1))   # fine now

        It was true when written, and measured: two leaked instances
        at shutdown. The cycle ran through nix's own memory - the
        state reached the callable through the base env, the callable
        reached the state through its closure cell - and Python's
        collector cannot walk the first arm. The C++ side's weak
        reference broke the wrong arm.

        The class now carries `@gc_slots`, so the collector is told how
        to see it. Zero leaked instances, and a gate holds it.

        The name is not sanitised. Upstream treats a `__` prefix
        specially - it strips it for the `builtins` attribute and
        keeps it in the base environment - and this passes the name
        through so a caller gets Nix's own behaviour rather than
        ours.

        A base environment holds a fixed number of names, and this
        build patches that number from 128 to 512
        (`nix/patches/nix-base-env-size.patch`). Registering past it
        raises rather than corrupting the heap, which stock Nix does
        not: `addPrimOp` tests no bound at all."""
        Cxx("""
if (name.empty())
    throw std::invalid_argument("empty name");
if (arity < 0)
    throw std::invalid_argument("arity must not be negative");
self.register_primop(name, static_cast<std::size_t>(arity), fn);
        """)

    @instant
    def make_primop(self, name: Str, arity: I64, fn: PyFunc) -> Value:
        """A Python callable as a Nix function value, which
        `builtins` does not hold.

        A primop answers a function in its result this way. `fn`
        takes `arity` Values and returns a Value, as for
        `register_primop`, and its arguments arrive forced. `name` is
        what `primop_name` and an error from `fn` show.

        `arity` must be at least 1. Nix has no function of no
        arguments; `register_primop` makes a lazy constant for 0, and
        a caller with a value in hand has no use for one.

        The callable lives as long as this state, as a registered one
        does. So each call keeps one more callable, and a primop that
        makes a function on every call grows the state."""
        Cxx("""
if (arity < 1)
    throw std::invalid_argument("arity must be at least 1");
return self.make_primop(name, static_cast<std::size_t>(arity), fn);
        """)

    def subscribe_logs(self, capacity: I64 = 1024,
                       level: I64 = 3) -> LogStream:
        """Record what Nix says on THIS state's thread.

        The direction `register_primop` runs, without a call: nix
        tells Python what it is doing, in the middle of work Python
        asked for.

        The subscription belongs to the THREAD, because an `EvalState`
        is affine and this Nix evaluates on one thread - nothing under
        `src/libexpr` names `eval-cores`, so 2.34.8 has no parallel
        evaluation. So the thread that owns a state is the thread its
        records are raised on, and the queue this returns holds that
        state's records and no other state's.

        `level` SETS this thread's verbosity, so it can widen as well
        as narrow, UP TO THE CEILING. `nix::verbosity` is pinned at
        import at `HUGGORM_LOG_CEILING` (CHATTY when unset), and nix
        produces nothing above it, so a subscription at `6` gets no
        debug records under the default (`tasks/102`). The level is
        per THREAD, so what a caller KEEPS costs no other caller
        anything.

        A daemon opened while this is live narrates at `level`, even
        above the ceiling: its lines arrive as errors.

        The global then goes back DOWN when this subscription ends -
        to what the remaining subscriptions still need, never past
        them. `tasks/095` measured what leaving it up cost: the
        daemon narrating on an unsubscribed caller's stderr, for the
        life of the process.

        It does not say "pinned wide open", which an earlier draft of
        this docstring did. There has been no pin since `tasks/089`
        step 4 removed it, and a pin is exactly what costs the daemon.

        What it still does NOT see is a record raised on a fetcher or
        a file-transfer thread, because that thread subscribed to
        none. A process-wide subscriber is what covers those, and the
        rpc is what needs one.

        REPLACES any subscription this thread had, and closes it. Two
        live subscriptions on one thread would each get an arbitrary
        half of the records, which is worse than either getting none.
        """
        Cxx("""
if (capacity < 1)
    throw std::invalid_argument("capacity must be at least 1");
if (level < 0)
    throw std::invalid_argument("level must not be negative");
(void) self;
return huggorm::subscribe_logs(static_cast<std::size_t>(capacity),
                               static_cast<std::uint64_t>(level));
        """)

    def unsubscribe_logs(self) -> None:
        """Stop recording on this state's thread.

        A queue already handed out still drains what it holds. This
        says only that nothing more goes into it.

        It also gives back the verbosity this thread asked for. The
        level a new daemon connection is told drops to the widest
        level any subscription that is still live needs - never below one, because that
        would drop a still-subscribed thread's records with nothing
        said. A thread that exits without calling this gives its
        level back anyway (`tasks/096`).

        It CROSSES the wire and `subscribe_logs` does not, which looks
        like an accident and is not. `subscribe_logs` answers a
        `LogStream`, and a LogStream is a proxy with no service - so
        the answer would be a handle no later call could use, and the
        schema refuses it for that reason. This answers nothing, so
        the reason does not apply.

        What a remote caller can do with it is stop a subscription
        somebody else made on that state's thread. That is the same
        power every shared handle already grants - a second connection
        holding an EvalState can `forget_file` on it too - so it is
        within the sharing model rather than a new hole in it."""
        Cxx("""
(void) self;
huggorm::unsubscribe_logs();
        """)

    def force(self, v: Value) -> None:
        """Force a value in place. Idempotent.

        Mutates GC-resident memory, and the async layer routes the
        call to this state's OWN thread - which is why a Value is
        affine and travels as a proxy."""
        Cxx("return self.state().forceValue(*v.get(), nix::noPos);")

    # Builders. A caller builds a list or an attribute set one element
    # at a time, and that shape is the WIRE's, not libexpr's: a Nix
    # collection is immutable and sized when it is built, so each call
    # here rebuilds it.
    #
    # The alternative is worse. `make_list(items: list[Value])` would
    # need a container of PROXIES to cross, and one lease per element
    # is not something anything grants in bulk - so it would have no
    # RPC surface at all. One element per call is what crosses.

    @produces("mkInt")
    def make_int(self, value: I64) -> Value:
        """A forced integer value."""

    @produces("mkFloat")
    def make_float(self, value: F64) -> Value:
        """A forced float value."""

    def make_string(self, value: Str,
                    context: list[Str] | None = None,
                    ) -> Value:
        """A forced string value, carrying `context`: elements as
        `string_context` names them.

        Not a `@produces`: `mkString` takes the state's allocator as a
        second argument, so the call is not "the initialiser with the
        declared arguments"."""
        Cxx("""
nix::NixStringContext parsed;
for (auto & element : context)
    parsed.insert(nix::NixStringContextElem::parse(element));
auto * made = self.alloc();
made->mkString(value, parsed, self.state().mem);
return self.wrap(made);
        """)

    @produces("mkBool")
    def make_bool(self, value: Bint) -> Value:
        """A forced boolean value."""

    @produces("mkNull")
    def make_null(self) -> Value:
        """The null value."""

    def make_list(self) -> Value:
        """An empty list. Fill it with `list_append`."""
        Cxx("""
auto * made = self.alloc();
made->mkList(self.state().buildList(0));
return self.wrap_builder(made);
        """)

    @fills("make_list", "list")
    def list_append(self, target: Value, item: Value) -> None:
        """Add one element to a list, in place."""
        Cxx("return target.stage(item.get());")

    def make_attrs(self) -> Value:
        """An empty attribute set. Fill it with `attrs_set`."""
        Cxx("""
auto * made = self.alloc();
auto builder = self.state().buildBindings(0);
made->mkAttrs(builder);
return self.wrap_builder(made);
        """)

    @fills("make_attrs", "attrs")
    def attrs_set(self, target: Value, name: Str, item: Value) -> None:
        """Set one attribute, in place.

        Setting a name twice replaces its value, matching an attribute
        set built by assignment."""
        Cxx("return target.stage_attr(name, item.get());")

    @needs("huggorm_decl/cpp/call_settings.hpp", "nix/flake/settings.hh")
    def lock_flake(self, ref: FlakeRef, recreate: Bint = False,
                   update: list[Str] | None = None,
                   write_lock_file: Bint = True,
                   settings: dict[str, Str] | None = None) -> LockedFlake:
        """Lock a flake, as `nix flake lock` does.

        `recreate` drops the old lock file; `update` names the inputs to
        refresh, `nixpkgs` or `a/b`. `settings` are flake settings over
        the process's, such as `accept-flake-config`.

        THIS FETCHES what is not locked yet, into this state's store."""
        Cxx("""
auto flake_settings = huggorm::call_settings<nix::flake::Settings>(
    settings.value_or(std::map<std::string, std::string>{}));
nix::flake::LockFlags flags;
flags.recreateLockFile = recreate;
flags.writeLockFile = write_lock_file;
for (auto & input : update) {
    auto path = nix::flake::NonEmptyInputAttrPath::parse(input);
    if (!path)
        throw nix::UsageError("an input path must not be empty: '%s'", input);
    flags.inputUpdates.insert(*path);
}
return self.keep(nix::flake::lockFlake(*flake_settings, self.state(), ref, flags));
        """)

    def repl(self) -> Repl:
        """A new, empty REPL scope over this state's base scope."""
        Cxx("return self.repl();")

    def call_flake(self, locked: LockedFlake) -> Value:
        """The flake's outputs, as `builtins.getFlake` gives them:
        unforced, so no output is evaluated until it is read."""
        Cxx("""
auto * made = self.alloc();
nix::flake::callFlake(self.state(), locked.locked, *made);
return self.wrap(made);
        """)

    def get_flake(self, ref: FlakeRef, use_registries: Bint = True) -> FlakeRef:
        """What `ref` resolves to through the registries, fetched.

        THIS FETCHES the flake, into this state's store."""
        Cxx("""
auto flake = nix::flake::getFlake(
    self.state(), ref,
    use_registries ? nix::fetchers::UseRegistries::All : nix::fetchers::UseRegistries::No);
return flake.resolvedRef;
        """)

    @needs("nlohmann/json.hpp", "nix/fetchers/attrs.hh")
    def flake_metadata_json(self, locked: LockedFlake) -> Str:
        """The object `nix flake metadata --json` prints.

        `CmdFlakeMetadata::run`, line for line. The store is the build
        store, as Nix uses the store of the command."""
        Cxx("""
auto & lockedFlake = locked.locked;
auto & flake = lockedFlake.flake;
auto & store = *self.state().buildStore;
nlohmann::json j;
if (flake.description)
    j["description"] = *flake.description;
j["originalUrl"] = flake.originalRef.to_string();
j["original"] = nix::fetchers::attrsToJSON(flake.originalRef.toAttrs());
j["resolvedUrl"] = flake.resolvedRef.to_string();
j["resolved"] = nix::fetchers::attrsToJSON(flake.resolvedRef.toAttrs());
j["url"] = flake.lockedRef.to_string();
j["locked"] = nix::fetchers::attrsToJSON(flake.lockedRef.toAttrs());
if (auto rev = flake.lockedRef.input.getRev())
    j["revision"] = rev->to_string(nix::HashFormat::Base16, false);
if (auto dirty = nix::fetchers::maybeGetStrAttr(flake.lockedRef.toAttrs(), "dirtyRev"))
    j["dirtyRevision"] = *dirty;
if (auto count = flake.lockedRef.input.getRevCount())
    j["revCount"] = *count;
if (auto modified = flake.lockedRef.input.getLastModified())
    j["lastModified"] = *modified;
j["path"] = store.printStorePath(store.toStorePath(flake.path.path.abs()).first);
j["locks"] = lockedFlake.lockFile.toJSON().first;
if (auto fingerprint = lockedFlake.getFingerprint(store, self.state().fetchSettings))
    j["fingerprint"] = fingerprint->to_string(nix::HashFormat::Base16, false);
return j.dump();
        """)


# --- free functions ------------------------------------------------


@needs("huggorm_decl/cpp/gc.hpp")
@threading("pool")
def gc_stats() -> dict[str, I64]:
    """Live collector counters, bound straight from gc.h.

    These prove the collector is ACTIVE: a no-op integration cannot
    fake them."""
    Cxx("""
return {
    {"heap_size", static_cast<std::int64_t>(GC_get_heap_size())},
    {"total_bytes", static_cast<std::int64_t>(GC_get_total_bytes())},
    {"bytes_since_gc", static_cast<std::int64_t>(GC_get_bytes_since_gc())},
    {"collections", static_cast<std::int64_t>(GC_get_gc_no())},
    {"used_bytes",
     static_cast<std::int64_t>(GC_get_heap_size() - GC_get_free_bytes())},
    // Uncollectable blocks, and every root is one: the number moves with
    // the live roots, in bytes rather than in count.
    {"non_gc_bytes", static_cast<std::int64_t>(GC_get_non_gc_bytes())},
    // OURS, not the collector's: how many roots this process holds. A
    // root that is never dropped keeps its value alive forever, and no
    // heap counter can tell that from a heap that simply grew.
    {"live_roots", huggorm::live_roots().load()},
    // OURS too: REPL environments freed. A held scope must never
    // count here, and nothing else can tell.
    {"scopes_collected", huggorm::scopes_collected().load()},
};
    """)


@needs("huggorm_decl/cpp/gc.hpp")
@threading("pool")
@blocks
@binds("huggorm::gc_collect")
def collect_garbage() -> None:
    """Run a full stop-the-world collection (twice).

    Global process state, mirroring libgc: not a method on EvalState.
    Blocking - dispatch it to a thread from async code.

    It registers the calling thread first. Boehm stops the world by
    signalling every registered thread, and a thread it does not know
    cannot answer - the collection aborts the process with "Collecting
    from unknown thread"."""


@needs("huggorm_decl/cpp/gc.hpp")
def collector_owner_thread() -> I64:
    """The kernel thread id of the thread that initialised the collector,
    or 0 where the platform has none.

    Boehm scans that thread's stack up to the process's main-stack base
    for as long as the process lives, so it must not exit. It is the
    thread that imported huggorm."""
    Cxx("""
return huggorm::gc_owner().load(std::memory_order_acquire);
    """)


@needs("huggorm_decl/cpp/gc.hpp")
@binds("huggorm::gc_start")
def start_collector() -> None:
    """Start the collector's threads now, rather than at the first
    evaluator.

    Once per process; a later call does nothing. It is also where Nix
    copies `NIX_PATH` into `nix-path`, so a caller that reads its
    settings before it makes an evaluator calls this first."""


@needs("huggorm_decl/cpp/gc.hpp")
@binds("huggorm::gc_unregister_thread")
def gc_release_thread() -> None:
    """Take the CURRENT thread off the collector's list.

    Runtime plumbing, not domain surface: it carries no threading
    policy, so the codegen leaves it alone and it has no async or RPC
    form. The absence is the declaration.

    A thread that registered must call this as its last GC action
    before it exits. Boehm stops the world by signalling every
    registered thread and waiting for each to answer. A thread that
    exits while still registered never answers, and the next
    collection aborts the process. That is what a dedicated affine
    executor does when its wrapper is closed."""


@needs("huggorm_decl/cpp/logging.hpp")
def process_verbosity() -> I64:
    """What nix will PRODUCE, process-wide, right now.

    `nix::verbosity`, read: the ceiling pinned at import from
    `HUGGORM_LOG_CEILING`, CHATTY when unset. Nothing moves it after
    that, because `printMsg` reads it on every thread and a later
    write races. Not what any subscriber KEEPS - that is per thread
    and `subscribe_logs` sets it."""
    Cxx("""
return static_cast<std::int64_t>(nix::verbosity);
    """)


@needs("huggorm_decl/cpp/logging.hpp")
def daemon_verbosity() -> I64:
    """The level `RemoteStore::setOptions` tells a new daemon
    connection: the widest level a live subscription asks for, and
    `lvlInfo` when none does.

    A connection already open keeps what its handshake sent."""
    Cxx("""
return static_cast<std::int64_t>(nix::remoteVerbosity.load(std::memory_order_relaxed));
    """)


@needs("huggorm_decl/cpp/logging.hpp")
def thread_verbosity() -> I64:
    """The level this thread keeps records at.

    Its own level, when `set_thread_verbosity` or `subscribe_logs` gave
    it one. The process default otherwise, read live, so a thread with
    no level follows `set_default_verbosity`.

    No `@threading`, for `begin_request`'s reason: the answer is a fact
    about the calling thread, and a hop to the pool would read a pool
    thread."""
    Cxx("""
return static_cast<std::int64_t>(huggorm::effective_verbosity());
    """)


@needs("huggorm_decl/cpp/logging.hpp")
def set_thread_verbosity(level: I64) -> None:
    """Give this thread a level of its own, with no queue.

    The level half of `subscribe_logs`, for a caller that reads every
    record through the process queue and still wants a level per call.
    It raises the level a new daemon connection is told, and
    `clear_thread_verbosity` gives that back.

    `subscribe_logs` replaces this level, and `unsubscribe_logs`
    clears it: one thread holds one level."""
    Cxx("""
if (level < 0 || level > nix::lvlVomit)
    throw std::invalid_argument("level must be from 0 (error) to 7 (vomit)");
huggorm::thread_level().set(static_cast<nix::Verbosity>(level));
    """)


@needs("huggorm_decl/cpp/logging.hpp")
def clear_thread_verbosity() -> None:
    """Take this thread's own level away, so it follows the default."""
    Cxx("""
huggorm::thread_level().clear();
    """)


@needs("huggorm_decl/cpp/logging.hpp")
def default_verbosity() -> I64:
    """The level of a thread with no level of its own.

    Nix starts threads this binding never sees - a file transfer, a
    substituter - and each of them keeps records at this level."""
    Cxx("""
return static_cast<std::int64_t>(
    huggorm::default_verbosity().load(std::memory_order_relaxed));
    """)


@needs("huggorm_decl/cpp/logging.hpp")
def set_default_verbosity(level: I64) -> None:
    """Set the level of every thread with no level of its own.

    It raises the level a new daemon connection is told, like a
    subscription does. `subscribe_process_logs` sets this too, and
    `unsubscribe_process_logs` puts nix's own `lvlInfo` back."""
    Cxx("""
if (level < 0 || level > nix::lvlVomit)
    throw std::invalid_argument("level must be from 0 (error) to 7 (vomit)");
huggorm::set_default_verbosity(static_cast<nix::Verbosity>(level));
    """)


@needs("huggorm_decl/cpp/logging.hpp")
def log_message(level: I64, message: Str) -> None:
    """Write one message through Nix's logger, at `level`.

    The same call a Nix builtin makes, so a subscriber receives it as
    a `msg` record, and the level filters it as it filters Nix's own."""
    Cxx("""
if (level < 0 || level > nix::lvlVomit)
    throw std::invalid_argument("level must be from 0 (error) to 7 (vomit)");
nix::logger->log(static_cast<nix::Verbosity>(level), message);
    """)


@needs("huggorm_decl/cpp/logging.hpp")
def current_request() -> I64:
    """The call this thread is inside, as `begin_request` named it, or 0.

    No `@threading`, for `begin_request`'s reason."""
    Cxx("""
return static_cast<std::int64_t>(huggorm::thread_request());
    """)


@needs("huggorm_decl/cpp/logging.hpp", "huggorm_decl/cpp/interrupt.hpp")
def begin_request(request: I64) -> I64:
    """Say which call this thread is inside. Answers the one before.

    Runtime plumbing, like `gc_release_thread`, and it carries NO
    threading policy for the same reason that one does not - plus a
    sharper one. `@threading` is what gives a free function an async
    form, and an async form hops to the shared pool. This writes a
    `thread_local`, so a hop would set it on a pool thread and leave
    the thread that does the work at 0. Declared, that mistake is
    silent: every gate still passes.

    So `runtime.py` calls it directly, on the thread that is about to
    enter C++.

    It ANSWERS the previous value and `end_request` takes it back, so
    the pair nests. Nothing nests today, because every wrapped call
    hops through an executor and arrives on a thread of its own. One
    word of state removes the question anyway."""
    Cxx("""
if (request < 0)
    throw std::invalid_argument("request id must not be negative");
huggorm::install_interrupt_check();
auto & slot = huggorm::thread_request();
auto previous = slot;
slot = static_cast<std::uint64_t>(request);
return static_cast<std::int64_t>(previous);
    """)


@needs("huggorm_decl/cpp/logging.hpp")
def end_request(previous: I64) -> None:
    """Mark this thread's call finished, and restore what ran before.

    Pushes one `"finalized"` record carrying the id that is ending. An
    id alone says which call a record belongs to; it never says the
    call has stopped, so a reader waiting for one would wait until its
    own bound expired. The marker is what makes the id worth having.

    It goes through the same routing every record does, so it lands in
    the queue that call's records landed in - the thread's if it
    subscribed, the process-wide one otherwise. A queue's drop policy
    names what to DROP, and this is neither a `"msg"` nor a
    `"result"`, so no bound and no level can refuse it.

    Nobody taking it is FINE and is not an error. The fallback is a
    `SimpleLogger` and a marker has no text to print, so an
    unsubscribed caller simply never sees one."""
    Cxx("""
if (previous < 0)
    throw std::invalid_argument("request id must not be negative");
auto & slot = huggorm::thread_request();
if (slot != 0)
    huggorm::route({.action = "finalized", .request = slot});
slot = static_cast<std::uint64_t>(previous);
    """)


@needs("huggorm_decl/cpp/interrupt.hpp")
def cancel_request(request: I64) -> None:
    """Stop the call `begin_request` named, from any thread.

    The call raises `Interrupted` at Nix's next `checkInterrupt`. A
    call still queued stops at its first one, and a call that never
    reaches one finishes as if nothing happened. A blocking system
    call runs to its end first.

    Runtime plumbing, like `begin_request`: the async runtime calls it
    when the awaiting task is cancelled. The request stays cancelled
    until `forget_request`, so an id is never reused while one is
    pending."""
    Cxx("""
if (request <= 0)
    throw std::invalid_argument("request id must be positive");
huggorm::cancellations().cancel(static_cast<std::uint64_t>(request));
    """)


@needs("huggorm_decl/cpp/interrupt.hpp")
def forget_request(request: I64) -> None:
    """Drop a cancellation once its call is over.

    Separate from `end_request`, because a cancel can arrive after the
    call ended, and only the canceller knows when it has stopped
    waiting."""
    Cxx("""
if (request <= 0)
    throw std::invalid_argument("request id must be positive");
huggorm::cancellations().forget(static_cast<std::uint64_t>(request));
    """)


@needs("huggorm_decl/cpp/interrupt.hpp")
def begin_interrupt_scope(scope: I64) -> I64:
    """Arm an interrupt scope on this thread. Answers the one before.

    `begin_request` for a caller whose unit of cancellation is not its
    unit of logging: one scope can hold several requests, and
    `cancel_interrupt_scope` stops whichever of them runs. Scopes and
    requests are two tables, so equal numbers do not collide.

    No `@threading`, for `begin_request`'s reason."""
    Cxx("""
if (scope < 0)
    throw std::invalid_argument("scope id must not be negative");
huggorm::install_interrupt_check();
auto & slot = huggorm::thread_interrupt_scope();
auto previous = slot;
slot = static_cast<std::uint64_t>(scope);
return static_cast<std::int64_t>(previous);
    """)


@needs("huggorm_decl/cpp/interrupt.hpp")
def end_interrupt_scope(previous: I64) -> None:
    """Disarm this thread's scope, and restore the one before."""
    Cxx("""
if (previous < 0)
    throw std::invalid_argument("scope id must not be negative");
huggorm::thread_interrupt_scope() = static_cast<std::uint64_t>(previous);
    """)


@needs("huggorm_decl/cpp/interrupt.hpp")
def cancel_interrupt_scope(scope: I64) -> None:
    """Stop the work inside `scope`, from any thread.

    The same rules as `cancel_request`: work stops at Nix's next
    `checkInterrupt`, and the scope stays cancelled until
    `forget_interrupt_scope`."""
    Cxx("""
if (scope <= 0)
    throw std::invalid_argument("scope id must be positive");
huggorm::scope_cancellations().cancel(static_cast<std::uint64_t>(scope));
    """)


@needs("huggorm_decl/cpp/interrupt.hpp")
def forget_interrupt_scope(scope: I64) -> None:
    """Drop a scope's cancellation once nothing runs inside it."""
    Cxx("""
if (scope <= 0)
    throw std::invalid_argument("scope id must be positive");
huggorm::scope_cancellations().forget(static_cast<std::uint64_t>(scope));
    """)


# --- nix.conf, and what a caller changes after it ------------------
#
# Here and not in a module of their own, because the eval and fetcher
# settings are registered from this module (`cpp/settings.hpp`). A
# second module that registers them would hold a second copy.
#
# No threading policy, so no async form and no rpc. These change the
# PROCESS, and a remote client changing the configuration of a shared
# service is a decision nobody has made.


@needs("huggorm_decl/cpp/settings.hpp")
def get_setting(name: Str) -> Str | None:
    """The effective value of one setting, or None for an unknown name.

    Every registered setting: the store's, the evaluator's and the
    fetchers'. The value is Nix's own rendering, so a list comes back
    space-separated, the way nix.conf spells it."""
    Cxx("""
std::map<std::string, nix::Config::SettingInfo> all;
nix::globalConfig.getSettings(all);
if (auto i = all.find(name); i != all.end())
    return i->second.value;
return std::nullopt;
    """)


@needs("huggorm_decl/cpp/settings.hpp")
def load_config() -> None:
    """Read nix.conf and NIX_CONFIG into the process's settings.

    What `nix` does at startup. Import does not do it, so the host's
    configuration reaches a process only when the caller asks. A
    state built before the call keeps what it read."""
    Cxx("""
nix::loadConfFile(nix::globalConfig);
    """)


@needs("huggorm_decl/cpp/settings.hpp")
def set_setting(name: Str, value: Str) -> None:
    """Set one setting for the process, as a line of nix.conf would.

    An `EvalState` reads the evaluator and fetcher settings when it is
    built, so a state that already exists keeps what it read. A store
    reads most of its settings at each call.

    Raises `UsageError` for a name no registered setting answers to.
    `GlobalConfig::set` answers false there and says nothing, which
    is how a misspelt name would otherwise vanish."""
    Cxx("""
if (!nix::globalConfig.set(name, value))
    throw nix::UsageError("unknown setting '%s'", name);
    """)


@needs("huggorm_decl/cpp/settings.hpp")
def list_settings(overridden_only: Bint = False) -> dict[str, Str]:
    """Every registered setting and its effective value.

    `overridden_only` keeps the ones something set: nix.conf,
    NIX_CONFIG or `set_setting`. Nix tracks that per setting."""
    Cxx("""
std::map<std::string, nix::Config::SettingInfo> all;
nix::globalConfig.getSettings(all, overridden_only);
std::map<std::string, std::string> out;
for (auto & [key, info] : all)
    out.emplace(key, info.value);
return out;
    """)


@needs("huggorm_decl/cpp/settings.hpp")
def reset_overridden() -> None:
    """Forget which settings something set, and keep every value.

    `list_settings(overridden_only=True)` answers from those marks, so
    a caller that applies its own settings resets first, and then
    reads back exactly what it applied."""
    Cxx("""
nix::globalConfig.resetOverridden();
    """)


@needs("nix/util/experimental-features.hh")
def is_experimental_feature(name: Str) -> Bint:
    """Whether this Nix names an experimental feature `name`.

    `extra-experimental-features` only warns about a name it does not
    know, and enables nothing. A caller that must refuse one asks
    here first."""
    Cxx("return nix::parseExperimentalFeature(name).has_value();")


@needs("nix/util/experimental-features.hh")
def enable_experimental_feature(name: Str) -> None:
    """Add one experimental feature to the process's set.

    As a program that needs a feature turns it on at startup, and not
    as a line of nix.conf: the setting keeps its overridden mark as it
    was, so `list_settings(overridden_only=True)` still names only
    what the configuration and the caller set. Raises `UsageError` for
    a name this Nix does not know, where the setting only warns."""
    Cxx("""
auto feature = nix::parseExperimentalFeature(name);
if (!feature)
    throw nix::UsageError("unknown experimental feature '%s'", name);
auto features = nix::experimentalFeatureSettings.experimentalFeatures.get();
features.insert(*feature);
nix::experimentalFeatureSettings.experimentalFeatures = features;
    """)


@needs("huggorm_decl/cpp/settings.hpp")
def settings_json() -> Str:
    """Every registered setting as Nix describes it, in JSON.

    Nix's own document: value, default, description, aliases and the
    experimental feature a setting needs. What `nix config show
    --json` prints, less the settings of libraries not linked here."""
    Cxx("""
return nix::globalConfig.toJSON().dump();
    """)


@needs("huggorm_decl/cpp/settings.hpp")
def eval_settings_json() -> Str:
    """The evaluator's settings as `settings_json` describes them.

    libcmd's `nix::evalSettings`, which nix.conf fills and each state
    copies. A state's own settings are not in it."""
    Cxx("""
return nix::evalSettings.toJSON().dump();
    """)


@needs("huggorm_decl/cpp/settings.hpp")
def fetch_settings_json() -> Str:
    """The fetchers' settings as `settings_json` describes them.

    libcmd's `nix::fetchSettings`, as `eval_settings_json` reads
    `nix::evalSettings`."""
    Cxx("""
return nix::fetchSettings.toJSON().dump();
    """)


@needs("huggorm_decl/cpp/settings.hpp")
def flake_settings_json() -> Str:
    """The flake settings as `settings_json` describes them.

    libcmd's `nix::flakeSettings`, as `eval_settings_json` reads
    `nix::evalSettings`."""
    Cxx("""
return nix::flakeSettings.toJSON().dump();
    """)


@needs("nix/expr/eval-settings.hh")
def parse_nix_path(value: Str) -> list[Str]:
    """Split a `NIX_PATH`-style string into its entries, as `nix` does.

    A `:` inside a URL does not split it, so `nixpkgs=https://...`
    stays one entry. The environment is not read here; the caller
    passes the value it means."""
    Cxx("""
auto entries = nix::EvalSettings::parseNixPath(value);
return std::vector<std::string>(entries.begin(), entries.end());
    """)


@needs("nix/expr/eval-settings.hh")
def is_pseudo_url(value: Str) -> Bint:
    """Whether a search path entry is a URL Nix fetches, not a path."""
    Cxx("""
return nix::EvalSettings::isPseudoUrl(value);
    """)


@needs("nix/cmd/common-eval-args.hh")
def current_system() -> Str:
    """The system `builtins.currentSystem` answers, process-wide.

    `eval-system`, or the machine's `system` when that is empty. An
    `EvalState` built with its own `eval-system` answers its own."""
    Cxx("""
return nix::evalSettings.getCurrentSystem();
    """)


@needs("nix/store/globals.hh")
def nix_version() -> Str:
    """The version of the Nix these bindings link, as `nix --version`
    prints it."""
    Cxx("""
return nix::nixVersion;
    """)


@needs("nix/expr/config.hh")
def boehm_gc() -> Bint:
    """Whether the linked libexpr allocates through the Boehm collector.

    A build without it never frees a value, which is tolerable only
    in a process that ends soon."""
    Cxx("""
return static_cast<bool>(NIX_USE_BOEHMGC);
    """)


@needs("huggorm_decl/cpp/logging.hpp")
@threading("pool")
def subscribe_process_logs(capacity: I64 = 1024,
                           level: I64 = 3) -> LogStream:
    """Record what no subscribed thread claims.

    A FREE function, because there is nothing to hang it on. Every
    other subscription belongs to a thread that an `EvalState` owns;
    this one belongs to the process, and `EvalState.subscribe_logs`
    already carries a `(void) self` that says the state was only ever
    a way to reach the right thread.

    What it sees is what `EvalState.subscribe_logs` names and cannot
    reach: a record raised on a fetcher thread, a file-transfer
    thread, or a build. A build's log is the one a Nix user most
    wants (`tasks/085`).

    NOT "everything in this process". A thread that subscribed CLAIMS
    its records, so an evaluation with its own subscriber does not
    appear here at all. Broadcasting to both was the alternative and
    it was rejected: a caller holding both subscriptions would see
    every evaluation record twice, with nothing on a record to
    deduplicate by. One reader over one subscription is the shape
    that answers the other question, and it is `tasks/085`'s third
    gap.

    `level` RAISES THE PROCESS DEFAULT, and it has to. The records
    this exists for come from threads that never set a level of their
    own, so they read the default - and a default left at `lvlInfo`
    would have the tap drop a debug record before this queue ever saw
    it. Asking for `6` here means every unclaimed thread reports at
    6 until the subscription ends.

    It raises the daemon level too, and that one reaches further than
    this process: `RemoteStore::setOptions` sends it to the daemon
    (`nix-remote-verbosity.patch`), which then narrates every worker
    op back over the socket. `unsubscribe_process_logs` gives it back. A
    daemon connection ALREADY OPEN keeps what the handshake gave it,
    which is the one thing giving it back cannot reach (`tasks/096`).

    REPLACES any process-wide subscription, and closes it. Refusing
    was the other answer: an in-process caller that drops its
    `LogStream` without unsubscribing would then wedge the sink for
    the life of the process. The rpc refuses instead, where a stream
    ending is what releases it.

    The bound means something different here. A per-state queue is
    bounded by one evaluation, so keeping every `"start"` and
    `"stop"` is affordable - which is why they are never dropped. A
    process-wide queue in a service that runs for days is bounded by
    the READER draining it, and a reader that stops draining grows
    this without limit. The capacity does not save it, because the
    records it never drops are the ones that accumulate."""
    Cxx("""
if (capacity < 1)
    throw std::invalid_argument("capacity must be at least 1");
if (level < 0)
    throw std::invalid_argument("level must not be negative");
return huggorm::subscribe_process_logs(static_cast<std::size_t>(capacity),
                                       static_cast<std::uint64_t>(level));
    """)


@needs("huggorm_decl/cpp/logging.hpp")
@threading("pool")
@binds("huggorm::unsubscribe_process_logs")
def unsubscribe_process_logs() -> None:
    """Stop recording process-wide.

    A queue already handed out still drains what it holds, exactly
    like `EvalState.unsubscribe_logs`. This says only that nothing
    more goes into it.

    It puts BOTH levels back: the process default returns to nix's
    own `lvlInfo`, and the daemon level drops to the widest level any
    per-thread subscription still holds. `tasks/095` measured what
    the second one cost while it was missing - 1052 daemon debug
    lines on an unsubscribed caller's stderr.

    It CROSSES the wire, and its counterpart does not, for the same
    reason the pair on `EvalState` splits that way: this answers
    nothing, so the refusal that stops a `LogStream` handle crossing
    does not apply to it.

    So a remote caller can stop a subscription somebody else made -
    including the one an open `Session/ProcessLogs` stream is pumping,
    which would leave that stream connected and silent. That is the
    same power every shared handle already grants, and the same one
    `EvalState.unsubscribe_logs` grants over a state's thread. Named
    here so it reads as the sharing model rather than an oversight."""


@needs("huggorm_decl/cpp/logging.hpp")
@binds("huggorm::install_log_tap")
@startup
def _log_tap_init() -> None:
    """Put the log tap behind the logger already installed, once.

    At import rather than on the first `subscribe_logs`, because
    `nix::logger` is a plain global `unique_ptr` (`logging.hh:258`)
    and replacing it while another thread reads it is a race with no
    lock to take.

    A REPLACEMENT, and it was a tee until `tasks/089`. A tee kept the
    logger that was already there as the MAIN one, so every record
    reached stderr whether a subscriber took it or not - and a client
    reading this protocol over stdin/stdout cannot have that.

    Subscribing to nothing still changes nothing. `LogTap` carries a
    `SimpleLogger` fallback and forwards to it whatever no queue
    claimed, so an unsubscribed caller sees what it saw before. What
    DOES change is the subscribed case: a claimed record no longer
    also appears on stderr."""


@needs("huggorm_decl/cpp/gc.hpp")
@binds("huggorm::gc_boot")
@startup
def _gc_init() -> None:
    """Initialise the collector on the importing thread, and start no
    thread.

    `nix::initGC` waits for the first evaluator or thread
    registration (`gc_start`), because it starts the marker threads.
    Importing huggorm starts none."""
