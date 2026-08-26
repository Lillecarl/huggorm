# The pure-mode probe

The evidence behind `../PURE-MODE.md`. One C++ class with a deleted
default constructor, a throwing constructor and a `string_view`
accessor - the smallest thing shaped like `nix::StorePath`.

`ann.py` is the claim: the SAME file compiles as a C++ extension type
and imports as ordinary Python.

    # compiles
    python setup.py build_ext --inplace
    python -c "import ann; ann.Thing('x')"

    # ...and the same file, read rather than run
    python -c "import cyshims, ann; \
               print(ann.Thing.__annotations__['_ptr'].spelling())"
    shared_ptr[CThing]

`c_thing.pxd` is what Cython cimports when compiling. `c_thing.py` is
what Python imports when reading. They describe the same C++ class,
and the second is what `cyshims` makes reachable - so a generator
could write the first from it.
