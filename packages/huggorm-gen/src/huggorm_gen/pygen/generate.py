"""
CLI: read the declarations, emit the package.

Glue only — the model comes from `huggorm_gen.cppgen`, its rules live
in `huggorm_gen.contracts`, and emission lives in emitter.py. Installed as the
`codegen-generate` entry point.
"""

import argparse
import ast
import copy
import pathlib
import sys
from typing import Any

from huggorm_decl import corpus
from huggorm_dsl import declare
from huggorm_gen import contracts
from huggorm_gen.cppgen.generate import (
    declared_entries,
    declared_enums,
    declared_errors,
    declared_functions,
    declared_model,
    declared_returned,
    declared_unions,
)
from huggorm_gen.pygen import surface
from huggorm_gen.pygen.emitter import (
    FREE_MODULE,
    STUB_PACKAGE,
    emitter_protocol_names,
    emitter_union_names,
    free_function_module,
    init_module,
    policy_module,
    protocol_module,
    returned_module,
    rpc_module,
    stub_package,
    unions_module,
    wrapper_module,
)
from huggorm_gen.pygen.grpc_schema import annotate, build_fdset

# One class, method or function as a plain dict.
Proto = dict[str, Any]


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


def build_manifest() -> Proto:
    """Everything the build decides, as a value.

    This used to be the first three hundred lines of `main`, ending
    in a `manifest.json` that other things read back. The
    serialisation bought nothing and cost the usual: a second shape to
    keep in step, a version stamp to check, and a `dict[str, Any]` at
    every boundary that touched it.

    A function instead. The emitters below take the value; so does
    the suite, which held the emitted code against the JSON and can
    now hold it against the same derivation the emitters use.

    Nothing here writes a file, and that is the point of the split:
    the three async-wrapper emissions used to happen in the middle of
    this, so there was no moment at which the build's decisions were
    complete and nothing had been written yet.
    """
    # Which annotation names are ALIASES, told before anything is
    # emitted. The emitter distinguishes a union from a bound class
    # when it writes an import - one comes from huggorm_bindings and
    # the other from the generated `_unions` - and there is no way to
    # tell them apart from a name alone. Set here rather than beside
    # the manifest, because the wrapper modules are written first.
    emitter_union_names(declared_unions())

    # Every declared class, by name. The reflected version asked the
    # compiled package for classes carrying a threading policy; every
    # declared class has one, and the two sets matched exactly.
    declared: dict[str, Proto] = declared_entries()
    wrapper_names = sorted(declared)

    # Which classes are HANDED BACK rather than constructed. The
    # declaration says, and it is the only thing that could: this was
    # a walk over the pxd's return types.
    returned_names = sorted(declared_returned())
    returned_set = set(returned_names)
    wrapper_names = [n for n in wrapper_names if n not in returned_set]
    if not wrapper_names:
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
    print(f"declared entries: {len(declared)} class(es) - "
          + ", ".join(sorted(declared)))

    def _proto(name: str) -> Proto:
        """One declared class, by name.

        A COPY. `_proto` is called twice for every class - once for
        the wrappers and once for the stubs - and the later passes
        write keys onto the wrappers' dicts in place. The stubs
        describe the BINDING and must not see them."""
        want = declared.get(name)
        if want is None:
            # Not a fallback. Every name reaching this comes from the
            # declaration set, so a miss means two derivations of the
            # same set disagree - and guessing a surface is how a
            # wrong answer reaches four generated files at once.
            raise SystemExit(f"{name} is named as a class but no "
                             f"declaration describes it")
        print(f"  {want['name']}: from the declaration")
        return copy.deepcopy(want)

    returned_protos = [_proto(n) for n in returned_names]
    protos = [_proto(n) for n in wrapper_names]

    unwrapped = sorted(p["name"] for p in protos + returned_protos
                       if not p["wrapped"])
    if unwrapped:
        print(f"not wrapped (pool and non-blocking, so nothing to wrap): "
              f"{', '.join(unwrapped)}")

    # The async spelling of a type, when the LANGUAGE gives one.
    #
    # From the vocabulary, not from a marker on the bindings package.
    # `Path` is `Annotated[pathlib.Path, Cxx("string"),
    # Async("anyio.Path")]`, so the two spellings of one word sit
    # together and neither file repeats the other's half.
    async_twins: dict[str, str] = declare.twins()

    # A free function comes from the declaration where there is one,
    # and from reflection where there is not - the same rule the
    # classes follow one screen up, and for the same reason. A
    # nanobind function is a builtin: `inspect.signature` refuses it,
    # so there is nothing to reflect.
    declared_fns = declared_functions()
    free_protos = [declared_fns[n] for n in sorted(declared_fns)]
    from_decl = sorted(declared_fns)
    if from_decl:
        print(f"free functions from the declaration: "
              f"{', '.join(from_decl)}")
    unwrapped_free = [p["name"] for p in free_protos if not p["wrapped"]]
    if unwrapped_free:
        print(f"free functions with no threading policy, so no wrapper: "
              f"{', '.join(unwrapped_free)}")
    enums = declared_enums()
    unions = declared_unions()
    errors = declared_errors()
    # Round-trip probes, not surface.
    for proto in protos + returned_protos:
        proto.pop("_helpers", None)

    manifest: Proto = {
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

    # grpc_schema owns wire naming; stamping it into the manifest is what
    # lets the server and the client read the names instead of each
    # rebuilding the same convention from scratch.
    annotate(manifest, declared_model())
    surface.annotate(manifest)

    return manifest


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
    manifest = build_manifest()
    protos = list(manifest["wrappers"].values())
    returned_protos = list(manifest["returned_types"].values())
    # Read back off the manifest rather than threaded out of the
    # derivation. Every one of these WAS a local up there, and passing
    # six of them across the split would have made the boundary a
    # tuple nobody could read.
    model = declared_model()
    emitter_protocol_names({c.protocol_name for c in model.ordered_served})
    unions = manifest["unions"]

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

    free_names = sorted(f.name for f in model.functions.values() if f.wrapped)
    if free_names:
        (out / f"{FREE_MODULE}.py").write_text(ast.unparse(
            free_function_module(model)) + "\n")
        print(f"generated {FREE_MODULE}.py for {len(free_names)} free "
              f"function(s): {', '.join(free_names)}")

    ordered = surface.order(manifest)
    (out / f"{surface.PROTOCOL_MODULE}.py").write_text(
        ast.unparse(protocol_module(declared_model())) + "\n")
    (out / f"{surface.RPC_MODULE}.py").write_text(
        ast.unparse(rpc_module(declared_model())) + "\n")
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

    # No manifest.json. It was a serialisation of `build_manifest()`,
    # and every reader calls the function instead - the emitters here,
    # and the suite, which used to hold the emitted code against a
    # second artifact of the same build (065).
    print(f"derived {len(protos)} wrapper(s) and {len(returned_protos)} "
          f"returned type(s)")

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
    (out / "_unions.py").write_text(unions_module(unions))
    # ...and the wire policy of every declared type, which the codec
    # reads and no caller does.
    (out / "_policy.py").write_text(policy_module(declared_model()))
    print(f"generated _unions.py for {len(unions)} sum type(s): "
          f"{', '.join(unions) or 'none'}")
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
