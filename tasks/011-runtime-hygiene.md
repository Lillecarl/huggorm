# runtime.py hygiene: duplicate to_dict, symbol-contract check

Review finding 12. WrapperError.to_dict is defined twice (identical).
The emitter-runtime import contract (attach_runner/unwrap_arg/runners)
is validated nowhere except incidentally - a rename ships a wheel that
fails at first wrapper import.

Fix: delete the duplicate; smoke_test asserts the exact symbol set the
emitter emits against _runtime exports.
