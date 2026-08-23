# runtime.py hygiene: symbol-contract check

Review finding 12, second half. The duplicate WrapperError.to_dict is
removed (2026-08-23). Remaining: the emitter-runtime import contract
(attach_runner/unwrap_arg/runners) is validated nowhere except
incidentally - a rename ships a wheel that fails at first wrapper
import.

Fix: smoke_test parses the emitted modules for _runtime references and
asserts every referenced symbol exists on the runtime module.

