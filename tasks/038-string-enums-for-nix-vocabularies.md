# The vocabularies Nix parses should be types, not bare strings

Carl, 2026-08-25: "I'd like to make a stringenum for things that are
stringenums if possible without ruining codegen."

## Where it stands

`Store.add_to_store` takes `method: str` and `hash_algo: str`. Both are
closed sets that libstore parses - `flat`, `nar`, `git`, `text` and
`blake3`, `md5`, `sha1`, `sha256`, `sha512` - and a Python developer
calling this has nothing to tell them so. An invented value fails at
the call, with a good message, which is the wrong end of the loop: an
editor should have offered the four.

## Why a StrEnum is the right shape

A `StrEnum` member IS a str at runtime. So NOTHING about the transport
changes: the field stays a protobuf `string`, the codec still sends a
string, and no schema or wire question arises. The whole cost is the
codegen recognising the type name.

That is also why it beats `Literal`: the value is still a plain string
wherever one is wanted, but there is a class to attach the vocabulary
to, and a decoded value can come back TYPED rather than as a bare str.

## What it touches

Discovery needs no new declaration, unlike `_errors_module`. The
classes live in the bindings package and are found the way wrapper
classes are - walk `dir(cythonix_bindings)` for `type` objects that
subclass both `str` and `Enum`. So adding a vocabulary means adding it
to the bindings and nothing else.

From there it is one table in the manifest and four readers:

- `grpc_schema._msg_arg_type` and `wire_blocker`: an enum name is the
  `string` scalar.
- `wire.py`'s `kind()`: scalar. Its converter should be the enum CLASS
  rather than `str`, so a value decoded from the wire arrives typed.
  `str(member)` is the value on the way out, so encoding needs nothing.
- the emitter: already works. `_cythonix_bindings_import` emits
  `from cythonix_bindings import X` for any annotated name that is not
  a builtin, so exporting the enum from `__init__` is enough.
- `stub_module`: **this is the gap.** Its `foreign` map is built from
  `home`, which holds only wrapper and returned-type modules. An enum
  is in neither, so the stub would NAME the type and not import it.
  Whatever fix this gets should cover any bindings-level type that is
  not a wrapper - the enums are just the first.

## Checking the values are Nix's

A TEST, not a build-time shim. Parametrize over each member and push it
through `add_to_store` against the chroot store: a real round trip
through libstore, hermetic, and it needs no new C++ and no new
declaration.

It cannot catch a member Nix ADDS. C++ has no reflection, so any list
of enum members here is hand-written whether it lives in Python or in a
shim - the check catches a typo, a rename and a removal, and that is
what it should claim.

## Naming

`ContentAddressMethod` and `HashAlgorithm`, matching the C++ types
`addToStoreFromDump` takes. No `Nix` prefix.
