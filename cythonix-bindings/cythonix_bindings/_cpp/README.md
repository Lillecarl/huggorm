# C++ this repo writes

Everything here exists because a pxd cannot say something, not because
Nix is missing it. One header per binding module, named for it, plus
`errors.hpp`, which every binding shares.

A helper belongs here when Cython cannot express the call:

- **a by-value return of a type with no default constructor.** Cython
  declares a temporary to hold one before assigning, so `nix::StorePath
  parseStorePath(...)` cannot be called directly. The helper returns a
  pointer and the binding owns it.
- **a member reached through a reference member.** `Store::config` is a
  `const StoreConfig &`, and describing it in a pxd means declaring the
  whole config type for the sake of one string.
- **a template Cython has no declaration for.** `ref<T>` is Nix's
  non-null shared_ptr; the helper returns the `std::shared_ptr<T>` it
  converts to.

A helper does NOT belong here when it is doing work. If it computes
something, decides something, or holds state, it is a binding written
in the wrong language: put it in the pyx, where the whole toolchain -
lint, typecheck, the generated surfaces - can see it.
