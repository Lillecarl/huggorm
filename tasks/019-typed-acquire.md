# Acquire should take typed constructor arguments

Found in the 2026-08-25 review.

## Problem

server._acquire_able guesses constructibility from
`inspect.signature(cls.__init__)` and admits any class with no
required positional parameters. Two things go wrong.

Cython cdef classes put real construction in __cinit__, and __init__
may be a validating stub. DerivedPath declares
`__init__(self, StorePath path=None, str output=None)` and raises when
path is None, so the guess says "constructible with no arguments" and
`Acquire("DerivedPath")` hands back a handle whose EVERY call fails
with an InternalError about the constructor. Alive-looking, useless.

EvalState is worse in the other direction: it takes a store_uri with a
default, so Acquire always builds the "local" one and no client can
ever ask for a different store.

## Why it matters for real Nix

This is not a mock-shaped problem. Real stores come from
`openStore(uri)` and real eval states take a store plus search paths.
Argument-less Acquire cannot express any of that, so the RPC layer
would be able to construct nothing worth constructing.

## Direction

Acquire carries typed constructor arguments, derived the same way
method arguments already are: the constructor's declared parameter
types reach the manifest, grpc_schema emits an AcquireReq per class
(or a oneof), and WireCodec decodes them with the machinery that
already exists for method params.

Open: the pxd declares C++ constructors, the pyx declares the Python
ones, and they differ (StorePath is constructible in C++, forbidden in
Python). The pyx wins - it is the surface - so __cinit__ signatures
need the same pxd backfill treatment methods already get.

Depends on nothing. Synergizes with 018 (a Store ABC wants a uri
argument) and blocks 015 in practice.
