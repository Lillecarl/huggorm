# An error carries Nix's ErrorInfo

**OPEN.** Found by the nanopynix port (`tasks/097`), 2026-09-27.

## Problem

A huggorm error carries two strings, `message` and `colored`. Every
`nix::Error` also carries a `nix::ErrorInfo`: the level, the primary
position, the evaluation trace and the suggestions. C++ is the only
place that holds it. After translation it is gone.

nanopynix-bindings attaches it to each raised error as `info`, a dict
(`nanopynix-bindings/src/nix_error_info.hh`):

- `level`, `msg`, `status`, `is_from_expr`;
- `pos`: `{file, line, column}` or None, the file as `Pos::print`
  renders the origin;
- `traces`: up to 32 of `{hint, pos}`, and `truncated`;
- `suggestions`: a list of strings.

## Evidence

Four tests on the huggorm lane fail on `excinfo.value.info is None`,
all in nanopynix's `test_scalar_accessor_semantics.py`: max-call-depth
at an explicit depth, runaway recursion at the default depth (inproc
and rpc), and a cyclic value. `MissingAttributeError.suggestions` is
also `[]` on huggorm, because it reads `info["suggestions"]`.

## Sketch

An `ErrorInfo` wire value, and a third part in `NixError._wire_fields`,
so the info crosses the wire as the two strings do. The translator
fills it from `e.info()`. The caps on traces and on each string stay,
for the reason nanopynix gives: the status details ride in an HTTP/2
header.

Open question: whether a position is `huggorm::position_file` plus
line and column, the shape `Doc` and `AttrDoc` use, or Nix's own
rendering of the origin.
