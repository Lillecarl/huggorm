"""
A Nix function's shape, as `inspect.Signature`.

`tasks/034` asks which Python type is right for introspecting a Nix
function, and this is the answer: the one Python's own tooling already
understands. `help()`, `inspect.signature()` and an IDE's call hints
all read a `Signature`, so a Nix lambda described as one costs a
caller nothing to learn.

Hand-written, and it has to be. The BINDING answers facts - which
formals, which have defaults, whether there is an ellipsis - and each
of those is one declared accessor. Assembling them into a `Signature`
is a mapping onto a Python library type, which no declaration
describes and no emitter should learn.

## What maps onto what

    { a, b ? 2 }:        (*, a, b=Ellipsis)
    { a, ... }:          (*, a, **kwargs)
    x:                   (x, /)
    builtins.add         (e1, e2, /)

A formal becomes KEYWORD_ONLY, because that is how `apply_auto`
passes it - by name, out of an attribute set. A `x:` lambda's
parameter becomes POSITIONAL_ONLY, because `apply` takes one value and
its name is not a keyword anyone can use.

## Why `Ellipsis` is the default

A Nix default is an unevaluated expression in the lambda's own
environment, so it cannot cross as a value and should not: a caller
needs to know a default EXISTS, which is what changes whether an
argument is required.

`Ellipsis` is the readable convention for that. `(*, a, b=Ellipsis)`
reads as "b is optional and I am not telling you what it defaults
to", where `None` would claim a default that Nix does not have and
`Parameter.empty` would say the parameter is required.

## What it refuses

A partially-applied builtin. `builtins.add 1` still wants an
argument and nothing in libexpr says how many - `getDoc` has no
branch for the shape at all. A `(*args)` would be a guess dressed as
an answer, so this raises instead.

## Async only

Every surface a caller has is async - the wrapper and the RPC client
both - and reading a signature is several accessor calls. A sync
version would only serve the raw binding layer, which is the one
place a caller can already read the accessors directly.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from huggorm_generated.protocols import ValueLike

# The default of a formal that HAS one. See the module docstring: the
# expression itself cannot cross, and the only thing a caller needs is
# that the argument is optional.
UNKNOWN_DEFAULT = Ellipsis

# What an ellipsis formal becomes. A name is needed because
# `Parameter` requires one, and this is the one Python code already
# writes for the same thing.
EXTRA = "kwargs"


async def signature_of(value: ValueLike | Any) -> inspect.Signature:
    """The `inspect.Signature` of a Nix function value.

    Raises `TypeError` for a value that is not a function, and for a
    partially-applied builtin - the one function shape whose remaining
    arguments nothing can name.

    Several accessor calls, so several round trips over RPC. Not
    cached and not attached as `__signature__`: a proxy is built for
    every function value that crosses, and paying for introspection
    nobody asked for would make realizing a tree of functions
    quadratic. A caller that wants it asks.
    """
    if await value.type_name() != "function":
        raise TypeError(
            f"a {await value.type_name()} has no signature; only a function "
            f"does")

    if await value.is_lambda():
        return await _lambda_signature(value)
    if await value.is_primop():
        return await _primop_signature(value)

    # The third shape. It is callable and its arity is gone: the
    # applied arguments are inside a chain nothing walks, and `getDoc`
    # has no branch for it either.
    raise TypeError(
        "a partially-applied builtin has no signature: nix does not say "
        "how many arguments it still wants, and guessing would be an "
        "answer rather than the absence it is")


async def _lambda_signature(value: Any) -> inspect.Signature:
    """A lambda's, from its formals or its single argument."""
    if not await value.has_formals():
        # `x: body`. POSITIONAL_ONLY, because `apply` takes a value
        # and the name is not a keyword any caller can use - it is
        # the lambda's own binding, reported so `help()` can show it.
        name = await value.lambda_arg()
        return inspect.Signature([
            inspect.Parameter(_identifier(name, "arg"),
                              inspect.Parameter.POSITIONAL_ONLY)])

    names = await value.formal_names()
    defaulted = set(await value.defaulted_formals())
    params = [
        inspect.Parameter(
            _identifier(name, f"formal_{n}"),
            inspect.Parameter.KEYWORD_ONLY,
            default=(UNKNOWN_DEFAULT if name in defaulted
                     else inspect.Parameter.empty))
        for n, name in enumerate(names)]

    # A parameter with no default may not follow one, even among
    # keyword-only parameters, so the required ones go first. The
    # binding answers alphabetically and this reorders within that -
    # required alphabetically, then optional alphabetically.
    params.sort(key=lambda p: (p.default is not inspect.Parameter.empty,))

    if await value.accepts_extra():
        # `...`, which is exactly VAR_KEYWORD: the lambda accepts
        # names it does not mention. It changes what `apply_auto`
        # PASSES as well as what it accepts.
        params.append(inspect.Parameter(EXTRA,
                                        inspect.Parameter.VAR_KEYWORD))

    # `{ a, b } @ rest:` binds the whole set as well, and a Signature
    # cannot say that: `rest` is not another parameter, it is the same
    # attribute set under a name. Reported through `lambda_arg` for a
    # caller who wants it, and absent here on purpose.
    return inspect.Signature(params)


async def _primop_signature(value: Any) -> inspect.Signature:
    """A builtin's, from the arity it declares.

    The one place an arity is honest. Names come from `primop_args`
    when it has them - and it does not always: a primop declares its
    `args` vector independently of its `arity`, so a mismatch is
    upstream's to have and this fills the gap rather than trusting
    the pair to agree.
    """
    arity = await value.primop_arity()
    names = await value.primop_args()
    params = [
        inspect.Parameter(
            _identifier(names[n] if n < len(names) else "", f"arg_{n}"),
            inspect.Parameter.POSITIONAL_ONLY)
        for n in range(arity)]
    return inspect.Signature(params)


def _identifier(name: str, fallback: str) -> str:
    """A name `inspect.Parameter` will accept.

    Nix identifiers are wider than Python's: `{ "with a space" ? 1 }`
    is a legal formal, and quoted formals can be keywords too.
    `Parameter` raises on either, which would turn a signature for an
    unusual function into a failure to introspect it at all.

    So an unusable name becomes a positional fallback. The real name
    is still readable through `formal_names`, which is the accessor
    that answers facts; this function is building a Python object and
    Python's rules apply to it.
    """
    if name.isidentifier() and not _is_keyword(name):
        return name
    return fallback


def _is_keyword(name: str) -> bool:
    import keyword

    return keyword.iskeyword(name)
