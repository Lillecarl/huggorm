"""The language a declaration is written in, and its reader.

Three things live in this repo and they are easy to confuse:

- the LANGUAGE - `declare.py`, which says what a declaration may say.
  The markers, their table, and the types a declaration names.
- a DECLARATION - a document written in that language. Those are
  `huggorm_decl`, and none of them is here.
- an EMITTER - something that turns a declaration into an output.
  Those are `huggorm_gen`, and none of them is here either.

This package is the first of the three. It carries no declaration
and writes no output, which is what lets a declaration set and an
emitter both depend on it without depending on each other.

`read.py` is here rather than with the emitters because parsing a
declaration is a fact about the LANGUAGE, not about any one output.
Both backends parse the same way or they are not reading the same
language.
"""
