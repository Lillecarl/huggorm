# Sixteen tasks answer neither signal

**OPEN.** The agreement check in `CLAUDE.md` reports a mismatch for 51
of the task files, and every one of them for the same reason: the file
carries no status word at all. The check cannot tell a done task from
an open one, so it says "open" and the `.done` suffix disagrees.

## What the check sees today

    51 files carry no **OPEN/DONE/...** word in their first 12 lines.
    35 of those are named `*.md.done`, so the suffix answers and only
       the check is noisy.
    16 are named `*.md`, so NOTHING answers.

The sixteen:

    008 transitive-policy              029 typed-dicts-for-the-protocol
    012 test-blind-spots               032 log-callbacks-to-the-client
    014 transport-shims                033 primops-in-python
    015 real-nix-spike                 034 functions-as-values
    016 evaluation-server              035 anyio-not-asyncio
    022 proto-field-stability          037 tests-outside-the-sandbox
    025 wrap-only-what-needs-wrapping  040 store-paths-as-filesystem-paths
    026 typed-proxy-parameters         050 shim-methods-hand-type-their-signatures

## Why it matters

`tasks/031` was named as the next thing to do, twice, from a summary
that read its filename and not its text. It was closed - the refusal
it described is the boundary it deliberately landed. That cost two
wrong recommendations, and the only thing that would have prevented it
is the file saying so on line one.

The sixteen above are that same trap, sixteen times over. Some of them
are certainly finished: `035 anyio-not-asyncio` names a decision the
suite already runs under, and `037 tests-outside-the-sandbox` names the
split `default.nix` already makes.

## What to do

Read each of the sixteen and decide from its TEXT, not its name.
Then give it a status word and rename it if the word says done.

Do not batch this into one commit. A wrong call here marks live work
as finished, which is the failure this file exists to stop - so one
commit per file, or one per group that shares an answer, with the
sentence that decided it.

The 35 `.done` files are a smaller thing. The suffix already answers,
so adding a word to each is tidying rather than a fix, and it can wait
until someone is in the file anyway.

## What NOT to do

Do not change the check to ignore a missing word. It is reporting a
real gap, and a check taught to be quiet about it would leave the
sixteen exactly as they are while looking clean.

Opened 2026-09-02, after closing `tasks/075`.
