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
    extract_wrapper,
    returned_types_from_api,
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

    wrapper_classes = _wrapper_classes(bindings)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    returned_classes = returned_types_from_api(api, bindings)
    returned_set = set(returned_classes)
    wrapper_classes = [c for c in wrapper_classes if c not in returned_set]
    if not wrapper_classes:
        print("no constructible wrapper classes found", file=sys.stderr)
        sys.exit(1)

    returned_protos = [
        extract_wrapper(kls, api=api, bindings=bindings) for kls in returned_classes
    ]
    returned_policies = {p["name"]: p["threading"] for p in returned_protos}

    for proto in returned_protos:
        fname = f"async_{proto['name'].lower()}.py"
        code = ast.unparse(returned_module(proto))
        (out / fname).write_text(code + "\n")
        print(f"generated {fname} for returned type {proto['name']} ({proto['threading']})")

    protos = [extract_wrapper(svc, api=api, bindings=bindings) for svc in wrapper_classes]

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

    for proto in protos:
        fname = f"async_{proto['name'].lower()}.py"
        code = ast.unparse(wrapper_module(proto, returned_policies))
        (out / fname).write_text(code + "\n")
        print(f"generated {fname} for {proto['name']} ({proto['threading']}, {len(proto['methods'])} methods)")

    all_names = [p["name"] for p in returned_protos] + [p["name"] for p in protos]
    (out / "__init__.py").write_text(ast.unparse(init_module(all_names)) + "\n")

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

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"wrote manifest ({len(protos)} wrappers, {len(returned_protos)} returned types) "
        f"to {out / 'manifest.json'}"
    )

    from codegen.grpc_schema import build_fdset
    (out / "grpc_schema.pb").write_bytes(build_fdset(manifest))
    print(f"wrote grpc_schema.pb to {out / 'grpc_schema.pb'}")

    shutil.copy(pathlib.Path(__file__).parent / "runtime.py", out / "_runtime.py")
    print(f"copied runtime into {out}")


if __name__ == "__main__":
    main()
