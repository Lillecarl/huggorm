# The hpp files should be smaller

**OPEN.** `_cpp/eval.hpp` is 417 lines of hand-written C++ and about
160 of them are pattern a declaration could carry. This names which,
and what vocabulary each one needs.

## Where it comes from

Carl: *"Why do we have hand crafted hpp files? Isn't our codegen good
enough? The goal is that our uberannotated Python source is the only
source of truth?"*

The goal is right and the file drifted. It was 108 lines under the
mock and it is 417 now, because the libexpr swap wrote a whole
`Evaluator` and `Bridge` surface by hand rather than growing the
declaration to reach it.

The census already prints the number on every build, which is what
made the drift visible instead of comfortable. That is the mechanism
working; this task is the response to what it said.

## The measurement

    417  code lines in _cpp/eval.hpp   (784 with prose)
     85    guarded accessors
     57    parse_expr / eval_expr / make_int / make_string / make_bool
     18    the type_name switch
    ---
    160    about 38%, and derivable

The other 257 are the parts a declaration has no business stating -
see "What is irreducible" below.

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

## What is irreducible, and should stay

About 257 lines, and each has a reason a declaration cannot state:

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
- **The staging builders.** `stage`, `stage_attr`, `materialise`, and
  the `built_` flag that refuses to rewrite an evaluated value. This
  one is arguable: "a collection built one element at a time, sealed
  on first read" is a PATTERN, and if a second binding ever wants it
  the declaration should carry it. One user is not a pattern yet.

## Order

1 first: it is the most lines, the most mechanical, and the guard is
a correctness rule rather than a convenience - deriving it means a
thirteenth accessor cannot forget it. Then 2, which is small and
mostly reuses `pyenum.py`. Then 3, which needs a marker designed
rather than a body deleted.

`tasks/061` overlaps: `@guard` is a new marker, and designing it
before the marker table exists means designing it twice.
