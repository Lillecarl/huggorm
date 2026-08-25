# Generate .pyi stubs for the bindings

Found 2026-08-25 while verifying 017 with mypy --strict.

## The gap

The bindings ship as compiled extension modules with no stubs:

    fake_library/store.cpython-314-x86_64-linux-gnu.so
    fake_library/eval.cpython-314-x86_64-linux-gnu.so

mypy cannot read a .so, so every binding type is `Any`:

    reveal_type(StorePath)        -> Any
    reveal_type(s.is_valid_path)  -> def (path: Any) -> Coroutine[..., bool]

Scalars are checked; binding types are not. Concretely, out of five
deliberate mistakes made through a protocol-typed parameter, mypy
caught four and missed exactly one:

    await store.total_nonsense()                  caught
    await store.add_text_to_store("only-one")     caught
    x: int = await store.get_uri()                caught
    await store.query_derivation()                caught (not on the base)
    await store.is_valid_path("not-a-storepath")  MISSED

The last one is missed because `StorePath` is `Any`, so any argument
satisfies it. The protocol layer is doing its job; the type it names
is empty.

## Why this belongs here

It is the same job the repo already does, one level down. The
generator knows every binding class, its methods, their parameter
names and types, its constructor signature (from the pxd, the only
place it exists) and its docstrings - that is what the manifest IS.
Emitting store.pyi and eval.pyx's stubs from the same protocol dicts
is a fourth emitter next to the wrappers, the protocols and the RPC
classes.

It also closes a loop: today the manifest's types are checked against
the pxd and against the live surface, but nothing checks that a
CONSUMER of the sync bindings is passing the right thing.

## Fix sketch

- emitter: `stub_module(proto)` -> a .pyi per binding module, from the
  protocol dicts already built.
- Cython can emit stubs itself (`annotation_typing` / `--annotate`);
  worth checking whether its output is good enough before writing one.
  If it is, the job becomes wiring it into the build - but note that
  Cython does not know the pxd-derived constructor signatures, and
  those are the ones inspect cannot see either (019).
- Install the .pyi next to each .so, plus a py.typed marker.
- Then a mypy run over a consumer is a real check, and 013 can point
  it at the demos.

## Blocked on nothing

Independent of 013, which is about adding the tools. This is about
giving them something to read.
