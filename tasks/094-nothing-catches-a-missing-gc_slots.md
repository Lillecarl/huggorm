# Nothing catches a missing @gc_slots

**OPEN.** `tasks/093` fixed one class that stores a Python object and
taught the DSL to say so. Nothing refuses the next class that stores
one and forgets.

## The shape of the miss

A bound class that holds an `nb::object`, a `nb::callable` or a
`std::function` wrapping one, and does NOT carry
`@gc_slots(...)`, leaks itself the moment that object closes over it.
The leak is silent: the suite passes, and nanobind reports it at
interpreter shutdown, after the last test.

That is this repo's named failure mode with a new face. It is not an
emitter skipping what it does not recognise - it is an emitter
CORRECTLY writing what the declaration said, where the declaration
forgot to say something no rule required it to say.

## Why it is not obvious

`register_primop` is the only place today that stores a callable, and
`cpp/eval.hpp` says why nanobind appears in it at all:

> It is the ONLY thing here that needs it, and it stays that way -
> anything else reaching for nanobind is a mapping in a costume.

So the count is one and the rule holds by inspection. The day a
second one arrives, nothing says so.

## What a check could look for

The census already reads `huggorm_decl/cpp/*.hpp`. A class whose C++
type names `nb::object`, `nb::callable` or `nb::handle` as a MEMBER,
whose declaration carries no `@gc_slots`, is the report.

Two things to establish before writing it:

1. **Whether the emitter can see the member.** The census reads text,
   not a parse, so "names it as a member" may be harder than it
   sounds. A cheaper approximation: the FILE mentions `nb::object`
   and some class in it lacks slots.
2. **Whether it should be a refusal or a report.** A refusal has to be
   right every time; a report can be conservative. `tasks/091`'s
   orphan list is the precedent for a report that nobody has to act
   on immediately.

## What it does not need

A DSL feature. `@gc_slots` exists; this is only the thing that notices
its absence.

Opened 2026-09-05, from `tasks/093`.
