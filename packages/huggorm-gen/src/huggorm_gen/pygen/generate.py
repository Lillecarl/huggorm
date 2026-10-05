"""
CLI: read the declarations, emit the package.

Glue only — the model comes from `huggorm_gen.cppgen`, its rules live
in `huggorm_gen.contracts`, and emission lives in emitter.py. Installed as the
`codegen-generate` entry point.
"""

import argparse
import ast
import pathlib
import sys

from huggorm_decl import corpus
from huggorm_gen import contracts, ir
from huggorm_gen.cppgen.generate import declared_model
from huggorm_gen.pygen.emitter import (
    FREE_MODULE,
    PROTOCOL_MODULE,
    RPC_MODULE,
    STUB_PACKAGE,
    free_function_module,
    init_module,
    policy_module,
    protocol_module,
    returned_module,
    rpc_module,
    stub_package,
    unions_module,
    wrapped_functions,
    wrapper_module,
)
from huggorm_gen.pygen.fmt import format_paths
from huggorm_gen.pygen.grpc_schema import build_fdset

# Nothing here imports huggorm_bindings.
#
# Four functions stood in this space: `_load_bindings_module`,
# `_wrapper_classes`, `_enum_classes` and `_free_functions`. Each
# asked the compiled package a question the declaration answers -
# which classes carry a threading policy, which are string
# vocabularies, which module-level names are functions - so every
# Python surface waited on a C++ compiler for facts a person wrote
# in `decl/`.
#
# They agreed with the declaration exactly, which is why they could
# go: the reflected wrapper set, enum set and function set each
# matched their `declared_*` counterpart name for name.
#
# One of them was already dead. `_wrapper_classes` excluded a class
# whose `_async` was False, and nothing has emitted `_async` since
# the mock was deleted.


# The payload modules, copied into the package as written.
VENDORED = {"_runtime.py", "_wiretypes.py", "_callspec.py"}


def _vendor(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Copy a generator module into the emitted package, WRITABLE.

    `shutil.copy` carries the source's MODE. This generator is
    installed into the Nix store, where every file is read-only, so a
    copied file arrives read-only too and the next run cannot
    overwrite it.

    There is always a next run. setuptools calls the build backend
    twice - once for metadata and once for the wheel - and setup.py
    generates on import, so the second pass writes over the first.
    Writing the bytes leaves the destination's own mode alone, which
    makes the generator idempotent over its own output.
    """
    dst.write_bytes(src.read_bytes())


def main(argv: list[str] | None = None) -> None:
    """The build: derive once, then emit."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True,
                        help="output directory for huggorm_generated")
    args = parser.parse_args(argv)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Every refusal this build can see, before it derives anything
    # (huggorm#61).
    corpus().read_all()
    # ...and every rule the typed model must obey, before anything is
    # written.
    broken = contracts.complaints(declared_model())
    for rule, why in broken:
        print(f"{rule}: {why}", file=sys.stderr)
    if broken:
        sys.exit(1)
    model = declared_model()

    # The in-process wrappers. Emitted here rather than mid-derivation:
    # a contract that fails now fails before any file is written.
    #
    # Served, not wrapped: every proxy gets its async form, because a
    # handle the server adopts needs an Async class behind it whether
    # or not the calls hop threads. The threading policy travels with
    # the proto, so a pool class keeps pool execution - serving is
    # addressability, not affinity.
    for cls in model.ordered_served:
        fname = f"async_{cls.name.lower()}.py"
        emit = (returned_module if cls.name in model.returned
                else wrapper_module)
        (out / fname).write_text(ast.unparse(emit(model, cls)) + "\n")
        print(f"generated {fname} for {cls.name} ({cls.decl.threading})")

    free_names = wrapped_functions(model)
    if free_names:
        (out / f"{FREE_MODULE}.py").write_text(ast.unparse(
            free_function_module(model)) + "\n")
        print(f"generated {FREE_MODULE}.py for {len(free_names)} free "
              f"function(s): {', '.join(free_names)}")

    (out / f"{PROTOCOL_MODULE}.py").write_text(
        ast.unparse(protocol_module(model)) + "\n")
    (out / f"{RPC_MODULE}.py").write_text(
        ast.unparse(rpc_module(model)) + "\n")
    print(f"generated {PROTOCOL_MODULE}.py and {RPC_MODULE}.py for "
          f"{len(model.ordered_served)} class(es)")
    (out / "__init__.py").write_text(ast.unparse(init_module(model)) + "\n")

    # Type stubs for the bindings themselves (huggorm#27). The bindings
    # are compiled extensions, so a typechecker reads no signatures out
    # of them and every binding type resolves to Any - which made the
    # generated protocols name types that check nothing.
    #
    # A PEP 561 stub-only package, because a .pyi has to sit beside the
    # module it describes and the bindings are built and installed
    # before this runs. The generated package cannot write into them.
    stub_dir = out.parent / STUB_PACKAGE
    stub_dir.mkdir(parents=True, exist_ok=True)
    stubs = stub_package(model)
    for fname, stub in stubs.items():
        (stub_dir / fname).write_text(ast.unparse(stub) + "\n")
    # PARTIAL, and the word is load-bearing. A stubs package normally
    # REPLACES the runtime one for a typechecker, so a hand-written
    # Python module in the bindings - errors.py - would vanish behind
    # stubs that never mention it. Partial says "fall back to the real
    # package for anything not stubbed here", which is exactly right:
    # these stubs exist because a compiled extension carries no
    # signatures, and a .py file needs no help.
    (stub_dir / "py.typed").write_text("partial\n")
    modules = sorted(f.removesuffix(".pyi") for f in stubs
                     if f != "__init__.pyi")
    print(f"generated {STUB_PACKAGE}/ for {len(modules)} binding module(s): "
          + ", ".join(modules))

    print(f"derived {len(model.constructed)} constructed and "
          f"{len(model.handed_back)} returned class(es)")
    # A call with no rpc keeps its in-process form and leaves the
    # protocol, which is a quiet change unless the build says it.
    for fn in model.functions.values():
        for why in model.function_blockers(fn):
            print(f"warning: {fn.name} has no RPC surface - {why}")
    for c in model.ordered_served:
        for m in c.methods:
            for why in ir.blockers(m.params, m.returns, model.served):
                print(f"warning: {c.name}.{m.name} has no RPC surface "
                      f"and is not on the protocol - {why}")

    (out / "grpc_schema.pb").write_bytes(build_fdset(model))
    print(f"wrote grpc_schema.pb to {out / 'grpc_schema.pb'}")

    # The payload ships rather than runs, so it lives beside the
    # two backends rather than inside either one.
    here = pathlib.Path(__file__).resolve().parent.parent / "payload"
    _vendor(here / "runtime.py", out / "_runtime.py")
    # ...and the SUM types, which have no home in the bindings: an
    # alias is Python and the module binding its arms is a compiled
    # extension. Written from the manifest, so the declaration states
    # `DerivedPath = StorePath | DerivedPathBuilt` once.
    (out / "_unions.py").write_text(unions_module(model.unions))
    # ...and the wire policy of every declared type, which the codec
    # reads and no caller does.
    (out / "_policy.py").write_text(policy_module(model))
    print(f"generated _unions.py for {len(model.unions)} sum type(s): "
          f"{', '.join(model.unions) or 'none'}")
    # Not the payload: it is hand-written and keeps its source's layout.
    # By file, because setuptools runs this twice and the second run
    # finds the first run's copy in `out`.
    format_paths(stub_dir, *(f for f in out.glob("*.py") if f.name not in VENDORED))
    # The codec reads declared type strings at run time and the schema
    # builder reads them at build time. One definition, copied, rather
    # than two that agree until one of them changes.
    _vendor(here / "wiretypes.py", out / "_wiretypes.py")
    # ...and the call spec's types, which the emitted RPC classes
    # build and the hand-written client reads.
    _vendor(here / "callspec.py", out / "_callspec.py")
    print(f"copied runtime into {out}")

    # PEP 561: without this marker a typechecker skips an INSTALLED
    # package entirely, however well annotated it is. The whole point
    # of the protocols is that a consumer can be checked against them,
    # and a consumer imports the installed package.
    (out / "py.typed").write_text("")


if __name__ == "__main__":
    main()
