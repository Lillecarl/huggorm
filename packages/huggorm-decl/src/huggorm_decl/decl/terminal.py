"""
nix::filterANSIEscapes: `nix/util/terminal.hh`.

How Nix makes a log line fit a terminal. A caller that prints Nix's
own output reads it the same way, rather than with a pattern of its
own that misses what Nix's does not.
"""

from huggorm_dsl.declare import (
    U64,
    Bint,
    Cxx,
    Str,
    needs,
)


@needs("nix/util/terminal.hh", "limits")
def filter_ansi_escapes(text: Str, filter_all: Bint = False,
                        width: U64 | None = None) -> Str:
    """Filter the ANSI escape sequences out of `text`, as Nix does.

    With `filter_all` false a colour sequence stays and counts for no
    width. With it true every sequence goes, the OSC 8 hyperlinks
    included. A tab becomes spaces to the next multiple of eight, and
    a carriage return and a bell go.

    `width` cuts the result to that many printable characters, a wide
    character counting two. None does not cut."""
    Cxx("""
auto limit = width ? static_cast<unsigned int>(*width)
                   : std::numeric_limits<unsigned int>::max();
return nix::filterANSIEscapes(text, filter_all, limit);
    """)
