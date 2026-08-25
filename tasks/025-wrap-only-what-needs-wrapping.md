# Wrap iff it needs a home thread or can block

Found while deciding 017's return types (2026-08-25), verified against
the real Nix source at
/nix/store/2ijv0g6069dsh55z3bdr5ln2iv69mw7r-source.

## The observation

The codegen decides whether to emit an async wrapper from the
THREADING policy. But threading is only half of what justifies a
wrapper. The full rule is:

    wrap iff the object needs a home thread, OR its methods can block

    RemoteStore, Value, Derivation   affine        -> wrap
    LocalStore                       pool, blocks  -> wrap
    StorePath, DerivedPath           pool, neither -> nothing to wrap

Wire-values fall out of the third row, which is how 017 resolved its
return-type divergence: an AsyncStorePath would put a thread-pool
round trip in front of a substr.

## Evidence from upstream

nix::StorePath holds ONE std::string baseName. to_string() is noexcept
and returns a string_view into that member; name() and hashPart() are
substr on it; isDerivation() is noexcept. Nothing allocates, does I/O
or blocks. All the work is in the constructors, and that is string
validation.

nix::DerivedPath is the same: variants over StorePath plus output
names, to_string/parse, comparison operators.

By contrast Store's methods hit a database, a daemon socket or the
network - which is why the split is real and not cosmetic.

## The knowledge already exists, unextracted

The pyx marks blocking per method, and has since the beginning:

    def get_uri(self) -> str:                 # no nogil: fast
        return self._ptr.get_uri().decode('utf-8')

    def add_text_to_store(self, ...):
        with nogil:                           # nogil: may block
            ...

`with nogil:` IS the declaration "this may block, release the GIL".
The pxd carries the matching `except + nogil` on the same methods. So
the codegen could DERIVE the blocking half instead of being told it,
the same way it derives everything else - but it currently reads
neither.

## Fix sketch

- Extract per-method blocking from the pyx (a `with nogil:` anywhere in
  the body) or from the pxd's per-method nogil, and put it in the
  manifest.
- Cross-check the two against each other: a pyx method that releases
  the GIL for a pxd declaration that is not nogil is a bug, and vice
  versa. That check is worth having on its own.
- Then the wrap decision, and the GIL policy comments in the pyx that
  currently only a human reads, become one derived fact.

Not urgent: 017 gets the right answer for wire-values from the
immutability half alone. This matters when a pool class turns up whose
methods are all cheap, or a wire-value gains an expensive method.
