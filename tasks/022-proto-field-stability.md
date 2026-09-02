# Proto field numbers are positional and unstable

**OPEN.** Field numbers are still positional. There is no lockfile and
nothing reads the manifest's `"schema": 1`.

Found in the 2026-08-25 review. Low priority while everything is
built together; a correctness trap the moment it is not.

## Problem

grpc_schema numbers request fields by position: `self` is 1, then
parameters in declaration order from 2. Reordering a method's
parameters, or inserting one, renumbers every field after it.
Wire-value message fields are numbered the same way from
_wire_fields order.

Nothing detects this. Client and server are built from one manifest in
one Nix build, so they always agree with each other - and would agree
just as happily on a schema incompatible with yesterday's.

## Why it matters

The whole point of emitting a FileDescriptorSet is that OTHER things
consume it: grpcurl today, protoc-generated clients in other languages
tomorrow, a persisted evaluation server (016) whose clients were built
last week. All of them break silently on renumbering - proto3 decodes
a wrong-typed field as a default rather than an error.

## Direction

Options, roughly in order of preference:

- Check in a lockfile mapping (message, field) -> number, generated on
  first sight and never reassigned. New fields take the next free
  number; a removed field's number is reserved. Build fails when the
  emitted schema contradicts the lock. This is what protobuf users do
  by hand, made automatic.
- Derive numbers from a stable hash of the field name, with collision
  resolution. No file to keep, but ugly numbers and a rehash on
  rename.
- Declare numbers explicitly in the pyx alongside _wire_fields. Honest
  and manual; scales badly to a real Nix surface.

The lockfile also gives the schema a diff a human can review, which
the .pb blob does not.
