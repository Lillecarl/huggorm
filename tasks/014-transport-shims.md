# Transport shims under the dispatcher

**OPEN.** Nothing is wired. The only transport is grpclib over a socket,
and tasks/035 is waiting on the same question.

Wire the user's existing grpclib shims (SSH, stdio, Python
multiprocessing) in front of the same manifest-driven dispatcher to
prove transport independence. Requires 002 first: per-connection
Session ownership and disconnect reaping.
