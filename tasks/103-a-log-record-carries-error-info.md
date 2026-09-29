# A log record carries Nix's ErrorInfo

**OPEN.** Found by the nanopynix port (`tasks/097`), 2026-09-29.

## Problem

`LogTap::logEI` renders an `ErrorInfo` into text and raises a `msg`
record (`test_logs.py::test_a_warning_takes_the_other_path`). The
position, the trace, the suggestions and `isFromExpr` go no further.
nanopynix-bindings sends them: its log event for `logEI` is an `error`
action with the `ErrorInfo` dict beside the text, and nanopynix's
`test_an_error_event_carries_the_same_payload_on_both_engines` fails on
huggorm: "inproc sent no structured payload with its error event".

## Two facts before any work

**A record is visible only in the unit that declares it.** `ErrorInfo`
is declared in `path.py` because the translator is there (`tasks/100`).
`LogRecord` is bound in `eval`, so the eval unit cannot name
`huggorm::ErrorInfo`. This is the second unit that needs a record from
another one. The generic answer: emit each module's records into a
header of their own, and have a unit include the header of every
module whose records it names, as `imports()` does for Python. Check
first whether any record spells a union alias or needs `as_tuple`.

**A bound value is rebuilt through its C++ constructor.** `LogRecord`
binds the hand-written `huggorm::LogRecord`, crosses the wire, and
`_from_parts` calls its constructor with the parts. An `info` part
would make `cpp/logging.hpp` hold `std::optional<huggorm::ErrorInfo>`,
a generated type in a hand-written header. A `nix::ErrorInfo` cannot
stand in: it cannot be rebuilt from the record, because a position's
origin is gone.

So the choice is Carl's: the hand-written header includes a generated
one, or `info` is an in-process accessor only (`@local`) and nanopynix's
rpc carries the dict itself.

## Adapter side

huggorm's tap raises `msg` for `logEI`; nanopynix's `LogEvent.error_info`
answers for an `error` action, with the dict as `args[2]`. The adapter's
`_callback_args` would map a record that carries info to `error`, with
the dict `error_detail` already builds.

## Decision, 2026-09-29

Carl chose the records header and a wire part: emit each module's
records into a header of their own, have a unit include the headers of
the records it names, and let `LogRecord` hold
`std::optional<huggorm::ErrorInfo>`, so `cpp/logging.hpp` includes a
generated header. The info then crosses huggorm's wire typed, as an
error's does.
