"""
The msgpack value codec (huggorm#142).

Every value goes through `pack` and `unpack`, so a shape msgpack
cannot carry fails here rather than on a socket.
"""

from typing import Any

import pytest
from test_store import chroot, wire_samples  # noqa: F401  (a fixture)

from huggorm import tree
from huggorm.codec import Codec, Node, pack, unpack
from huggorm_bindings import Store
from huggorm_generated._callspec import Wire, WireKind
from huggorm_generated._policy import UNION_ARMS


def _no_proxy(_: Any) -> Any:
    raise AssertionError("no proxy was expected here")


def _across(codec: Codec, w: Wire, value: Any) -> Any:
    return codec.decode(w, unpack(pack(codec.encode(w, value, _no_proxy))),
                        _no_proxy)


@pytest.mark.usefixtures("flakes")
def test_every_wire_value_crosses(chroot: Store) -> None:  # noqa: F811
    """Each sample comes back equal, with an equal hash, for every
    wire value and every union arm the samples hold."""
    codec = Codec()
    samples = wire_samples(chroot)
    for name, (built, extra) in sorted(samples.items()):
        w = Wire(WireKind.VALUE, name)
        for value in [built, *(type(built)._from_parts(*p) for p in extra)]:
            back = _across(codec, w, value)
            assert back == value, (name, value, back)
            assert hash(back) == hash(value), name

    crossed = 0
    for alias, arms in UNION_ARMS.items():
        w = Wire(WireKind.UNION, alias)
        for arm in arms:
            if arm.kind is WireKind.VALUE and arm.name in samples:
                value = samples[arm.name][0]
                back = _across(codec, w, value)
                assert back == value and type(back) is type(value), (alias, arm)
                crossed += 1
    assert crossed >= 2, "the samples reach no union's value arm"


def test_a_scalar_arm_matches_its_exact_type() -> None:
    """`True` is an int to isinstance. Sent as the uint arm, it would
    come back as 1."""
    codec = Codec()
    w = Wire(WireKind.UNION, "Attr")
    for value in ("name", 7, True, False):
        back = _across(codec, w, value)
        assert back == value and type(back) is type(value), value


def test_str_and_bytes_stay_apart() -> None:
    codec = Codec()
    as_bytes, as_str = Wire(WireKind.SCALAR, "bytes"), Wire(WireKind.SCALAR, "str")
    assert _across(codec, as_bytes, b"\xff\x00") == b"\xff\x00"
    with pytest.raises(TypeError, match="arrived as str"):
        codec.decode(as_bytes, unpack(pack("abc")), _no_proxy)
    with pytest.raises(TypeError, match="arrived as bytes"):
        codec.decode(as_str, unpack(pack(b"abc")), _no_proxy)
    # A converter alone would read 5 as five zero bytes.
    with pytest.raises(TypeError, match="arrived as int"):
        codec.decode(as_bytes, 5, _no_proxy)


def test_absence() -> None:
    codec = Codec()
    item = Wire(WireKind.SCALAR, "int")
    optional = Wire(WireKind.SCALAR, "int", optional=True)
    assert _across(codec, optional, None) is None
    assert _across(codec, optional, 0) == 0
    with pytest.raises(TypeError, match="not optional"):
        codec.encode(item, None, _no_proxy)
    with pytest.raises(TypeError, match="not optional"):
        codec.decode(item, None, _no_proxy)
    # A container keeps None apart from empty only where the
    # declaration allows None.
    assert _across(codec, Wire(WireKind.LIST, item=item, optional=True),
                   None) is None
    assert _across(codec, Wire(WireKind.LIST, item=item), None) == []
    assert _across(codec, Wire(WireKind.MAP, item=item), None) == {}


def test_a_proxy_crosses_as_its_handle() -> None:
    codec = Codec()
    w = Wire(WireKind.LIST, item=Wire(WireKind.PROXY, "Value"))
    objs = [object(), object()]
    ids = {id(o): f"h{i}" for i, o in enumerate(objs)}
    raw = unpack(pack(codec.encode(w, objs, lambda o: ids[id(o)])))
    assert raw == ["h0", "h1"]
    assert codec.decode(w, raw, lambda hid: hid.upper()) == ["H0", "H1"]


def test_a_tree_crosses() -> None:
    codec = Codec()
    held = object()
    node = tree.Entries({
        "b": tree.Items([tree.Leaf("int", 1), tree.Leaf("bool", True),
                         tree.Leaf("float", 0.5)]),
        "a": tree.Leaf("str", "x"),
        "c": tree.Stays("Value", held),
    })
    raw = unpack(pack(codec.encode_tree(node, lambda cls, obj: "h1")))
    back = codec.decode_tree(raw, lambda cls, hid: (cls, hid))
    assert back == {"a": "x", "b": [1, True, 0.5], "c": ("Value", "h1")}
    assert list(back) == ["a", "b", "c"], "attribute names come back sorted"
    with pytest.raises(TypeError, match="tree node"):
        codec.decode_tree([Node.STAYS, "Value"], lambda cls, hid: None)


def test_a_malformed_union_is_refused() -> None:
    codec = Codec()
    w = Wire(WireKind.UNION, "Attr")
    for raw in ([3, "x"], ["0", "x"], [0], "x"):
        with pytest.raises(TypeError, match="arm index"):
            codec.decode(w, raw, _no_proxy)
    # A value arm holding the union again, past any real depth.
    with pytest.raises(ValueError, match="nested more than"):
        codec.decode(w, [0, "x"], _no_proxy, depth=10_000)
