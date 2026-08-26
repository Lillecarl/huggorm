"""The two backends, asked the same questions.

Every other gate in this directory compares TEXT: emitted Cython
against the repo's Cython, emitted C++ against the repo's C++. Text is
the right unit while the question is "could the emitter have written
this". It is the wrong unit for the question that decides whether
Cython can be removed, which is "does the replacement behave the
same".

So this one imports two COMPILED modules - one built by Cython from
`decl/path.py`, one built by nanobind from the same file - and asks
them the same questions. A difference here is a difference a caller
would see.

    python3 parity.py <directory holding the nanobind module>

It is not symmetric. Where the two disagree, one of them is right,
and the table below says which and why. A disagreement not in that
table is a failure.
"""

import argparse
import copy
import pathlib
import sys

# A real store path, the one the suites use. Not a made-up string:
# libstore parses this and rejects anything that is not base-32.
HELLO = "7rjjfrn5w3z1kb2v9v0ilxmvmb2n5k1y-hello-2.12.1"
DRV = HELLO + ".drv"


def answers(make: object) -> dict[str, object]:
    """One backend's answer to every question, by name.

    Every question is asked of BOTH backends and compared by name, so
    a backend cannot pass by refusing to answer."""
    a, b, other = make(HELLO), make(HELLO), make(DRV)  # type: ignore[operator]
    out: dict[str, object] = {
        "to_string": a.to_string(),
        "name": a.name(),
        "hash_part": a.hash_part(),
        "is_derivation": a.is_derivation(),
        "str": str(a),
        "repr": repr(a),
        "eq": a == b,
        "eq-other": a == other,
        # The VALUE, not the number: two hashes agreeing is the
        # contract, and the number itself is a build's business.
        "hash-equal": hash(a) == hash(b),
        "lt": a < other,
        "sorted": [str(x) for x in sorted([other, a])],
        "in-set": len({a, b}) == 1,
    }
    # Four questions whose answer may be an exception. Asked the same
    # way, because "it raised" is an answer a caller sees.
    probes = {
        "eq-None": lambda: a == None,  # noqa: E711
        "copy": lambda: str(copy.copy(a)),
        "deepcopy": lambda: str(copy.deepcopy(a)),
        "bad-name": lambda: make("not-a-store-path"),  # type: ignore[operator]
    }
    for name, ask in probes.items():
        try:
            out[name] = ask()
        # Broad on purpose: the exception TYPE is the answer being
        # compared, so narrowing here would decide the result.
        except Exception as exc:
            out[name] = f"{type(exc).__name__}: {exc}"
    return out


# Where the two disagree and the difference is CORRECT, with the
# reason. Nothing is here yet, and that is the honest state: every
# difference found so far has been the nanobind emitter falling short
# rather than the two backends being right to differ.
KNOWN: dict[str, str] = {}

# Where the two disagree because the nanobind emitter has not learnt
# something yet. Not the same thing as KNOWN, and kept apart on
# purpose: one says "this is fine", the other says "this is work".
# Emptying this table is what makes removing Cython a decision rather
# than a gamble.
#
# It IS empty. Every difference it held was the nanobind emitter
# falling short, and each one is closed: the repr now names its
# fields, a value copies, and a nix exception arrives as the class
# `cythonix_bindings.errors` declares with libstore's terminal
# escapes stripped. Sixteen of sixteen answers agree.
NOT_YET: dict[str, str] = {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("nanobind_dir",
                    help="directory holding the nanobind-built module")
    args = ap.parse_args()
    sys.path.insert(0, str(pathlib.Path(args.nanobind_dir).resolve()))
    import path as nb_path

    from cythonix_bindings.path import StorePath as CyPath

    cy, nb = answers(CyPath), answers(nb_path.StorePath)
    width = max(len(k) for k in cy)
    problems: list[str] = []
    outstanding: list[str] = []
    same = 0
    for key in cy:
        if cy[key] == nb[key]:
            same += 1
            continue
        if key in KNOWN:
            print(f"  {key}: differs, and it is RIGHT to. {KNOWN[key]}")
            continue
        if key in NOT_YET:
            outstanding.append(f"  {key}: {NOT_YET[key]}")
            continue
        problems.append(f"  {key:<{width}}  cython:   {cy[key]!r}")
        problems.append(f"  {'':<{width}}  nanobind: {nb[key]!r}")
    print(f"  StorePath: {same} of {len(cy)} answers identical, "
          f"from one declaration through two backends")
    if outstanding:
        print(f"  {len(outstanding)} difference(s) the nanobind emitter "
              f"has not closed yet:")
        print("\n".join(outstanding))
    if problems:
        print("\n".join(["", "FAILED - the two backends disagree:", *problems]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
