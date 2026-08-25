"""
CLI: parse the pxd, reflect the installed bindings, emit the package.

Glue only — extraction lives in model.py, emission in emitter.py.
Installed as the `codegen-generate` entry point.
"""

import argparse
import ast
import json
import pathlib
import shutil
import sys
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
    binding_map,
    check_binding_map,
    check_collection_contract,
    check_error_contract,
    check_wire_contract,
    check_wrap_contract,
    extract_errors,
    extract_free_function,
    extract_wrapper,
    returned_types_from_api,
    unbound_pxd_classes,
)
from codegen.pxd import extract_api
from codegen.wiretypes import MANIFEST_SCHEMA, names_in

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
                if ([p["type"] for p in m["params"]] != [p["type"] for p in b["params"]]
                        or m["return_type"] != b["return_type"]):
                    complaints.append(
                        f"{kid['name']}.{m['name']} does not match "
                        f"{base_name}.{m['name']}: "
                        f"{[p['type'] for p in m['params']]} -> {m['return_type']} "
                        f"vs {[p['type'] for p in b['params']]} -> {b['return_type']}")
    return base_of, shared_of, complaints


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="output directory for cythonix_generated")
    parser.add_argument(
        "--pxd",
        required=True,
        nargs="+",
        help="paths to the bindings .pxd declaration files (the C++ mapping)",
    )
    args = parser.parse_args(argv)

    bindings = _load_bindings_module()

    api: dict[str, Any] = {"classes": {}, "free_functions": []}
    for path_str in args.pxd:
        part = extract_api(pathlib.Path(path_str).read_text())
        api["classes"].update(part["classes"])
        # Free functions used to be parsed here and dropped on the floor.
        api["free_functions"] += part["free_functions"]
        print(f"parsed pxd: {len(part['classes'])} classes, "
              f"{len(part['free_functions'])} free function(s) from {path_str}")

    # The pxd and the pyx are the two hand-written files, and _binds is
    # the only thing joining them. Check the join before trusting either.
    mapping = binding_map(bindings)
    complaints = check_binding_map(api, mapping)
    if complaints:
        for c in complaints:
            print(f"binding map: {c}", file=sys.stderr)
        sys.exit(1)
    for c_name in unbound_pxd_classes(api, mapping):
        print(f"warning: pxd declares {c_name}, no binding claims it with _binds")
    print(f"binding map: {len(mapping)} classes "
          + ", ".join(f"{c}->{py}" for c, py in sorted(mapping.items())))

    wrapper_classes = _wrapper_classes(bindings)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    returned_classes = returned_types_from_api(api, bindings, mapping)
    returned_set = set(returned_classes)
    wrapper_classes = [c for c in wrapper_classes if c not in returned_set]
    if not wrapper_classes:
        print("no constructible wrapper classes found", file=sys.stderr)
        sys.exit(1)

    returned_protos = [
        extract_wrapper(kls, api=api, mapping=mapping) for kls in returned_classes
    ]
    protos = [extract_wrapper(svc, api=api, mapping=mapping, constructible=True)
              for svc in wrapper_classes]

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

    for proto in returned_protos:
        if not proto["wrapped"]:
            continue
        fname = f"async_{proto['name'].lower()}.py"
        code = ast.unparse(returned_module(proto, async_types, returned_policies))
        (out / fname).write_text(code + "\n")
        print(f"generated {fname} for returned type {proto['name']} ({proto['threading']})")

    for proto in protos:
        if not proto["wrapped"]:
            continue
        fname = f"async_{proto['name'].lower()}.py"
        code = ast.unparse(wrapper_module(proto, returned_policies, async_types))
        (out / fname).write_text(code + "\n")
        print(f"generated {fname} for {proto['name']} "
              f"({proto['threading']}, {len(proto['methods'])} methods)")

    free_protos = [extract_free_function(fn, api, mapping)
                   for fn in _free_functions(bindings)]
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
    complaints = check_wire_contract(protos + returned_protos)
    if complaints:
        for c in complaints:
            print(f"wire contract: {c}", file=sys.stderr)
        sys.exit(1)
    # The helper probe exists for that check only; it is not surface.
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

    manifest: Proto = {
        "schema": MANIFEST_SCHEMA,
        "wrappers": {p["name"]: p for p in protos},
        "returned_types": {p["name"]: p for p in returned_protos},
        "free_functions": {p["name"]: p for p in free_protos},
        "errors": errors,
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
                        f"(live annotation and pxd both silent)")
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
    all_protos = (
        [extract_wrapper(k, api=api, mapping=mapping) for k in returned_classes]
        + [extract_wrapper(k, api=api, mapping=mapping, constructible=True)
           for k in wrapper_classes])
    # Bases before subclasses: a stub may forward-reference, but there
    # is no reason to make a reader do it.
    order_of = {p["name"]: i for i, p in enumerate(all_protos)}
    all_protos.sort(key=lambda p: (
        len([b for b in p["bases"] if b.rsplit(".", 1)[-1] in order_of]),
        order_of[p["name"]]))
    produced = {p["name"] for p in returned_protos}
    home = {p["name"]: p["module"] for p in all_protos}
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
        mentioned = {n for t in written for n in names_in(t)}
        foreign = {n: home[n] for n in mentioned
                   if n in home and home[n] != module}
        short = module.rsplit(".", 1)[-1]
        (stub_dir / f"{short}.pyi").write_text(ast.unparse(
            stub_module(module, mine, mine_free, produced, foreign)) + "\n")
        exported[module] = [p["name"] for p in mine] + [p["name"] for p in mine_free]
    (stub_dir / "__init__.pyi").write_text(
        ast.unparse(stub_init_module(exported)) + "\n")
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
