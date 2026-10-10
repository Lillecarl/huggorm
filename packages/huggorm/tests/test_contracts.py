"""
Contracts that hold without a server running.
"""

import ast
import pathlib
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from huggorm_gen import ir


def test_no_hardcoded_domain_types(model: ir.Model) -> None:
    """No layer above the bindings may name a domain type.

    Wire policy is declared next to the binding and reaches the schema,
    the server and the client through the model. A type name written
    into any of them is the duplication this design exists to remove:
    it means adding a class needs edits in four places, and forgetting
    one fails at the first call that touches it, not at build time."""
    domain = set(model.classes)
    # Exception classes are domain types too, and the same rule holds
    # for the same reason: which errors exist is the bindings' to
    # declare, so no layer above them may carry a list of them
    # (huggorm#36). The builtins this layer raises ITSELF - KeyError for
    # an unknown handle - are not in that set and are not the subject.
    domain |= set(model.errors.classes)
    here = pathlib.Path(__file__).resolve().parent.parent / "huggorm"
    offenders = []
    for mod in ("server.py", "remote.py", "lifecycle.py", "codec.py",
                "protocol.py"):
        tree = ast.parse((here / mod).read_text(), filename=mod)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in domain:
                offenders.append(f"{mod}:{node.lineno}: {node.value!r}")
    assert not offenders, "; ".join(offenders)


def test_a_declared_error_crosses_as_its_own_message() -> None:
    """The class identity is an index into the declared errors, not a
    name to look up. A name crosses only for an undeclared cause, and
    only a builtin exception class is built from one (huggorm#36).

    Needs no server: this is what the server would put on the wire."""
    from huggorm.protocol import Faults
    from huggorm_bindings.errors import BadStorePath
    from huggorm_generated._policy import ERROR_FIELDS
    from huggorm_generated._runtime import InternalError

    faults = Faults()
    failed = InternalError("Store.parse_store_path failed",
                           cause=BadStorePath("plain", "coloured"))
    raw = faults.encode(failed)
    assert raw[4] == list(ERROR_FIELDS).index("BadStorePath"), raw

    # An UNDECLARED cause crosses by name, with no parts.
    plain = faults.encode(InternalError("something else failed",
                                        cause=ValueError("nope")))
    assert plain[2:] == ["ValueError", "nope", None, None], plain


def test_every_declared_error_has_its_parts(model: ir.Model) -> None:
    """The emitted error table holds every declared error, with the
    parts the model declares, in order.

    The emitter loops the error table; it names no class. So adding an
    error to the bindings adds it here, and this holds the two in
    step."""
    from huggorm_generated._policy import ERROR_FIELDS

    declared = model.errors.classes
    assert declared, "the bindings declare no errors at all"
    assert set(ERROR_FIELDS) == set(declared)
    for name, error in declared.items():
        assert [a.name for a in ERROR_FIELDS[name]] == [
            f.name for f in error.wire_fields], name


def test_every_declared_error_rebuilds_from_its_parts(
        model: ir.Model) -> None:
    """An error must survive its own round trip.

    An error crosses as its declared parts and comes back as
    `cls(*parts)`, so the parts have to reach the attributes they are
    named after. A class whose __init__ reorders, renames or drops a
    part fails HERE rather than at the first remote failure, which is
    the one moment nobody is watching for a bug.

    A test rather than a build check, and it used to be
    `model.check_error_contract`. Nothing static proves this - it
    builds one of each and reads it back - so it needed the compiled
    package, and the generator imported that package to run it. That
    import is what kept every Python surface behind a C++ compiler.
    The build sandbox runs this suite, so it is still a build gate;
    it is just no longer a reason for the generator to reflect.
    """
    import importlib

    module_name = model.errors.module
    assert module_name, "the bindings declare no error module"
    module = importlib.import_module(module_name)
    for name, error in model.errors.classes.items():
        fields = error.wire_fields
        assert fields, (
            f"{name}: no wire_fields, so nothing says how to rebuild it "
            f"on the far side")
        # Distinct values, so a swap is visible. A reordering that kept
        # the same string in both slots would otherwise pass.
        probe = [f"<{f.name}>" for f in fields]
        built = getattr(module, name)(*probe)
        for f, sent in zip(fields, probe, strict=True):
            got = getattr(built, f.name, None)
            assert got == sent, (
                f"{name}.wire_fields names {f.name!r}, but building it from "
                f"its parts leaves {f.name} = {got!r}, not {sent!r}")


def test_a_string_enum_decodes_to_its_class(model: ir.Model) -> None:
    """A value read off the wire comes back typed.

    A StrEnum crosses as a plain string - it IS one. What the declared
    enum table buys is the other direction: the codec knows which
    class to rebuild, so a caller gets ContentAddressMethod.FLAT
    rather than "flat", and a value that is not a member raises here
    instead of reaching libstore.

    Needs no server: this is the converter both sides use."""
    from conftest import across

    from huggorm.codec import Codec
    from huggorm_bindings import ContentAddressMethod
    from huggorm_generated._callspec import Wire, WireKind
    from huggorm_generated._policy import WIRE_FIELDS

    assert model.enums, "the bindings declare no vocabularies"
    method = Wire(WireKind.ENUM, "ContentAddressMethod")
    assert WIRE_FIELDS["ContentAddress"][0].type == method

    assert across(method, ContentAddressMethod.FLAT) is ContentAddressMethod.FLAT
    with pytest.raises(ValueError, match="not a valid"):
        Codec().decode(method, "nonsense", lambda _: None)


def test_the_front_door_covers_the_surface() -> None:
    """One import, and nothing real left behind it.

    `import huggorm` used to export two mock DEMO classes, so a user
    faced three packages and no guidance on which to import - the
    build topology as the first thing to learn (huggorm#51).

    Derived rather than listed twice: this asks the two packages what
    they export and requires the front door to carry all of it. A new
    binding that lands without reaching the front door fails here
    rather than being missed by a reader.

Everything behind the front door
    has to reach it, with no exception left: the Mock* classes were the
    last one, and they are gone (huggorm#60)."""
    import huggorm
    import huggorm_bindings
    import huggorm_generated

    behind = {
        name
        for pkg in (huggorm_bindings, huggorm_generated)
        for name in pkg.__all__
    }
    missing = sorted(behind - set(huggorm.__all__))
    assert not missing, f"not reachable from `import huggorm`: {missing}"
    assert all(hasattr(huggorm, n) for n in huggorm.__all__)


def test_every_name_the_model_DECLARES_reaches_the_front_door() -> None:
    """The same question as above, asked of the model instead.

    The test above walks the two packages' `__all__`, which covers
    everything that IS a Python object in one of them. A union is not:
    `DerivedPath = StorePath | DerivedPathBuilt` is an alias the
    generated package writes from the model, and it reached the front
    door because a person put it there.

    That is the hole. The model grows TABLES - classes, then
    functions, then enums, then unions - and a test that names them
    is a test the next table is born outside of. So this names none.

    A NAME TABLE is a mapping field whose keys are all identifiers.
    That separates the four from `errors`, which is one record rather
    than a table."""
    import dataclasses
    from collections.abc import Mapping

    from conftest import load_model

    import huggorm

    model = load_model()
    tables = [getattr(model, f.name) for f in dataclasses.fields(model)]
    declared = {
        name
        for table in tables
        if isinstance(table, Mapping) and table
        and all(str(k).isidentifier() for k in table)
        for name in table
    }
    assert "DerivedPath" in declared, (
        "the union table stopped being read, so this gate is asleep")

    missing = sorted(declared - set(huggorm.__all__))
    assert not missing, (
        f"the model declares {missing}, and `import huggorm` does not "
        f"reach them. A declared name that no front door carries is a "
        f"name only a reader of the model knows about.")


def test_the_package_ships_no_demos() -> None:
    """A demo is reading material, not library surface.

    custom.py, async_demo.py, remote_demo.py and run_remote.py shipped
    inside the installable package. They live in examples/ now, and
    this is what keeps them there."""
    import huggorm

    installed = pathlib.Path(huggorm.__file__).parent
    demos = sorted(f.name for f in installed.glob("*.py")
                   if f.stem.endswith("_demo") or f.stem in
                   ("custom", "run_remote"))
    assert demos == [], demos


def test_an_enum_survives_a_container() -> None:
    """An enum is a scalar, and a container does not change that.

    Nothing declares an enum container today, so the case is built
    here. That is the whole reason it stayed latent (huggorm#47)."""
    from conftest import across, crossed

    from huggorm.codec import Codec
    from huggorm_bindings import ContentAddressMethod as CA
    from huggorm_bindings import HashAlgorithm
    from huggorm_generated._callspec import Wire, WireKind

    words = Wire(WireKind.LIST, item=Wire(WireKind.ENUM, "HashAlgorithm"))
    table = Wire(WireKind.MAP, item=Wire(WireKind.ENUM, "ContentAddressMethod"))

    sent = [HashAlgorithm.SHA256, HashAlgorithm.SHA512]
    assert crossed(words, sent) == ["sha256", "sha512"], "a member IS its string"
    back = across(words, sent)
    assert back == sent and all(isinstance(v, HashAlgorithm) for v in back)

    by_name = {"a": CA.NAR, "b": CA.FLAT}
    assert crossed(table, by_name) == {"a": "nar", "b": "flat"}
    assert all(isinstance(v, CA) for v in across(table, by_name).values())

    # A member that is not one raises here rather than reaching
    # libstore - the same guarantee a singular field has.
    with pytest.raises(ValueError, match="not a valid"):
        Codec().decode(words, ["sha256", "nonsense"], lambda _: None)


def test_a_method_with_no_wire_form_is_absent_everywhere(
        model: ir.Model) -> None:
    """A method the wire cannot carry has no wire number.

    Not everything a binding offers is a remote call. EvalState's
    make_primop takes a Python callable, which is not data, and a
    remote one would make the evaluator call back over the socket
    (huggorm#33). So the server publishes no handler for it, and the
    protocol cannot promise it.

    It stays on the one class, with a `Local` spec. A remote backend
    refuses it with the spec's reason (`test_primop.py`)."""
    from huggorm_generated import AsyncEvalState, _policy
    from huggorm_generated._callspec import Local
    from huggorm_generated.protocols import EvalStateLike

    evaluator = model.classes["EvalState"]
    blocked = {m.name for m in evaluator.methods if not model.offered(m)}
    assert "make_primop" in blocked, sorted(blocked)

    wired = {m.name for m in _policy.METHODS["EvalState"]}
    for name in blocked:
        assert not hasattr(EvalStateLike, name), f"{name} is on the protocol"
        assert hasattr(AsyncEvalState, name), f"{name} lost its method"
        assert name not in wired, f"{name} has a wire number"
        assert isinstance(getattr(_policy, f"_EvalState_{name}"), Local), name


def test_an_untyped_cause_rebuilds_from_builtins_only() -> None:
    """The approximation names no types of its own.

    It used to hold a table of five builtins, which was wrong twice:
    every OTHER builtin silently became a bare Exception, and adding
    one meant editing a file above the bindings.

    The rule is now one sentence - resolve in `builtins`, and only if
    it is an exception class - so what may be constructed is bounded
    without anything keeping a list. Nothing else the peer names gets
    near a constructor, and a name that vanished before is now kept in
    the message."""
    from huggorm.protocol import _approximate

    # A builtin exception rebuilds as itself.
    for name in ("ValueError", "KeyError", "OSError", "IndexError",
                 "ZeroDivisionError", "StopAsyncIteration"):
        out = _approximate(name, "boom")
        assert type(out).__name__ == name, out

    # A builtin that is NOT an exception is never constructed...
    for name in ("print", "dict", "object", "type"):
        assert type(_approximate(name, "boom")) is Exception, name
    # ...nor is anything that is not a builtin at all.
    for name in ("Store", "NixError", "os.system", ""):
        assert type(_approximate(name, "boom")) is Exception, name

    # A builtin whose constructor wants more than a message degrades
    # rather than raising, and the name survives in the text.
    out = _approximate("UnicodeDecodeError", "boom")
    assert type(out) is Exception and "UnicodeDecodeError" in str(out)


def test_a_declared_order_is_an_order_that_works(
        model: ir.Model) -> None:
    """A type the declaration says compares must actually compare.

    The stubs are generated from `dunders`, so a type listed there as
    ordering typechecks under `sorted()`. For two of them that was a
    promise nothing kept: `PathInfo` and `StoreLocation` define
    `__eq__` and no ordering, the class gets all six comparison slots
    anyway, and reflection read the slots back as implemented
    comparisons. `sorted(infos)` passed the typechecker and raised
    TypeError (huggorm#52).

    Reflection cannot answer this - it measures what the compiler
    emitted, not what the source said - so the fix was to take
    `dunders` from the declaration. This is the test that says the fix
    holds, and it asks the question the stub's reader will ask: if the
    declaration says `<` works, does `<` work?"""
    import importlib

    checked = []
    for c in model.classes.values():
        if "__lt__" not in c.dunders:
            continue
        cls = getattr(importlib.import_module(c.qualified_module), c.name)
        # Two of the same type, however this one is built. A value that
        # cannot be constructed here is skipped rather than faked: the
        # claim is about types a caller can hold.
        try:
            a, b = cls("0" * 32 + "-a"), cls("0" * 32 + "-b")
        except Exception:
            continue
        assert (a < b) is not NotImplemented
        assert sorted([b, a]) == [a, b]
        checked.append(c.name)
    # A test that checked nothing would pass forever.
    assert checked, "no ordered type in the model was constructible"


def test_reflection_would_still_get_the_order_wrong(
        model: ir.Model) -> None:
    """Why `dunders` cannot be reflected, held as a fact.

    A bound class defining any rich comparison gets `tp_richcompare`,
    and CPython fills all six comparison slots with wrappers. So for a
    value type with `__eq__` and no ordering, `cls.__lt__` EXISTS and
    refuses when called. It was a cdef class that made this a bug; a
    nanobind one behaves the same way, which is why the test stayed.

    That is what made the reflected dunders wrong, and it is still
    true - the fix was to stop asking the compiled class. This test
    says so out loud: if it ever starts failing, the slot has stopped
    being filled and `dunders` could be measured again."""
    import importlib

    found = []
    for c in model.classes.values():
        if c.wire != "value" or "__lt__" in c.dunders:
            continue
        cls = getattr(importlib.import_module(c.qualified_module), c.name)
        # The slot is there. Reflection sees it and calls it an
        # implemented comparison; the declaration knows better.
        assert getattr(cls, "__lt__", None) is not None, c.name
        found.append(c.name)
    assert found, "no value type without a declared order was found"


def test_the_stubs_promise_the_same_order_the_model_does(
        model: ir.Model) -> None:
    """The stubs are what a caller's typechecker reads.

    huggorm#52 was a complaint about the STUBS. Stubs that come from a
    second route, one that never sees the declaration, can disagree
    with the model about whether PathInfo has an ordering. A second
    route to the same fact is a second answer to it.

    So this compares the two artefacts rather than calling anything.
    Building an instance and trying `<` looks stronger and is weaker:
    a produced value has no constructor, so that check skips exactly
    the classes most likely to be wrong."""
    import sys

    from huggorm_dsl.read import DECLARED_DUNDERS

    # Found on the path, not beside the bindings. A PEP 561 stub
    # package is its own distribution and Nix installs it in its own
    # store path, so `huggorm_bindings.__file__` is the wrong anchor.
    stubs = next((d for entry in sys.path
                  if (d := pathlib.Path(entry) / "huggorm_bindings-stubs")
                  .is_dir()), None)
    assert stubs is not None, "huggorm_bindings-stubs is not on sys.path"

    declared = {name: set(c.dunders)
                for name, c in model.classes.items()}
    checked = 0
    for pyi in sorted(stubs.glob("*.pyi")):
        for node in ast.parse(pyi.read_text()).body:
            if not isinstance(node, ast.ClassDef) or node.name not in declared:
                continue
            # A taught dunder is a declared METHOD; this compares the
            # derived value dunders.
            stubbed = {n.name for n in node.body
                       if isinstance(n, ast.FunctionDef)
                       and n.name.startswith("__") and n.name != "__init__"
                       and n.name not in DECLARED_DUNDERS}
            assert stubbed == declared[node.name], (
                f"{pyi.name}:{node.name} stubs {sorted(stubbed)}, "
                f"the declaration says {sorted(declared[node.name])}")
            checked += 1
    assert checked, "no stubbed class was found in the model"


def test_a_blocking_method_on_a_value_gets_a_coroutine(
        model: ir.Model) -> None:
    """`@blocks` on a value is awaitable, as a free coroutine.

    A value has no wrapper, so without one an async caller stalls its
    event loop (huggorm#25). `Input.fingerprint` hashes a path input's
    tree. Its async form is `input_fingerprint(input, store)`, the
    package exports it, and the method's stub names it."""
    import sys

    from huggorm_gen.ir import MethodRef
    from huggorm_gen.pygen.emitter import package_exports

    found = {f.calls: f.name for f in model.blocking_methods}
    assert found.get(MethodRef("Input", "fingerprint")) == "input_fingerprint", found
    assert "input_fingerprint" in package_exports(model)

    stubs = next(pathlib.Path(entry) / "huggorm_bindings-stubs"
                 for entry in sys.path
                 if (pathlib.Path(entry) / "huggorm_bindings-stubs").is_dir())
    tree = ast.parse((stubs / "fetchers.pyi").read_text())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "Input")
    method = next(n for n in cls.body
                  if isinstance(n, ast.FunctionDef) and n.name == "fingerprint")
    assert "huggorm.input_fingerprint" in (ast.get_docstring(method) or "")


def test_a_class_with_no_door_refuses_to_be_built(
        model: ir.Model) -> None:
    """A class the declaration says does not construct must refuse.

    Every layer above reads `constructs` and declines to offer a
    constructor, so the BINDING has to agree or the layers are
    describing a class that does not behave that way.

    The binding did not agree once, and the reason is worth keeping
    even though the machinery is gone. `nb::init<>()` was the only way
    to reach a trampoline, and the held C++ type is abstract too - so
    `std::is_constructible_v<Type>` was false, nanobind always built
    the trampoline, and `MockStore()` succeeded. The failure moved to
    the first call, as "tried to call a pure virtual function", which
    is a worse place to learn about it. There are no trampolines now
    (huggorm#60) and the binding simply declares no constructor, but
    the guard this test drives is the same one.

    This asked `abstract` and slept for it. Nothing had carried
    `@abstract` since the mock went, so the loop ran zero times and
    said so in a comment. `constructs` is the question it always
    meant - "is there a door" - and it separates the two things
    `@abstract` used to say (huggorm#61). nix::Store now states the
    true C++ fact about itself and still constructs, through its
    factory, so it is correctly NOT a subject here; the five produced
    types are.

    No `match=`. Which sentence a refusal carries depends on WHY the
    door is shut, and there are three - produced by something else,
    no constructor declared, abstract with no factory. The class name
    is the part every one of them has."""
    import importlib

    checked = []
    for c in model.classes.values():
        if c.constructs:
            continue
        cls = getattr(importlib.import_module(c.qualified_module), c.name)
        with pytest.raises(TypeError) as caught:
            cls()
        assert c.name in str(caught.value), str(caught.value)
        checked.append(c.name)
    assert len(checked) >= 5, checked


def test_every_declared_constructor_default_reaches_the_binding() -> None:
    """A default a declaration gives, a caller may leave out.

    `Param` unpacks as (name, type), so an emitter written as
    `for n, _ in params` never sees a default. The `nb::init` path was
    written that way, and `EvalState(store_uri, settings=None)` bound
    `settings` as required, while the stub said it was optional
    (huggorm#97). Only a constructor with a C++ body wrote defaults.

    nanobind keeps a signature's defaults as the third item of
    `__nb_signature__`, so the count is read off the compiled class."""
    import huggorm_bindings
    import huggorm_decl

    checked = []
    for cls in huggorm_decl.corpus().classes:
        if cls.ctor is None:
            continue
        declared = sum(pr.has_default for pr in cls.ctor.params)
        if not declared:
            continue
        init = getattr(huggorm_bindings, cls.name).__init__
        bound = [len(defaults or ()) for _, _, defaults in
                 init.__nb_signature__]
        assert declared in bound, (cls.name, declared, bound)
        checked.append(cls.name)
    assert "EvalState" in checked, checked


def test_a_wire_value_cannot_be_subclassed(model: ir.Model) -> None:
    """A type that crosses as its PARTS must be final.

    Two things break otherwise, and both are silent. A subclass
    carrying state no declared field reads arrives on the far side
    missing it, because the codec sends `_parts()` and nothing else.
    And `__eq__` and `__hash__` are derived over those same parts, so
    a subclass compares and hashes equal to a base that is not the
    same object at all.

    It also settles what `__eq__` means. A record's equality is a
    typed C++ `const T &` comparison, and nanobind will cast a derived
    instance to the base - so the same-class rule the declaration
    states was enforced by nothing but a cast that happened to fail on
    uninitialised storage. `nb::is_final()` is the enforcement.

    A PROXY is deliberately not final: a caller may subclass one to
    add behaviour, and nothing about a handle breaks when they do."""
    import importlib

    checked = []
    for c in model.classes.values():
        if c.wire != "value":
            continue
        cls = getattr(importlib.import_module(c.qualified_module), c.name)
        with pytest.raises(TypeError, match=r"prohibit|final"):
            type(f"Sub{c.name}", (cls,), {})
        checked.append(c.name)
    assert checked, "the model declares no wire value"
