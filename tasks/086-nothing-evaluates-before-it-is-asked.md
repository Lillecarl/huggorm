# Nothing evaluates before it is asked

**OPEN.** The last third of `tasks/016`, and the last piece of the
destination `CLAUDE.md` names: "registered expressions evaluated
eagerly in the background so a user-triggered eval is already in
progress or already done."

It waited for `tasks/083`, and correctly: re-evaluating eagerly is
only useful once something knows the old answer is stale. That is
built now - `Watcher` decides what to forget and `Notifier` notices
without being asked - so this is what is missing.

## What exists to build on

    Watcher.eval_file(root)    evaluates and records a snapshot
    Watcher.changed(path)      forgets the roots that could depend on it
    Notifier.next_change()     one event, acted on, roots ANSWERED

The last line is the hinge. `changed()` already returns the roots it
forgot, and `next_change()` passes that up. So the trigger is not
missing - what is missing is anything that does something with it.
Today a caller gets a list of stale roots and has to re-evaluate them
itself.

## What it is

A registry of roots to keep warm, and a task that re-evaluates one
when it goes stale.

    register(root)     keep this one warm
    unregister(root)
    ...and a loop over next_change() that re-evaluates what it named

That is small, and the smallness is the point: 083 built the change
source so that this would be a consumer rather than a rewrite.

## What it has to get right, and none of it is obvious

**A re-evaluation is not free and a change is not one event.** Saving
a file raises several inotify events, and an editor writing by rename
raises more. Re-evaluating on each of them evaluates the same root
three or four times. Some debounce is needed, and a debounce is a
TIMER - which is the first thing in this whole area that cannot be
gated without one. That is the design question, not the loop.

**A failed eager evaluation is not a failure.** A file saved
mid-edit does not parse. The eager pass must not raise into whatever
is running, must not retry forever, and must not swallow the error so
completely that a caller asking later gets a fresh copy of the same
failure with no history.

**It shares the state's thread with the user.** An `EvalState` is
affine, so an eager evaluation and a user-triggered one QUEUE behind
each other. Starting one eagerly can therefore make a user wait,
which is the opposite of the point. Whether the eager pass yields, or
runs on its own state, or is simply cancelled when a real call
arrives, is a decision this task has to make and record.

**Cancelling an evaluation in flight.** libexpr offers no such thing.
So "cancelled" can only mean "we stopped waiting for it", and the
evaluation keeps running on that thread. Worth measuring before
designing around it.

## Where it lives

Beside `Notifier`, under both surfaces, for the reason Carl gave for
`Watcher`: it is written against `EvalStateLike`, so one object serves
the in-process and the remote case. It needs no C++.

## Where it does not go

`tasks/085` is the other open piece of the destination, and it is a
different problem: a process-wide log sink needs C++ and is blocked on
approval. This one is blocked on nothing.
