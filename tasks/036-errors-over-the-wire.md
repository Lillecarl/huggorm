# A typed error has to survive the wire

Carl, 2026-08-25: "We need to build some kind of conversion that
converts exceptions to something that can travel over gRPC."

## Where it stands

In-process, a Nix failure is now typed and clean: `BadStorePathName`
under `BadStorePath` under `NixError`, with `str(e)` stripped and
`e.colored` holding what libstore wrote (tasks/015).

None of that survives a round trip. The path an error takes today:

- the server catches anything that is not a `WrapperError` and wraps
  it in `InternalError(message, cause=e)`;
- `InternalError.to_dict()` writes `cause_type` (the class NAME) and
  `cause_message` into JSON, which crosses in the gRPC status;
- `WrapperError.from_dict()` rebuilds the cause by looking that name
  up in a hard-coded map of five builtins - ValueError, TypeError,
  RuntimeError, KeyError, OSError - and falls back to `Exception`.

So a `BadStorePath` arrives as a plain `Exception` carrying the right
message and the wrong type, and `except BadStorePath` on the client
catches nothing. `e.colored` is gone entirely.

## Why the obvious fix is not obviously right

Add the nix errors to that map, and the GENERATED runtime imports from
the bindings to do it. It does not import from them today, on purpose:
`_runtime.py` is emitted beside the wrappers and knows nothing about
which library it is wrapping. A hard-coded list of Nix exception names
in it is the same duplication the wire policy exists to remove - add
an error type and you edit a file three layers up.

## Directions, roughly in order of preference

- **Declare the hierarchy, the way everything else here is declared.**
  The bindings own `errors.py`; the manifest could carry the class
  names and their bases, and the runtime could rebuild the hierarchy
  from that at import. Errors then reach the wire the way types do,
  and adding one means editing the bindings and nothing else.
- **Send the module path, not just the class name.** `cause_type`
  becomes `cythonix_bindings.errors.BadStorePath` and the client
  imports it. Smaller, and it works for any library - but it lets a
  message name any importable class, which is a capability nobody
  asked for.
- **A structured error message instead of JSON in the status.** The
  gRPC status has room for typed details, and this repo already has a
  schema builder. Bigger, and it fixes the shape rather than the
  lookup.

Whichever way, `colored` has to ride along: it is the field that
exists for the caller's terminal, and the caller with a terminal is
usually the remote one.

## Test that would catch it

`test_remote.test_a_decoded_cause_survives_the_wire` already asserts
this for ValueError. The same test against a real binding failure is
the whole task in one line.
