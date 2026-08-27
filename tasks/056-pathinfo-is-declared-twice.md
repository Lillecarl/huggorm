# PathInfo is declared twice, and the two disagree

**HALF DONE.** The duplicate is gone: `decl/pathinfo.py`, which only
the corpus gate read, is deleted. PathInfo stays synthetic and stays
in `decl/store.py`.

**What is left is one coherent change, not several.** Binding
nix::ValidPathInfo, the move to `decl/pathinfo.py`, `store_dir`
joining the wire and the hand-written `_from_parts` are the same
decision. `_from_parts` cannot be written without `store_dir` on the
wire, so the wire moves with the type whatever the order - and
same-version-only makes that change free (055). A step that faked
`store_dir` to keep the surface still would be a lie in the
reconstruction path.

A synthetic value binds no Nix class, so "one Nix class, one file"
does not yet cover `cythonix::PathInfo` - it belongs to its producer,
beside `Store.query_path_info`. The file is EARNED when the type
becomes nix::ValidPathInfo, because path-info.hh is that type's
header.

**First step when it starts, before the real `_from_parts` is
finished:** write the bijection with one field deliberately dropped,
run the round-trip gate, watch it fail NAMING the field, restore. The
gate has never been seen to fail because nothing yet can make it -
and if some other test catches the dropped field first, then the
gate's failure mode is shadowed and that is worth learning before
`_from_parts` exists rather than after.

**Originally.** Found by Carl asking why `@cxx_parts` exists at all.

## Problem

One C++ type, `nix::ValidPathInfo`, has two declarations with
different surfaces. Only one is built.

    decl/store.py     PathInfo         SHIPPED (in NANOBIND)
    decl/pathinfo.py  ValidPathInfo    read only by gates/nbcheck.py

| method | store.py | pathinfo.py |
| --- | --- | --- |
| `store_path()` | absent | `StorePath` |
| `path()` | `StorePath` | `str`, store dir joined on |
| `deriver()` | `StorePath \| None` | `str \| None` |

Three methods differ in TYPE and one exists in only one of them. This
is the F1 and F7a shape - one fact stated twice, and something reading
the wrong statement - at the scale of a whole type.

`decl/pathinfo.py` currently describes a `path()` this repo does not
ship. It is measured against nanopynix by the spike gate, so the gate
is judging a surface nobody gets.

## Why there are two

`decl/store.py` uses `@cxx_parts`: the emitter declares a POD struct
`cythonix::PathInfo` whose members ARE the wire types, and binds that.
`decl/pathinfo.py` binds `nix::ValidPathInfo` itself.

Reading the real type is easy - pathinfo.py proves it, four accessors
derived and seven hatched. The blocker is the far side of the wire.

`nix::ValidPathInfo` (path-info.hh):

- `struct ValidPathInfo : virtual UnkeyedValidPathInfo` - a virtual
  base, so it is NOT an aggregate and cannot be brace-initialised;
- no default constructor;
- the only constructors are `ValidPathInfo(StorePath,
  UnkeyedValidPathInfo)` and `UnkeyedValidPathInfo(storeDir, Hash)`,
  with every other field assigned afterwards;
- the wire carries `nar_hash` RENDERED (`sha256:<base32>`) and `ca`
  RENDERED, so rebuilding means parsing both back into `nix::Hash` and
  `nix::ContentAddress`;
- `storeDir` is not on the wire at all, and `UnkeyedValidPathInfo`
  requires it.

Against the synthetic struct, where `_from_parts` is one line:

    return cythonix::PathInfo{path, nar_hash, nar_size, deriver,
                              registration_time, ultimate, ca,
                              references, sigs};

So `@cxx_parts` buys a trivial round trip and pays a nine-field copy
on every `query_path_info`, plus this duplicate.

## Alternatives

**1. Keep the synthetic struct.** Delete `decl/pathinfo.py` or align
it. Cost: the copy stays, and `path()` keeps answering a `StorePath`
where `nix path-info` prints a full path.

**2. Bind `nix::ValidPathInfo`, re-parse in `_from_parts`.** A helper
in `_cpp/` doing `nix::Hash::parseAny` and
`nix::ContentAddress::parse`, plus `storeDir` joining the wire. That
is real logic, so it is a decision and belongs in `_cpp/` by the rule
already written there. Roughly six lines.

**3. Bind directly and carry unrendered parts.** `nix::Hash` becomes
its own message. Bigger wire change, and it makes the wire describe
Nix's types rather than a caller's.

**4. Real type locally, synthetic remotely.** Two types for one
surface. The repo refuses this everywhere else and should here.

## Recommendation

Decide 1 or 2 - and either way, **remove the duplicate first**. A
declaration that describes a surface we do not ship is worse than no
declaration, because the spike gate is measuring it.

Leaning 2: `path()` answering the string `nix path-info` prints is
what a caller expects, `store_path()` beside it gives the narrow type,
and the copy goes away. But it needs `storeDir` on the wire, which is
a schema change, so it wants doing before the schema matters.

## Reviewed, 2026-08-27

cython-reviewer's verdict on the no-`@cxx_parts` sketch in
`.scratchpad/no-cxx-parts/`: the direction is right and `@cxx_parts`
should go. Two conditions.

**A generated round-trip gate per wire value, mandatory.**
`@cxx_parts` refused a field map that missed a field or invented one.
A hand-written `_from_parts` that forgets `ultimate` COMPILES and
zero-inits silently, so the bug comes back through the body. Once
fidelity is a by-hand bijection rather than a struct copy, the round
trip must be PROVEN: build one, encode, decode, compare parts.

**`path()` stays a StorePath.** The first sketch renamed it to
`store_path()` and gave `path()` a rendered string - copied from
`decl/pathinfo.py`, the declaration this repo does not ship. Upstream
`ValidPathInfo::path` is a StorePath member called `path`, four tests
call `info.path().to_string()`, and 040/042 already argue that a
StorePath is a name while rendering is a separate act.

Also settled there: one declaration file per Nix class, but the
EXTENSION grain stays per header - several files feed one translation
unit, and generate.py's list becomes a mapping. `Cxx` in a body is
what distinguishes a derived binding from a hatched one. The fields
tuple survives for SELECTION and ORDER only.

## Surface changes this would make, listed rather than silent

- `store_dir` appears - UnkeyedValidPathInfo's constructor needs it
  and nothing else carries it. New accessor, new wire field.
- `registration_time` becomes `I64 | None` where today it is `I64`
  with 0 meaning unknown.

## Not yet decided

Whether the wire should carry rendered or structured values in
general. 056 is one instance of that question; `sigs`, `ca` and
`nar_hash` are all rendered today.
