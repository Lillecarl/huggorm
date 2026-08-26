# The pure-mode probe

The evidence behind `../PURE-MODE.md`. One C++ class shaped like
`nix::StorePath`: a deleted default constructor, a throwing
constructor, a `string_view` accessor and a defaulted `operator==`.

    nix run --file ../.. ourPython -- setup.py build_ext --inplace

## `ann.py` - the same file twice

Compiles as a C++ extension type, and imports as ordinary Python:

    python -c "import ann; ann.Thing('x')"
    python -c "import cyshims, ann; \
               print(ann.Thing.__annotations__['_ptr'].spelling())"
    shared_ptr[CThing]

`c_thing.pxd` is what Cython cimports when compiling. `c_thing.py` is
what Python imports when reading. They describe the same C++ class,
and a generator could write the first from the second.

## `verify.py` - the three shapes the bindings need

1. **A custom exception translator.** `Thing("")` raises
   `errors.ProbeError`, colour field intact, through
   `except +translate_probe_error`. The declaration carries it, and
   the declaration is a pxd either way - so pure mode never spells it.

   It fires only through `make_thing`, a factory declared in the pxd.
   `make_shared` carries libcpp's own plain `except +`
   (`Cython/Includes/libcpp/memory.pxd:107`), so a translator declared
   on the CONSTRUCTOR never runs: the call Cython emits is
   `std::make_shared`, not the constructor.

2. **nogil inside a `def`**, which is where the real bindings put it -
   not only inside a cfunc. `slow_len()` releases and returns 5.

3. **The produced-value shape.** `Located` holds no C++ at all: object
   slots something else fills, `_from_parts` through `__new__`, and an
   `__init__` that refuses. What `StoreLocation` and `PathInfo` need.

`check_shims.py` asserts what all of this assumes about Cython.
