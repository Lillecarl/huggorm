# Transport shims under the dispatcher

Wire the user's existing grpclib shims (SSH, stdio, Python
multiprocessing) in front of the same manifest-driven dispatcher to
prove transport independence. Requires 002 first: per-connection
Session ownership and disconnect reaping.
