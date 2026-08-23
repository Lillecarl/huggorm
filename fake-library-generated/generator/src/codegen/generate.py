"""
CLI: load the spec, emit the package, write the manifest.

Glue only — extraction lives in model.py, emission in emitter.py.
Installed as the `codegen-generate` entry point.
"""

import argparse
import ast
import importlib
import json
import pathlib
import shutil
import sys

from codegen.emitter import service_module, bound_module, init_module
from codegen.model import (
    extract_service,
    extract_errors,
    inherited_from_pxd,
    bound_types_from_pxd,
)
from codegen.pxd import extract_api


def _load_bindings_module():
    import fake_library

    return fake_library


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="output directory for fake_library_generated")
    parser.add_argument("--spec", default="spec", help="spec module name (default: spec)")
    parser.add_argument(
        "--spec-dir",
        default=None,
        help="directory containing the spec module (default: cwd)",
    )
    parser.add_argument(
        "--pxd",
        default=None,
        help="path to the bindings .pxd declaration file; enables pxd-derived "
        "inherited exposure with full typing",
    )
    args = parser.parse_args(argv)

    if args.spec_dir is not None:
        spec_dir = pathlib.Path(args.spec_dir).resolve()
    else:
        spec_dir = pathlib.Path.cwd()
    if str(spec_dir) not in sys.path:
        sys.path.insert(0, str(spec_dir))

    spec_mod = importlib.import_module(args.spec)
    services = getattr(spec_mod, "SERVICES", [])
    if not services:
        print(f"No SERVICES found in {args.spec}", file=sys.stderr)
        sys.exit(1)

    bindings = _load_bindings_module()

    api = None
    if args.pxd:
        api = extract_api(pathlib.Path(args.pxd).read_text())
        print(f"parsed pxd: {len(api['classes'])} classes from {args.pxd}")

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if api is not None:
        bound_classes = bound_types_from_pxd(api, bindings)
    else:
        bound_classes = []
    bound_protos = [extract_service(kls) for kls in bound_classes]
    bound_policies = {p["service"]: p["threading"] for p in bound_protos}

    for proto in bound_protos:
        fname = f"async_{proto['service'].lower()}.py"
        code = ast.unparse(bound_module(proto))
        (out / fname).write_text(code + "\n")
        print(f"generated {fname} for returned type {proto['service']} ({proto['threading']})")

    protos = []
    for svc in services:
        inherited = inherited_from_pxd(svc, api, bindings) if api is not None else None
        hide = getattr(svc, "_hide", ())
        protos.append(extract_service(svc, inherited_methods=inherited, hide=hide))

    # re-filter using bound policy knowledge: a pool service may not
    # return affine types at all - hide them from the surface entirely.
    affine_bound = {name for name, pol in bound_policies.items() if pol == "affine"}
    for svc, proto in zip(services, protos):
        if proto["threading"] == "pool":
            before = len(proto["methods"])
            proto["methods"] = [m for m in proto["methods"] if m["return_type"] not in affine_bound]
            dropped = before - len(proto["methods"])
            if dropped:
                print(f"dropped {dropped} affine-returning method(s) from pool service {proto['service']}")

    for proto in protos:
        fname = f"async_{proto['service'].lower()}.py"
        code = ast.unparse(service_module(proto, bound_policies))
        (out / fname).write_text(code + "\n")
        print(f"generated {fname} for {proto['service']} ({proto['threading']}, {len(proto['methods'])} methods)")

    all_names = [p["service"] for p in bound_protos] + [p["service"] for p in protos]
    (out / "__init__.py").write_text(ast.unparse(init_module(all_names)) + "\n")

    manifest = {
        "schema": 1,
        "services": {p["service"]: p for p in protos},
        "bound_types": {p["service"]: p for p in bound_protos},
        "errors": extract_errors(spec_mod),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"wrote manifest ({len(protos)} services, {len(bound_protos)} bound types, "
        f"{len(manifest['errors'])} errors) to {out / 'manifest.json'}"
    )

    shutil.copy(spec_dir / f"{args.spec}.py", out / "spec.py")
    shutil.copy(pathlib.Path(__file__).parent / "runtime.py", out / "_runtime.py")
    print(f"copied IDL spec and runtime into {out}")


if __name__ == "__main__":
    main()
