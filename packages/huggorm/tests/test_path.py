"""
The first REAL Nix type (huggorm#15).

nix::StorePath is the smallest thing that proves the chain end to end:
pkg-config linkage against the split nix-store component, a namespaced
C++ class, a constructor that validates and throws from libstore, and
accessors returning views into the object's own storage.

It stands beside the mock's StorePath rather than replacing it.
"""

import copy
from typing import TYPE_CHECKING

import pytest

from huggorm_bindings import StorePath
from huggorm_bindings.errors import BadStorePath, NixError
from huggorm_generated._callspec import Wire, WireKind

if TYPE_CHECKING:
    from huggorm_gen import ir

HELLO = "7rjjfrn5w3z1kb2v9v0ilxmvmb2n5k1y-hello-2.12.1"


def test_a_real_store_path_parses() -> None:
    p = StorePath(HELLO)
    assert p.to_string() == HELLO
    assert p.name() == "hello-2.12.1"
    assert p.hash_part() == "7rjjfrn5w3z1kb2v9v0ilxmvmb2n5k1y"
    assert len(p.hash_part()) == 32, "HashLen, in base-32 characters"


def test_derivations_are_recognised() -> None:
    assert not StorePath(HELLO).is_derivation()
    assert StorePath(HELLO + ".drv").is_derivation()


def test_validation_comes_from_libstore() -> None:
    """The whole reason to bind the real thing rather than reimplement
    it. Nothing in this repo knows what makes a store path valid."""
    with pytest.raises(BadStorePath, match="too short to be a valid store path"):
        StorePath("not-a-store-path")
    with pytest.raises(BadStorePath):
        StorePath("0" * 32 + "-bad name with spaces")


def test_a_nix_error_keeps_its_type() -> None:
    """nanobind's own translator maps anything it does not recognise
    onto RuntimeError, which loses every distinction libstore drew.
    The module registers `translate_nix_error` ahead of it, and the
    hierarchy mirrors nix's own so catching the base still works."""
    with pytest.raises(NixError) as caught:
        StorePath("nope")
    assert isinstance(caught.value, BadStorePath)
    assert not isinstance(caught.value, RuntimeError), "the old behaviour"


def test_error_messages_carry_no_terminal_escapes() -> None:
    """libstore writes its messages in colour whether or not anything
    is a terminal, so what() comes back holding escape codes. They are
    stripped at the boundary: they are wrong in a traceback and wrong
    over the wire, and every reader downstream would have to know."""
    with pytest.raises(NixError) as caught:
        StorePath("nope")
    assert "\x1b" not in str(caught.value), repr(str(caught.value))


def test_the_colour_is_kept_beside_the_plain_message() -> None:
    """It exists so an error can be PRINTED to a terminal, which is
    the one place it is useful. Stripping it at the boundary and
    nowhere else would take that from every caller who has a tty."""
    with pytest.raises(NixError) as caught:
        StorePath("nope")
    err = caught.value
    assert "\x1b[" in err.colored, repr(err.colored)
    assert err.message == str(err)
    # Same message, one dressed and one not.
    assert "too short to be a valid store path" in err.colored
    assert "too short to be a valid store path" in err.message


def test_accessors_do_not_hand_back_views() -> None:
    """Every accessor returns a string_view INTO the object's baseName.
    A view outliving its owner is a dangling pointer, not an exception,
    so the binding copies before anything reaches Python."""
    name = StorePath(HELLO).name()  # the path itself is now garbage
    assert name == "hello-2.12.1"


def test_a_real_path_copies() -> None:
    p = StorePath(HELLO)
    assert copy.copy(p).to_string() == HELLO
    # Immutable, so a deep copy is a copy.
    assert copy.deepcopy(p).to_string() == HELLO


def test_a_real_path_crosses_the_wire() -> None:
    """It is a wire-value like any other: the codec sends the
    _wire_fields the binding declares, and rebuilds it on the far side
    through _from_parts. No layer above the binding knows the type
    exists."""
    from conftest import across, crossed, load_model, part

    path = load_model().classes["StorePath"]
    assert path.binds == "CStorePath"
    assert path.wire == "value"

    w = Wire(WireKind.VALUE, "StorePath")
    assert part("StorePath", crossed(w, StorePath(HELLO)), "base_name") == HELLO
    back = across(w, StorePath(HELLO))
    assert isinstance(back, StorePath)
    assert back.to_string() == HELLO


def test_a_value_compares_hashes_and_prints() -> None:
    """A value type IS its declared parts, in all three senses.

    Without this, two paths naming the same store object were never
    equal, a set of them deduplicated nothing, and repr() showed an
    address instead of the one string the object carries - so every
    caller compared .to_string() by hand (huggorm#46).

    The comparison is C++'s. nix::StorePath defaults operator== and
    operator<=>, so the binding declares them rather than
    reimplementing the answer in Python."""
    a, b = StorePath(HELLO), StorePath(HELLO)
    other = StorePath("1" * 32 + "-other")

    assert a == b
    assert (a != b) is False, "__ne__ follows __eq__ rather than identity"
    assert a != other
    assert a != HELLO, "a path is not its string"
    assert a != 7 and a.__eq__(7) is NotImplemented

    assert hash(a) == hash(b)
    assert len({a, b, other}) == 2, "a set of paths deduplicates"
    assert {a: "yes"}[b] == "yes", "and a dict keyed by one looks up"

    assert repr(a) == f"StorePath(base_name={HELLO!r})"
    assert str(a) == HELLO

    # Ordering, so sorted() matches the order a store's own set has.
    assert other < a and a > other and a >= b and a <= b
    assert sorted([a, other])[0] == other


def test_every_value_type_has_value_semantics(model: ir.Model) -> None:
    """The rule, held against the model rather than a list.

    A `_copied = True` class that did not compare would be a value in
    name only. The build refuses one now, so this asserts the RESULT
    on every declared value at once - and it grows on its own when a
    new one lands."""
    import huggorm_bindings

    values = [n for n, c in model.classes.items() if c.wire == "value"]
    assert len(values) >= 4, values
    for name in values:
        cls = getattr(huggorm_bindings, name)
        for dunder in ("__eq__", "__hash__", "__repr__"):
            assert getattr(cls, dunder) is not getattr(object, dunder), (
                f"{name}.{dunder}")


def test_an_explicit_DEFAULT_is_not_an_absent_field() -> None:
    """A falsy value that was SET reads back as itself, not as None.

    This is 048's property. The codec once read a scalar back as
    `None if not raw`, so an explicitly-passed 0 or "" arrived as
    None. Absence is msgpack's nil and nothing else.

    Asked of the CODEC rather than of a type (huggorm#60): real Nix's
    one optional scalar, `PathInfo.registration_time`, collapses 0 and
    unknown on purpose, so no object's accessors can pose the
    question."""
    from conftest import across, crossed

    when = Wire(WireKind.SCALAR, "int", optional=True)
    assert crossed(when, None) is None and across(when, None) is None
    assert crossed(when, 0) == 0 and across(when, 0) == 0
    assert across(when, 1_700_000_000) == 1_700_000_000
