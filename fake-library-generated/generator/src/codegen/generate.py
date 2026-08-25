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

from codegen.emitter import wrapper_module, returned_module, init_module
from codegen.model import (
    binding_map,
    check_binding_map,
    check_wire_contract,
    extract_wrapper,
    returned_types_from_api,
    unbound_pxd_classes,
)
from codegen.pxd import extract_api


def _load_bindings_module():
    import fake_library

    return fake_library


def _wrapper_classes(bindings_module) -> list[type]:
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


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="output directory for fake_library_generated")
    parser.add_argument(
        "--pxd",
        required=True,
        nargs="+",
        help="paths to the bindings .pxd declaration files (the C++ mapping)",
    )
    args = parser.parse_args(argv)

    bindings = _load_bindings_module()

    api = {"classes": {}}
    for path_str in args.pxd:
        part = extract_api(pathlib.Path(path_str).read_text())
        api["classes"].update(part["classes"])
        print(f"parsed pxd: {len(part['classes'])} classes from {path_str}")

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
    returned_policies = {p["name"]: p["threading"] for p in returned_protos}
    protos = [extract_wrapper(svc, api=api, mapping=mapping) for svc in wrapper_classes]

    # policy enforcement: a pool wrapper may not return affine types at
    # all - drop them from the surface entirely.
    affine_bound = {name for name, pol in returned_policies.items() if pol == "affine"}
    for proto in protos:
        if proto["threading"] == "pool":
            before = len(proto["methods"])
            proto["methods"] = [m for m in proto["methods"] if m["return_type"] not in affine_bound]
            dropped = before - len(proto["methods"])
            if dropped:
                print(f"dropped {dropped} affine-returning method(s) from pool wrapper {proto['name']}")

    # Every name that gets an Async wrapper. Parameters typed with one of
    # these accept the wrapper as well as the sync binding object, and
    # the emitter widens their annotations accordingly.
    async_types = {p["name"] for p in returned_protos} | {p["name"] for p in protos}

    for proto in returned_protos:
        fname = f"async_{proto['name'].lower()}.py"
        code = ast.unparse(returned_module(proto, async_types))
        (out / fname).write_text(code + "\n")
        print(f"generated {fname} for returned type {proto['name']} ({proto['threading']})")

    for proto in protos:
        fname = f"async_{proto['name'].lower()}.py"
        code = ast.unparse(wrapper_module(proto, returned_policies, async_types))
        (out / fname).write_text(code + "\n")
        print(f"generated {fname} for {proto['name']} ({proto['threading']}, {len(proto['methods'])} methods)")

    all_names = [p["name"] for p in returned_protos] + [p["name"] for p in protos]
    (out / "__init__.py").write_text(ast.unparse(init_module(all_names)) + "\n")

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

    manifest = {
        "schema": 1,
        "wrappers": {p["name"]: p for p in protos},
        "returned_types": {p["name"]: p for p in returned_protos},
    }

    # No silent Any may survive into the artifact: a method whose types
    # never resolved is uncallable over the wire while looking alive
    # locally. Fail the build naming every offender; an explicit escape
    # hatch can be added when a legitimate case first appears.
    unresolved = []
    for group in ("wrappers", "returned_types"):
        for cls_name, proto in manifest[group].items():
            for m in proto["methods"]:
                for p in m["params"]:
                    if p["type"] == "Any":
                        unresolved.append(
                            f"{cls_name}.{m['name']} param {p['name']!r} (live annotation and pxd both silent)")
                if m["return_type"] == "Any":
                    unresolved.append(f"{cls_name}.{m['name']} return type")
    if unresolved:
        for u in unresolved:
            print(f"unresolved type: {u}", file=sys.stderr)
        sys.exit(1)

    # grpc_schema owns wire naming; stamping it into the manifest is what
    # lets the server and the client read the names instead of each
    # rebuilding the same convention from scratch.
    from codegen.grpc_schema import annotate, build_fdset
    annotate(manifest)

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"wrote manifest ({len(protos)} wrappers, {len(returned_protos)} returned types) "
        f"to {out / 'manifest.json'}"
    )

    (out / "grpc_schema.pb").write_bytes(build_fdset(manifest))
    print(f"wrote grpc_schema.pb to {out / 'grpc_schema.pb'}")

    shutil.copy(pathlib.Path(__file__).parent / "runtime.py", out / "_runtime.py")
    print(f"copied runtime into {out}")


if __name__ == "__main__":
    main()
