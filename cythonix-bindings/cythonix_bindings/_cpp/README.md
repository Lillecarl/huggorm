# C++ this repo writes

Everything here exists because a DECLARATION cannot say it, not
because Nix is missing it. One header per binding module, named for
it, plus `errors.hpp`, which every binding shares.

A helper belongs here when the call is not a binding:

- **a decision.** `nix::ValidPathInfo` holds a store directory and a
  store path; what Python wants is the two joined. Which separator,
  which fields get rendered, what an unset registration time means -
  each is a choice, and a choice is not a binding.
- **lifetime the binding cannot own.** `open_store_uri` keeps one
  LocalStore per state directory, because `nix::openStore` caches
  nothing and two LocalStores in one process deadlock.
- **an initialisation order.** libstore ABORTS rather than raising
  when `initNix` has not run, so `init_libstore` runs at import.

A declaration reaches one of these with `@binds` (a free function) or
`@cxx_body` (one method), and both are COUNTED - the build prints how
many lines came through the hatch, per class. That is the bargain: a
hatch nobody measures becomes the place the real code lives.

A helper does NOT belong here when the emitter can derive it. A method
pointer, a lambda over a parameter, a container conversion, a value's
comparisons - nanobind casts or the emitter writes all of them, and a
hand-written version is a second answer to a question already
answered.
