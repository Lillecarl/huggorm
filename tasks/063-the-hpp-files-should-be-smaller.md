# The hpp files hold mappings, and must not

**OPEN.** `_cpp/eval.hpp` is 417 lines of hand-written C++. About 160
of them are MAPPINGS, which are not allowed to exist. This names
which, and what vocabulary each one needs.

## Where it comes from

Carl: *"Why do we have hand crafted hpp files? Isn't our codegen good
enough? The goal is that our uberannotated Python source is the only
source of truth?"*

The goal is right and the file drifted. It was 108 lines under the
mock and it is 417 now, because the libexpr swap wrote a whole
`Evaluator` and `Bridge` surface by hand rather than growing the
declaration to reach it.

Carl set the rule when he saw the number, and it is sharper than a
budget: *"We shouldn't have any C++ mapping code at all, if we need
helpers to make the codegen simpler we can have those but nothing
hand-written C++ for the actual mappings."*

So this is not a reduction target. A mapping in `_cpp` is in the
wrong file, and the count is how many are left rather than how many
are affordable.

**The test is who calls it.** Generated code calls a HELPER. A
MAPPING *is* the generated code.

I proposed a line-count baseline that would fail the build on growth,
and Carl rejected it for the right reason: a budget legitimises the
thing that should not exist. Recorded because it is the tempting
wrong answer.

## The measurement

    417  code lines in _cpp/eval.hpp   (784 with prose)

    MAPPINGS - must go
     85    guarded accessors
     57    parse_expr / eval_expr / make_int / make_string / make_bool
     18    the type_name switch
     ~8    list_append / attrs_set bodies
    ---
    ~168

    HELPERS - stay, and the generated code calls them
    ~249    the GC root, thread registration, EvalCore, the core's
            deleter, the staging machinery, the is_* predicates

## 1. Guarded accessors - 85 lines, and the biggest win

Every reader on `Value` has the same body:

    auto * v = get();
    if (v->type<true>() == nix::nThunk)
        throw std::runtime_error("value is a thunk");
    if (v->type() != nix::nInt)
        throw std::runtime_error("value is not int");
    return v->integer().value;

Twelve of them. The only things that differ are the ARM
(`nix::nInt`), the field (`integer()`), and sometimes a conversion
(`.value`, `std::string(...)`).

`nix::Value` is a tagged union whose readers are `noexcept` and
UNDEFINED on the wrong tag, so the guard is not optional and never
will be. That makes it exactly the kind of thing to derive: a rule
that must be applied everywhere, identically, with no judgement.

What the declaration would say:

    @guard("int")
    def integer(self) -> I64:
        """This value as an integer."""

`@guard` names a key of the `@tree` scalars map, which the class
already declares. The thunk check comes free - a thunk is a kind that
is not the named one - so it stops being written twelve times.

## 2. The type_name switch - 18 lines, and the repo already does this

`type_name` maps `nix::ValueType` to the strings `@tree` keys on.
`decl/words.py` already declares `HashAlgorithm` and
`ContentAddressMethod` as StrEnums whose members ARE the strings a Nix
parser takes, and `pyenum.py` emits them.

This is the same mapping pointed the other way: C++ enumerator to
Python string, rather than Python string to C++ parse. One emitter
knows how to write a table between an enum and a set of strings; it
should write both.

The declaration would name the enum and the pairs, and the switch
would be emitted. It also closes a real gap: `nFloat`, `nPath` and
`nNull` are arms the walk could carry as SCALARS and does not,
because adding one today means editing C++.

## 3. The thin producers - 57 lines, and they need `via` back

`make_int` is:

    auto * v = state().allocValue();
    v->mkInt(value);
    return wrap(v);

The interesting half is one line. The rest is the wrap.

This is `via` again, and the direction matters. `via` was deleted
because the INBOUND use is unsafe here - `nix::Value`'s readers are
undefined on the wrong tag, so no method may bind a pointer-to-member
through a handle. The OUTBOUND use is a different mechanism with the
same name: a produced pointer wraps in the handle before it crosses.

So the lesson from deleting `via` is not "handles cannot be derived".
It is that one word carried two mechanisms, and only one of them was
refuted. The outbound half should come back under its own name, and
`@produced(by=...)` is where it belongs - the class already says who
makes one; it could say what wraps it.

## What is a HELPER, and stays

About 249 lines. Each is infrastructure the generated code calls,
rather than a statement of what a Python name means:

- **The GC root.** `nix::allocRootValue` and the `RootValue` member.
  A declaration language would need a concept of "reachable from a
  foreign collector", which is one library's problem rather than a
  binding pattern.
- **Thread registration.** `GC_get_stack_base`, the two thread-local
  flags, the register/unregister pair. libexpr exposes no API for
  this; it is the integration this repo owns, and it is where the
  6600x latch bug lived.
- **`EvalCore`.** Its member ORDER is the fact - each object takes the
  previous by reference - and a declaration that stated the order
  would be restating C++ initialisation rules.
- **The core's deleter.** Registering before teardown, once, so no
  holder has to remember.
- **The staging builders.** `stage`, `stage_attr`, `materialise`, the
  `is_*` predicates, and the `built_` flag that refuses to rewrite an
  evaluated value.

  These are helpers and they split cleanly from the mappings that use
  them. `Evaluator::list_append` is a MAPPING - it says what the
  Python name `list_append` does - and its body is two lines: check
  `is_builder`, call `stage`. That body should be emitted, and the
  two functions it calls should stay here. The same split applies to
  `attrs_set`.

## Order

1 first: it is the most lines, the most mechanical, and the guard is
a correctness rule rather than a convenience - deriving it means a
thirteenth accessor cannot forget it. Then 2, which is small and
mostly reuses `pyenum.py`. Then 3, which needs a marker designed
rather than a body deleted.

`tasks/061` overlaps: `@guard` is a new marker, and designing it
before the marker table exists means designing it twice.

## The numbers, remeasured (065)

The figures above are stale in two ways. `_cpp/eval.hpp` is
`huggorm_decl/cpp/eval.hpp` since the restructure, and it is 238 code
lines rather than 417 - the accessors named under MAPPINGS came down
before this was reread.

The second way matters more. The census counted `cpp/<module>.hpp`
for each module it emitted, so it only ever measured the two files
whose names are module names. What the whole directory holds:

    238  eval.hpp          counted
     35  derived_path.hpp  counted
     37  errors.hpp        NOT counted, until 065
     20  libstore.hpp      NOT counted, until 065
    ---
    330  total

Plus 103 in `Cxx` bodies - 56 in `eval.py`, 47 in `store.py`.

So the number to work down is 330, not 273, and 57 of it had never
appeared in a build log. `census_cpp` prints the directory total now
and names any file no module claims.

`errors.hpp` and `libstore.hpp` are the two orphans. Neither has a
declaration emitted beside it, which is why the per-module census
could not see them and is the first thing to look at: a helper with
no declaration next to it has nothing pulling it toward being
derived.

## Remeasured again (after 066)

The three fronts above are CLOSED. Nothing in this section is new
work; it is a reread, because the file still said OPEN while pointing
at code that no longer exists.

`cpp/eval.hpp` is 238 lines and every one of them is on the HELPER
list this task wrote. The guarded accessors are gone, the `type_name`
switch is gone, and `make_int` / `make_string` / `make_bool` /
`parse_expr` / `eval_expr` are gone. What is left is the GC root and
thread registration, `EvalCore` and its deleter, and `Bridge` - the
staging builders, the `is_*` predicates, `sorted`, `symbol`, `intern`,
`materialise`, `wrap`. Generated code calls all of it.

So the "Order" section is spent. The directory total has not moved -
still 330 - because the lines that came out of `eval.hpp` were
replaced by lines that were never counted. That is the honest reading
of a flat number.

    35  derived_path.hpp
    37  errors.hpp
   238  eval.hpp          all helpers
    20  libstore.hpp
   ---
   330

Three things are left, smallest first.

### errors.hpp names its Python module in a string literal - DONE

`error_class` imports `"huggorm_bindings.errors"`. That module's name
is DERIVED three other ways - `errors_module()` from the declaration's
stem, `_policy.ERROR_MODULE`, and the emitted file name - and this is
a fourth copy that no reader of the other three would find.

It is the bug 065 already found one layer up, still here. Renaming
`decl/errors.py` makes this lookup return null, and `raise_as` then
falls back to a plain RuntimeError carrying the message. Every nix
error silently loses its type, and no gate says so.

The generated catch chain already passes the class name. It should
pass the module too, which makes `raise_as` a helper with no library
knowledge in it at all.

Done. `chain()` takes the module and writes
`raise_as("huggorm_bindings.errors", "BadStorePath", e)`, from the
same `errors_module()` that names the emitted file and fills the
policy table. The static import cache went with it: it would have
held whichever module asked first.

Proved by renaming `decl/errors.py` to `decl/nixfaults.py`. The
bindings and the generated package both build, and the smoke test's
`except NixError` still catches - which it could only do if the
emitted C++ had followed the rename. The suites then fail loudly at
typecheck, naming all seven sites, because a test may name the module
it tests.

The rename also found a copy an hour old: the smoke test block added
by 066 wrote `from huggorm_bindings.errors import NixError`. It reads
`_policy.ERROR_MODULE` now. That is the argument for perturbation in
one line - the copy was written, reviewed and committed the same day,
and only breaking the gate found it.

errors.hpp is 41 code lines, up from 37: the parameter costs two
lines and the DECREF the dropped static did not need costs two more.
A number going up while a mapping goes away is the right trade, and
it is why this task has no budget.

### libstore.hpp: open_store is a conversion

`init_libstore` is a helper and stays - it is idempotent
initialisation that libstore exposes no other way, and a declaration
points `@binds` at it.

`open_store` is one line: `nix::openStore(uri)`, taken as a
`shared_ptr` rather than the `ref<Store>` upstream returns. CLAUDE.md
lists a conversion as a MAPPING. Whether the declaration can say
"take this return as a shared_ptr" is the question; if it can, this
file is 12 lines.

### derived_path.hpp has reached its own trigger

The file states the condition itself: *"WHEN THIS MOVES INTO THE
EMITTER: the second union whose C++ arm wraps a declared arm in a
one-member struct. One user is a helper; two is a pattern."*

There are two - `SingleDerivedPath` and `DerivedPath` - so the
condition is met. The declaration already knows the arms; the four
`as_arms` / `from_arms` visits say the same thing by hand.

This is the largest of the three and the one that needs a marker
designed rather than a literal moved, so `tasks/061` overlaps it the
way it overlapped `@guard`.

#### Corrected, after reading derived-path.hh

"a marker per union" was my guess and it is too big. What upstream
actually holds (nix-store 2.34.8,
`nix/store/derived-path.hh`):

    DerivedPathOpaque      { StorePath path; }

    SingleDerivedPathBuilt { ref<const SingleDerivedPath> drvPath;
                             OutputName  output; }
    DerivedPathBuilt       { ref<const SingleDerivedPath> drvPath;
                             OutputsSpec outputs; }

    struct SingleDerivedPath : variant<DerivedPathOpaque,
                                       SingleDerivedPathBuilt>
    struct DerivedPath       : variant<DerivedPathOpaque,
                                       DerivedPathBuilt>

There is no inheritance. Each union is a struct that PUBLICLY
INHERITS its own `std::variant` and re-exposes it through `raw()`,
which is why the visits say `std::get_if<...>(&p.raw())` rather than
`&p` - they cast back to the base.

The part that shrinks the marker: `SingleDerivedPath::Opaque` and
`DerivedPath::Opaque` are BOTH `DerivedPathOpaque`. The same struct,
the same single member `path`. Only the Built arm differs between the
two unions, and only in one member - `output`, one string, against
`outputs`, an OutputsSpec.

So the fact is stated once, not once per union: *the opaque arm's C++
type is a one-member struct wrapping the declared arm, through the
member `path`*. Four visits collapse to one declared fact.

`held` stays a helper either way. `ref<const SingleDerivedPath>` is
non-nullable by construction, so building one from a value allocates,
and that is upstream's spelling rather than a decision this repo
makes.

Worth checking separately, and probably NOT the same pattern:
`OutputsSpec` is a variant too - `variant<All, Names>` where `All` is
a `std::monostate` and `Names` is a `std::set` with its default
constructor deleted. A monostate arm is a different shape from a
wrapped-struct arm, and its two `Cxx` bodies should not be folded
into this marker just because both are variants.

### And 61 Cxx bodies

`Cxx(...)` in a declaration is the sanctioned hatch: C++ written
where the declaration can see it, lifted out by the reader. 61 uses,
concentrated in `eval.py` (15), `store.py` (14) and
`derived_path.py` (9). Not counted in the 330 and not a violation -
but the hatch is where a mapping goes to hide, and `derived_path.py`
having nine of them beside a header this task wants derived is worth
one look.
