# Test blind spots

Review finding 14. Known gaps:

- smoke_test's query_derivation manifest assertion compares a string
  against a list of dicts - vacuous (only the hasattr check works).
- Nothing tests: unknown handle/class errors, Release invalidating a
  handle, bint/Any-typed methods over gRPC, concurrent first-calls on
  one handle, annotation resolution.
- RemoteObj.__getattr__ uses next() without default: missing methods
  raise StopIteration instead of AttributeError.

Fix alongside 001/003/004 so the new behavior is pinned by tests.
