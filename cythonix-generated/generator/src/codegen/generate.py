"""
CLI: read the declarations, emit the package.

Glue only — the protocol dict comes from `cythonix_idl`, its rules
live in model.py, and emission lives in emitter.py. Installed as the
`codegen-generate` entry point.
"""

import argparse
import ast
import copy
import json
import pathlib
import shutil
import sys
from enum import Enum
from types import ModuleType
from typing import Any

from codegen.emitter import (
    FREE_MODULE,
    STUB_PACKAGE,
    free_function_module,
    init_module,
    protocol_module,
    returned_module,
    rpc_module,
    stub_init_module,
    stub_module,
    wrapper_module,
)
from codegen.model import (
    check_collection_contract,
    check_error_contract,
    check_optional_contract,
    check_wire_contract,
    check_wrap_contract,
    extract_enum,
    extract_errors,
)
from codegen.wiretypes import MANIFEST_SCHEMA, names_in
from cythonix_idl.generate import (
    declared_entries,
    declared_functions,
    declared_returned,
    declared_unions,
)

# See model.Proto: one class, method or function as a plain dict.
Proto = dict[str, Any]


def _load_bindings_module() -> ModuleType:
    import cythonix_bindings

    return cythonix_bindings


def _wrapper_classes(bindings_module: ModuleType) -> list[type]:
    """
    Every public wrapper class that declares a threading policy, except
    those excluded from generation (_async = False, e.g. the abstract
    base). Sorted for deterministic output.
    """
    out = []
    pkg = bindings_module.__name__
    for name in dir(bindings_module):
        obj = getattr(bindings_module, name)
        if not isinstance(obj, type):
            continue
        mod = getattr(obj, "__module__", "")
        if mod != pkg and not mod.startswith(pkg + "."):
            continue
        if name.startswith("_"):
            continue
        # Own-class lookup only: plain getattr would inherit Animal's
        # _async = False through the MRO and exclude every subclass.
        if obj.__dict__.get("_async", True) is False:
            continue
        if not hasattr(obj, "_threading"):
            continue
        out.append(obj)
    return sorted(out, key=lambda c: c.__name__)


def _enum_classes(bindings_module: ModuleType) -> list[type]:
    """Every public string enum the bindings export.

    Found by reflection, with no declaration of its own: a class that
    subclasses both str and Enum IS a string vocabulary, and there is
    nothing else it could be. That is the difference from the error
    hierarchy, which needed `_errors_module` because an exception
    class looks like any other class.

    They are not wrappers - _wrapper_classes wants a _threading policy
    and an enum has none - so nothing generates an async form for one.
    A member is a str, so it crosses the wire as a str and the schema
    needs no new field type."""
    pkg = bindings_module.__name__
    out = []
    for name in dir(bindings_module):
        obj = getattr(bindings_module, name)
        if not isinstance(obj, type) or name.startswith("_"):
            continue
        mod = getattr(obj, "__module__", "")
        if mod != pkg and not mod.startswith(pkg + "."):
            continue
        if issubclass(obj, str) and issubclass(obj, Enum):
            out.append(obj)
    return sorted(out, key=lambda c: c.__name__)


def _free_functions(bindings_module: ModuleType) -> list[Any]:
    """Every public module-level function in the bindings, sorted.

    All of them, not only the ones declaring _threading. The policy
    decides whether a function gets an async wrapper and an rpc; it
    does not decide whether the function EXISTS. A stub package that
    described only the opted-in ones would hide the rest from a
    typechecker while the module still exports them."""
    pkg = bindings_module.__name__
    out = []
    for name in sorted(dir(bindings_module)):
        obj = getattr(bindings_module, name)
        if name.startswith("_") or isinstance(obj, type) or not callable(obj):
            continue
        mod = getattr(obj, "__module__", "")
        if mod != pkg and not mod.startswith(pkg + "."):
            continue
        out.append(obj)
    return out


def _sig(m: Proto) -> list[str]:
    """One method's parameter list, as the pair that has to match:
    the declared type and the declared default."""
    return [f"{p['name']}: {p['type']}"
            + (f" = {p['default']}" if p["default"] is not None else "")
            for p in m["params"]]


def _hierarchy(
    wrapper_classes: list[type], protos: list[Proto]
) -> tuple[dict[str, str], dict[str, set[str]], list[str]]:
    """Link each emitted wrapper to its nearest emitted ancestor, and
    work out which methods the ancestor may guarantee.

    The base carries the INTERSECTION of what its subclasses actually
    expose after their own policy filtering - which is exactly the set a
    caller can use without knowing which implementation it holds, and so
    exactly what an abstract Store is for. Nothing is shadow-dropped: a
    method the base does not promise simply is not on it.

    That resolves the tension between inheritance and per-subclass
    policy. LocalStore is pool and loses query_derivation, RemoteStore
    is affine and keeps it, so query_derivation is not part of the
    guaranteed surface and lands on RemoteStore alone.

    Returns (base_of, shared_of, complaints).
    """
    by_class = dict(zip(wrapper_classes, protos, strict=True))
    emitted = set(wrapper_classes)
    base_of: dict[str, str] = {}
    children: dict[str, list[Proto]] = {}
    for cls in wrapper_classes:
        parent = next((k for k in cls.__mro__[1:] if k in emitted), None)
        if parent is not None:
            base_of[by_class[cls]["name"]] = by_class[parent]["name"]
            children.setdefault(by_class[parent]["name"], []).append(by_class[cls])

    shared_of: dict[str, set[str]] = {}
    complaints: list[str] = []
    for base_name, kids in children.items():
        base = next(p for p in protos if p["name"] == base_name)
        names = {m["name"] for m in base["methods"]}
        for kid in kids:
            names &= {m["name"] for m in kid["methods"]}
        shared_of[base_name] = names

        # Inheritance is only sound if the shared methods really are the
        # same method. A subclass whose signature drifted would inherit
        # the base's body and lie about its own types.
        base_sigs = {m["name"]: m for m in base["methods"]}
        for kid in kids:
            for m in kid["methods"]:
                if m["name"] not in names:
                    continue
                b = base_sigs[m["name"]]
                # Defaults too, not only types. The base declares the
                # method the subclass inherits, so a subclass that
                # changed only a default would be called with the
                # base's one - and the two would disagree about what
                # the short call means.
                if (_sig(m) != _sig(b) or m["return_type"] != b["return_type"]):
                    complaints.append(
                        f"{kid['name']}.{m['name']} does not match "
                        f"{base_name}.{m['name']}: "
                        f"{_sig(m)} -> {m['return_type']} "
                        f"vs {_sig(b)} -> {b['return_type']}")
    return base_of, shared_of, complaints


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="output directory for cythonix_generated")
    args = parser.parse_args(argv)

    bindings = _load_bindings_module()

    wrapper_classes = _wrapper_classes(bindings)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Which classes are HANDED BACK rather than constructed. The
    # declaration says, and it is the only thing that could: this was
    # a walk over the pxd's return types.
    returned_classes: list[type] = []
    named: set[str] = set()
    for name in declared_returned():
        kls = getattr(bindings, name, None)
        if isinstance(kls, type) and name not in named:
            returned_classes.append(kls)
    returned_classes.sort(key=lambda c: c.__name__)
    returned_set = set(returned_classes)
    wrapper_classes = [c for c in wrapper_classes if c not in returned_set]
    if not wrapper_classes:
        print("no constructible wrapper classes found", file=sys.stderr)
        sys.exit(1)

    # Where every proto dict comes from.
    #
    # This is the seam that ended the build's one possible order.
    # Every proto dict used to come from IMPORTING the compiled
    # extension and reflecting on it - which put the async wrappers,
    # the protocols, the RPC stubs and the type stubs behind a C++
    # compiler for facts a person wrote in a declaration first.
    #
    # Reflection ran beside this for as long as there was something to
    # measure against, and the claim held: the declaration carries
    # everything reflection found. Then the compiled class it measured
    # was a nanobind one, which has no signature to reflect, and the
    # other route stopped existing.
    #
    # Imported, not read from a file. The specification is Python and
    # so is this, so a serialisation between them would be one more
    # shape to keep in step.
    declared: dict[str, Proto] = declared_entries()
    print(f"declared entries: {len(declared)} class(es) - "
          + ", ".join(sorted(declared)))

    def _proto(kls: type) -> Proto:
        """One binding class, as the declaration that describes it.

        A COPY. `_proto` is called twice for every class - once for
        the wrappers and once for the stubs - and the wrapper pass
        edits `methods` in place, dropping the affine-returning ones
        from a pool class. Handing back the same dict both times let
        that edit reach the stubs, which describe the BINDING and have
        no such rule."""
        want = declared.get(kls.__name__)
        if want is None:
            # Not a fallback. A binding module holds nothing but what
            # a declaration emitted, so a class here that no
            # declaration names means the two lists disagree - and
            # guessing its surface is how a wrong answer reaches four
            # generated files at once.
            raise SystemExit(
                f"{kls.__module__}.{kls.__qualname__} is in the bindings "
                f"but no declaration names it")
        print(f"  {want['name']}: from the declaration")
        return copy.deepcopy(want)

    returned_protos = [_proto(kls) for kls in returned_classes]
    protos = [
        _proto(svc)
        for svc in wrapper_classes
    ]

    # Which classes get an async wrapper at all. A pool class whose
    # methods cannot block gets nothing from one, so it crosses every
    # layer as the sync binding object itself (tasks/025).
    complaints = check_wrap_contract(protos + returned_protos)
    if complaints:
        for c in complaints:
            print(f"wrap contract: {c}", file=sys.stderr)
        sys.exit(1)
    # ...and nothing may return a CONTAINER of wrapped types. Every
    # layer attaches a runner to one object, not to the elements of a
    # collection, so such a return builds and then fails at the first
    # call that touches it.
    complaints = check_collection_contract(protos + returned_protos)
    if complaints:
        for c in complaints:
            print(f"collection contract: {c}", file=sys.stderr)
        sys.exit(1)
    # ...and an optional return may name a VALUE, never a wrapped
    # type. The wire can carry absence - a message field has presence
    # - but no layer adopts nothing into a runner.
    complaints = check_optional_contract(protos + returned_protos)
    if complaints:
        for c in complaints:
            print(f"optional contract: {c}", file=sys.stderr)
        sys.exit(1)
    unwrapped = sorted(p["name"] for p in protos + returned_protos
                       if not p["wrapped"])
    if unwrapped:
        print(f"not wrapped (pool and non-blocking, so nothing to wrap): "
              f"{', '.join(unwrapped)}")

    # Only a WRAPPED returned type gets adopted into a runner; an
    # unwrapped one is handed back exactly as the binding produced it.
    returned_policies = {p["name"]: p["threading"] for p in returned_protos
                         if p["wrapped"]}

    # policy enforcement: a pool wrapper may not return affine types at
    # all - drop them from the surface entirely.
    affine_bound = {name for name, pol in returned_policies.items() if pol == "affine"}
    for proto in protos:
        if proto["threading"] == "pool":
            before = len(proto["methods"])
            proto["methods"] = [m for m in proto["methods"] if m["return_type"] not in affine_bound]
            dropped = before - len(proto["methods"])
            if dropped:
                print(f"dropped {dropped} affine-returning method(s) "
                      f"from pool wrapper {proto['name']}")

    base_of, shared_of, complaints = _hierarchy(wrapper_classes, protos)
    if complaints:
        for c in complaints:
            print(f"hierarchy: {c}", file=sys.stderr)
        sys.exit(1)
    for proto in protos:
        proto["async_base"] = base_of.get(proto["name"])
        shared = shared_of.get(proto["name"])
        if shared is not None:
            # A different `dropped` from the count above; naming it
            # so was how a typechecker noticed.
            only_on_subclasses = [m["name"] for m in proto["methods"]
                                  if m["name"] not in shared]
            if only_on_subclasses:
                print(f"{proto['name']} guarantees {sorted(shared)}; "
                      f"{sorted(only_on_subclasses)} live on subclasses only")
            proto["methods"] = [m for m in proto["methods"] if m["name"] in shared]
    # Subclasses keep only what the base does not already provide.
    for proto in protos:
        base = proto["async_base"]
        if base is not None:
            inherited = shared_of.get(base, set())
            proto["inherited"] = sorted(
                m["name"] for m in proto["methods"] if m["name"] in inherited)
            proto["methods"] = [m for m in proto["methods"]
                                if m["name"] not in inherited]

    # Every name that gets an Async wrapper. Parameters typed with one of
    # these accept the wrapper as well as the sync binding object, and
    # the emitter widens their annotations accordingly.
    async_types = {p["name"] for p in returned_protos + protos if p["wrapped"]}

    # The async spelling of a type, when the bindings declare one. Read
    # here rather than off the manifest because the wrappers are
    # emitted before the manifest is assembled.
    async_twins: dict[str, str] = dict(getattr(bindings, "_async_twins", {}))

    for proto in returned_protos:
        if not proto["wrapped"]:
            continue
        fname = f"async_{proto['name'].lower()}.py"
        code = ast.unparse(returned_module(proto, async_types, returned_policies,
                                           async_twins))
        (out / fname).write_text(code + "\n")
        print(f"generated {fname} for returned type {proto['name']} ({proto['threading']})")

    for proto in protos:
        if not proto["wrapped"]:
            continue
        fname = f"async_{proto['name'].lower()}.py"
        code = ast.unparse(wrapper_module(proto, returned_policies,
                                          async_twins, async_types))
        (out / fname).write_text(code + "\n")
        print(f"generated {fname} for {proto['name']} "
              f"({proto['threading']}, {len(proto['methods'])} methods)")

    # A free function comes from the declaration where there is one,
    # and from reflection where there is not - the same rule the
    # classes follow one screen up, and for the same reason. A
    # nanobind function is a builtin: `inspect.signature` refuses it,
    # so there is nothing to reflect.
    declared_fns = declared_functions()
    free_protos = [declared_fns[fn.__name__]
                   for fn in _free_functions(bindings)
                   if fn.__name__ in declared_fns]
    from_decl = sorted(set(declared_fns) &
                       {fn.__name__ for fn in _free_functions(bindings)})
    if from_decl:
        print(f"free functions from the declaration: "
              f"{', '.join(from_decl)}")
    wrapped_free = [p for p in free_protos if p["wrapped"]]
    unwrapped_free = [p["name"] for p in free_protos if not p["wrapped"]]
    if unwrapped_free:
        print(f"free functions with no threading policy, so no wrapper: "
              f"{', '.join(unwrapped_free)}")
    free_names = [p["name"] for p in wrapped_free]
    if wrapped_free:
        code = ast.unparse(free_function_module(wrapped_free, async_types))
        (out / f"{FREE_MODULE}.py").write_text(code + "\n")
        print(f"generated {FREE_MODULE}.py for {len(wrapped_free)} free "
              f"function(s): {', '.join(free_names)}")

    # The wire policy and the serialization contract must agree before
    # anything downstream trusts either. Loud, at build time.
    #
    # The enum NAMES go in with them: an enum is a scalar everywhere
    # else, so a wire field may declare one. Read here rather than
    # from the manifest, which is not built yet.
    enum_names = {k.__name__ for k in _enum_classes(bindings)}
    # A union is not a class in the manifest's groups either, and for
    # a sharper reason than an enum: it never reaches an extension at
    # all. `DerivedPath = StorePath | DerivedPathBuilt` is module-level
    # Python in the declaration, so the only route here is the
    # declaration itself.
    unions = declared_unions()
    complaints = check_wire_contract(
        protos + returned_protos, enum_names, set(unions))
    if complaints:
        for c in complaints:
            print(f"wire contract: {c}", file=sys.stderr)
        sys.exit(1)
    # These probes exist for that check only; they are not surface.
    for proto in protos + returned_protos:
        proto.pop("_helpers", None)

    # The exception hierarchy, read from the module the bindings
    # declare. An error crosses the wire as a NAME, and this is the set
    # that makes a name safe to construct (tasks/036).
    errors = extract_errors(bindings)
    complaints = check_error_contract(errors)
    if complaints:
        for c in complaints:
            print(f"error contract: {c}", file=sys.stderr)
        sys.exit(1)

    # String vocabularies libstore parses. A member is a str, so this
    # table says only "this NAME is a scalar" - to the schema, to the
    # codec, and to the stub generator, which needs to import it.
    enums = {k.__name__: extract_enum(k) for k in _enum_classes(bindings)}

    manifest: Proto = {
        "schema": MANIFEST_SCHEMA,
        "wrappers": {p["name"]: p for p in protos},
        "returned_types": {p["name"]: p for p in returned_protos},
        "free_functions": {p["name"]: p for p in free_protos},
        "errors": errors,
        "enums": enums,
        # {alias: [arm, ...]}, in DECLARED order - the order a oneof
        # numbers its fields in, so a reorder is a wire change.
        "unions": unions,
        # The async spelling of a type, when it has one. Declared by
        # the bindings; the emitter turns it into one annotation and
        # one constructor call.
        "async_twins": async_twins,
    }

    # No silent Any may survive into the artifact: a method whose types
    # never resolved is uncallable over the wire while looking alive
    # locally. Fail the build naming every offender; an explicit escape
    # hatch can be added when a legitimate case first appears.
    unresolved: list[str] = []
    for fname, proto in manifest["free_functions"].items():
        for p in proto["params"]:
            if p["type"] == "Any":
                unresolved.append(f"{fname} param {p['name']!r}")
        if proto["return_type"] == "Any":
            unresolved.append(f"{fname} return type")
    for group in ("wrappers", "returned_types"):
        for cls_name, proto in manifest[group].items():
            for p in proto.get("ctor", ()):
                if p["type"] == "Any":
                    unresolved.append(
                        f"{cls_name}.__init__ param {p['name']!r}")
            for m in proto["methods"]:
                for p in m["params"]:
                    if p["type"] == "Any":
                        unresolved.append(
                            f"{cls_name}.{m['name']} param {p['name']!r} "
                        f"(the declaration does not spell it)")
                if m["return_type"] == "Any":
                    unresolved.append(f"{cls_name}.{m['name']} return type")
    if unresolved:
        for u in unresolved:
            print(f"unresolved type: {u}", file=sys.stderr)
        sys.exit(1)

    # grpc_schema owns wire naming; stamping it into the manifest is what
    # lets the server and the client read the names instead of each
    # rebuilding the same convention from scratch.
    from codegen import surface
    from codegen.grpc_schema import annotate, build_fdset
    annotate(manifest)
    surface.annotate(manifest)

    # The three surfaces - protocol, in-process wrapper, RPC client -
    # must agree on which returns get adopted into an object of their
    # own. returned_policies is that set; check nothing else claims it.
    complaints = surface.check_adoptable(manifest, set(returned_policies))
    if complaints:
        for c in complaints:
            print(f"adoptable: {c}", file=sys.stderr)
        sys.exit(1)

    ordered = surface.order(manifest)
    adoptable = set(returned_policies)
    (out / f"{surface.PROTOCOL_MODULE}.py").write_text(
        ast.unparse(protocol_module(manifest, ordered, adoptable)) + "\n")
    (out / f"{surface.RPC_MODULE}.py").write_text(
        ast.unparse(rpc_module(manifest, ordered,
                               surface.wrapped_names(manifest))) + "\n")
    withheld = [
        f"{proto['name']}.{m['name']}"
        for proto in ordered for m in proto["methods"] if m["protocol_blockers"]
    ]
    print(f"generated {surface.PROTOCOL_MODULE}.py and "
          f"{surface.RPC_MODULE}.py for {len(ordered)} class(es)")
    for name in withheld:
        proto_name, _, m_name = name.partition(".")
        proto = next(p for p in ordered if p["name"] == proto_name)
        m = next(m for m in proto["methods"] if m["name"] == m_name)
        for why in m["protocol_blockers"]:
            print(f"warning: {name} is not on the protocol - {why}")

    all_names = [p["name"] for p in ordered]
    (out / "__init__.py").write_text(
        ast.unparse(init_module(all_names, free_names)) + "\n")

    # Type stubs for the bindings themselves (tasks/027). The bindings
    # are compiled extensions, so a typechecker reads no signatures out
    # of them and every binding type resolves to Any - which made the
    # generated protocols name types that check nothing. Everything
    # needed is already in the protocol dicts.
    #
    # A PEP 561 stub-only package, because a .pyi has to sit beside the
    # module it describes and the bindings are built and installed
    # before this runs. The generated package cannot write into them.
    stub_dir = out.parent / STUB_PACKAGE
    stub_dir.mkdir(parents=True, exist_ok=True)
    # Re-extracted, NOT the protos above: those have been through the
    # affine-return drop and 018's hierarchy split, which are rules
    # about the async wrappers. The bindings themselves have neither.
    #
    # Through `_proto`, so the stubs read the same declaration the
    # rest does. A second route to the same fact was a second answer
    # to it: reflecting here directly made `manifest.json` stop
    # claiming PathInfo has an ordering while `store.pyi` went on
    # claiming it, because the stubs never saw the declaration.
    all_protos = (
        [_proto(k) for k in returned_classes]
        + [_proto(k) for k in wrapper_classes])
    # Bases before subclasses: a stub may forward-reference, but there
    # is no reason to make a reader do it.
    order_of = {p["name"]: i for i, p in enumerate(all_protos)}
    all_protos.sort(key=lambda p: (
        len([b for b in p["bases"] if b.rsplit(".", 1)[-1] in order_of]),
        order_of[p["name"]]))
    # A stub says NoReturn for a constructor that raises. Returned
    # types are produced by definition; a wrapper says so with
    # `@produced`, which is the declaration's word for it.
    produced = ({p["name"] for p in returned_protos}
                | {p["name"] for p in all_protos if p["produced"]})
    home = {p["name"]: p["module"] for p in all_protos}
    # An enum is named in a signature and defined somewhere else, so
    # the stub for the module that names it needs the import. `home`
    # is where a stub learns that, and it held only wrapper and
    # returned-type modules until now.
    home.update({name: proto["module"] for name, proto in enums.items()})
    modules = sorted({p["module"] for p in all_protos}
                     | {p["module"] for p in free_protos})
    exported: dict[str, list[str]] = {}
    for module in modules:
        mine = [p for p in all_protos if p["module"] == module]
        mine_free = [p for p in free_protos if p["module"] == module]
        # Types this module names but does not define. Read through
        # the subscripts, not off the head: a method returning
        # `list[StorePath]` names StorePath as surely as one returning
        # StorePath does, and the stub needs the import either way.
        written = [p["type"] for pr in mine for m in pr["methods"]
                   for p in m["params"]]
        written += [m["return_type"] for pr in mine for m in pr["methods"]]
        written += [p["type"] for pr in mine for p in pr["ctor"]]
        written += [p["type"] for pr in mine_free for p in pr["params"]]
        written += [pr["return_type"] for pr in mine_free]
        # And the defaults, which are expressions rather than types but
        # name types all the same: `method: ContentAddressMethod =
        # ContentAddressMethod.NAR` needs the import for the second
        # half even if the first half were spelled differently.
        written += [p["default"] for pr in mine for m in pr["methods"]
                    for p in m["params"] if p["default"] is not None]
        written += [p["default"] for pr in mine_free for p in pr["params"]
                    if p["default"] is not None]
        mentioned = {n for t in written for n in names_in(t)}
        foreign = {n: home[n] for n in mentioned
                   if n in home and home[n] != module}
        short = module.rsplit(".", 1)[-1]
        (stub_dir / f"{short}.pyi").write_text(ast.unparse(
            stub_module(module, mine, mine_free, produced, foreign)) + "\n")
        exported[module] = [p["name"] for p in mine] + [p["name"] for p in mine_free]
    # The enums, re-exported from wherever they live. They get NO .pyi
    # of their own and want none: they are plain Python, and `partial`
    # in py.typed is exactly the instruction to read the real module
    # for anything these stubs do not cover. Only __init__.pyi has to
    # mention them, because it must re-export what the package does.
    for name, proto in sorted(enums.items()):
        exported.setdefault(proto["module"], []).append(name)
    (stub_dir / "__init__.pyi").write_text(
        ast.unparse(stub_init_module(
            {m: exported[m] for m in sorted(exported)})) + "\n")
    # PARTIAL, and the word is load-bearing. A stubs package normally
    # REPLACES the runtime one for a typechecker, so a hand-written
    # Python module in the bindings - errors.py - would vanish behind
    # stubs that never mention it. Partial says "fall back to the real
    # package for anything not stubbed here", which is exactly right:
    # these stubs exist because a compiled extension carries no
    # signatures, and a .py file needs no help.
    (stub_dir / "py.typed").write_text("partial\n")
    print(f"generated {STUB_PACKAGE}/ for {len(modules)} binding module(s): "
          + ", ".join(sorted(m.rsplit('.', 1)[-1] for m in modules)))

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"wrote manifest ({len(protos)} wrappers, {len(returned_protos)} returned types) "
        f"to {out / 'manifest.json'}"
    )

    for fname, proto in manifest["free_functions"].items():
        for why in proto["wire_blockers"]:
            print(f"warning: {fname} has no RPC surface - {why}")
    # The same for methods. A method with no rpc keeps its in-process
    # wrapper and leaves the protocol, which is a quiet change if the
    # build does not say it out loud.
    for group in ("wrappers", "returned_types"):
        for cls_name, proto in manifest[group].items():
            for m in proto["methods"]:
                for why in m.get("wire_blockers", ()):
                    print(f"warning: {cls_name}.{m['name']} has no RPC "
                          f"surface - {why}")

    (out / "grpc_schema.pb").write_bytes(build_fdset(manifest))
    print(f"wrote grpc_schema.pb to {out / 'grpc_schema.pb'}")

    here = pathlib.Path(__file__).parent
    shutil.copy(here / "runtime.py", out / "_runtime.py")
    # The codec reads declared type strings at run time and the schema
    # builder reads them at build time. One definition, copied, rather
    # than two that agree until one of them changes.
    shutil.copy(here / "wiretypes.py", out / "_wiretypes.py")
    print(f"copied runtime into {out}")

    # PEP 561: without this marker a typechecker skips an INSTALLED
    # package entirely, however well annotated it is. The whole point
    # of the protocols is that a consumer can be checked against them,
    # and a consumer imports the installed package.
    (out / "py.typed").write_text("")


if __name__ == "__main__":
    main()
