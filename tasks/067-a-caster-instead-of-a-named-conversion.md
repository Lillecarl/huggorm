# A caster, instead of a conversion named at every call site

**OPEN.** Eleven sites in the declarations name a conversion by hand
that a nanobind `type_caster` would do once. This says which, and what
it would cost.

## Where it comes from

`tasks/063` deleted `cpp/derived_path.hpp` by teaching the union's
alias to say what C++ holds it. The emitter writes `as_arms`,
`from_arms` and `held` from that fact now, and no hand-written line
survives.

The conversion still has a NAME, though, and eleven bodies say it:

    decl/derived_path.py  SingleDerivedPathBuilt.__init__   huggorm::held
    decl/derived_path.py  SingleDerivedPathBuilt.drv_path   huggorm::as_arms
    decl/derived_path.py  DerivedPathBuilt.__init__         huggorm::held
    decl/derived_path.py  DerivedPathBuilt.drv_path         huggorm::as_arms
    decl/store.py         query_missing                     huggorm::from_arms
    decl/store.py         print_derived_path                huggorm::from_arms
    decl/store.py         parse_derived_path                huggorm::as_arms
    decl/store.py         query_path_info                   *self.query...()

Plus `*self.drvPath` twice, inside the two `as_arms` calls above.

That is goal 3: a rule applied identically in eleven places belongs in
the emitter. It is not a violation today - a `Cxx` body is the
sanctioned hatch and the function it calls is generated - but the
declarations still carry a line that says nothing about Nix.

## What a caster would change

`_bare` puts `std::variant<nix::StorePath, nix::DerivedPathBuilt>` in
every signature, and the comment above it says why:

> nanobind's caster is specialised on std::variant exactly, not on
> something deriving from one. So the binding takes the arms the
> PYTHON side has and a body converts, which is a decision and belongs
> in a body.

A generated `type_caster<nix::DerivedPath>` removes that. It composes
with the caster nanobind already ships - `make_caster<Arms>` - and
does the visit in `from_cpp` and the build in `from_python`, which is
`as_arms` and `from_arms` under different names. Then the signature is
`nix::DerivedPath` and every body loses its call:

    parse_derived_path   return nix::DerivedPath::parse(self.config, target);
    print_derived_path   return target.to_string(self.config);
    query_missing        return self.queryMissing(targets);

A second caster, for `nix::ref<const T>`, takes the rest.
`SingleDerivedPathBuilt::drvPath` is one, so `drv_path` becomes
`@reads("drvPath")` and the two `__init__` bodies stop calling `held`.
`Store::queryPathInfo` returns one, so `query_path_info` stops
dereferencing.

## What it would cost, and why it is not done yet

**It writes C++ shapes this repo has not written.** A caster is not a
function the generated code calls; it is a specialisation the compiler
finds. `NB_TYPE_CASTER`, `cast_t`, `movable_cast_t` and the
`from_python` / `from_cpp` pair are nanobind's protocol, and getting
one wrong fails at RUNTIME with a bad_cast out of module init rather
than at compile time.

**Neither union is default-constructible.** `DerivedPathOpaque` holds
a `nix::StorePath`, which has no default constructor, so the caster
needs the `std::optional`-storage dance nanobind's own variant caster
does for exactly this reason. That is real code, not a template.

**CLAUDE.md wants Carl's word first.** Every line of C++ the codegen
did not write needs approval in advance, and a feasibility spike is C++
written by hand even when the end state is generated.

## What the declaration would have to say

Probably nothing new for the union: `Variant(cxx=, raw=, header=,
wraps=)` already carries every fact the caster needs, which is the
reason this is worth doing rather than a second design.

The `ref` caster needs one fact this repo has not stated: that
`nix::ref<T>` is a non-nullable handle, and how to make one
(`nix::make_ref<T>`). `Decl.holder` says something close for
`shared_ptr` and may be the right place.
