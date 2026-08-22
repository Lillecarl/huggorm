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

from codegen.emitter import service_module, init_module
from codegen.model import extract_service, extract_errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="output directory for fake_library_generated")
    parser.add_argument("--spec", default="spec", help="spec module name (default: spec)")
    parser.add_argument(
        "--spec-dir",
        default=None,
        help="directory containing the spec module (default: generator's parent)",
    )
    args = parser.parse_args()

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

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    protos = [extract_service(svc) for svc in services]
    for proto in protos:
        fname = f"async_{proto['service'].lower()}.py"
        code = ast.unparse(service_module(proto))
        (out / fname).write_text(code + "\n")
        print(f"generated {fname} for {proto['service']} ({proto['threading']})")

    names = [p["service"] for p in protos]
    (out / "__init__.py").write_text(ast.unparse(init_module(names)) + "\n")

    manifest = {
        "schema": 1,
        "services": {p["service"]: p for p in protos},
        "errors": extract_errors(spec_mod),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"wrote manifest ({len(protos)} services, {len(manifest['errors'])} errors) "
        f"to {out / 'manifest.json'}"
    )

    shutil.copy(spec_dir / f"{args.spec}.py", out / "spec.py")
    shutil.copy(pathlib.Path(__file__).parent / "runtime.py", out / "_runtime.py")
    print(f"copied IDL spec and runtime into {out}")


if __name__ == "__main__":
    main()
