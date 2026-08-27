# PathInfo is declared twice, and the two disagree

**OPEN.** Found by Carl asking why `@cxx_parts` exists at all.

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

## Not yet decided

Whether the wire should carry rendered or structured values in
general. 056 is one instance of that question; `sigs`, `ca` and
`nar_hash` are all rendered today.
