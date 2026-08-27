# Supporting adjacent Nix versions

**OPEN, and undecided on purpose.** Not tested, not implemented. This
records the design space while it is cheap to choose, because the
choice constrains the emitter and the wire.

## Problem

The bindings must build against several adjacent Nix versions. Small
API deltas exist between them: a renamed method, a member that moved,
a signature that gained a parameter.

nanopynix answers this in C++, with preprocessor macro ladders. That
answer does not transfer, and the reason is specific to this repo.

## Why the C++ answer does not transfer

nanopynix is bindings and nothing else, so a `#if NIX_VERSION >= ...`
costs it one ladder in one file.

Here, four generated surfaces sit ABOVE the bindings - the manifest,
the async wrappers and protocols, the RPC client, the gRPC schema -
and not one of them has a preprocessor. A `#ifdef` in the emitted C++
would leave every layer above describing a method that may or may not
exist, and the honest options there are the union (a stub promising a
method that raises AttributeError) or the intersection (a binding
built and then hidden). Both are lies a typechecker will repeat.

The declaration is Python and is never executed, so it can carry the
version fact as DATA and let the generator resolve it - which is the
thing the C++ route cannot do.

## The one real delta we have already hit

`store.py:180` carries it as prose:

    There is no getUri() any more. 2.34 moved it onto the config
    as getHumanReadableURI

Resolved today by pinning to 2.34.8 and hand-writing the new spelling
in a `@cxx_body`. That is the whole current strategy: support one
version, describe the other in a comment.

## Alternatives

**A. Resolve at GENERATE time.** The build reads
`pkg-config --modversion nix-store` - `setup.py` already shells out to
pkg-config - and passes it to the generator. A declaration states the
delta; the emitter picks one arm and writes plain C++.

    @cxx_name("getHumanReadableURI", since="2.34", before="getUri")
    def get_uri(self) -> Str: ...

    @since("2.30")
    def query_referrers(self, path: "StorePath") -> "list[StorePath]": ...

Emitted C++ has no conditional. The manifest, the stubs and the schema
describe exactly the version that was built.

**B. Resolve at COMPILE time.** The emitter writes the ladder.

    #if NIX_VERSION >= 23400
        .def("get_uri", &nix::Store::getHumanReadableURI)
    #else
        .def("get_uri", &nix::Store::getUri)
    #endif

One `.cpp` for the whole range. Every surface above it is wrong for at
least one version in that range.

**C. A version-conditional `@cxx_body`, and nothing else.** The status
quo, made explicit: the escape hatch takes the ladder, the census
counts it, and the surface never varies.

    @cxx_body("""#if NIX_VERSION >= 23400
        return s.config.getHumanReadableURI();
    #else
        return s.getUri();
    #endif""")

## Recommendation

**A, with B available inside a hatch as C describes.**

A is the only one where the four generated surfaces stay honest, and
it is the option the parse-don't-execute design already paid for: a
declaration can say `since="2.34"` about a C++ symbol this machine has
never compiled, and the reader costs a parse.

B is not wrong so much as unavailable above the bindings. Where a
delta is genuinely internal to one method body - same name, same
signature, different implementation - C is the right size, because
nothing above the binding can tell the difference and the census
already counts what it costs.

## What needs a decision

1. **Is the supported range a build input or a repo fact?** A range in
   `default.nix` lets one checkout target several; a single pinned
   version is what exists today and is simpler. A affects only the
   former.
2. **Does the WIRE have to be stable across versions?** This is the
   sharp one. If a client built against 2.34 talks to a server built
   against 2.30, a method absent on one side must not shift another
   method's proto field number. Field numbers are assigned in
   `grpc_schema.py`; making them stable across a version delta is a
   real constraint on it and worth knowing before A is built, not
   after (see 022 and 045).
3. **How far back?** Two adjacent versions and N are different
   problems: the second wants the `since`/`before` pair above, the
   first wants a range expression and a test matrix.

## Not doing yet

No implementation, no version matrix in CI, no second Nix checkout.
The cost of deciding late is that the emitter grows an assumption; the
cost of building early is a matrix nobody has asked for.
