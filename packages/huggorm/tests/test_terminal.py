"""Nix's own reading of a terminal line."""

from huggorm_bindings import filter_ansi_escapes

RED = "\x1b[31;1m"
RESET = "\x1b[0m"
LINK = "\x1b]8;;https://example.org\x1b\\here\x1b]8;;\x1b\\"


def test_a_colour_stays_unless_every_sequence_goes() -> None:
    text = f"{RED}error:{RESET} x"
    assert filter_ansi_escapes(text) == text
    assert filter_ansi_escapes(text, filter_all=True) == "error: x"


def test_a_hyperlink_goes_with_every_sequence() -> None:
    assert filter_ansi_escapes(LINK, filter_all=True) == "here"


def test_a_tab_becomes_spaces_to_the_next_stop() -> None:
    assert filter_ansi_escapes("ab\tc") == "ab      c"


def test_width_counts_printable_characters_only() -> None:
    assert filter_ansi_escapes(f"{RED}abcdef{RESET}", filter_all=True,
                               width=3) == "abc"
