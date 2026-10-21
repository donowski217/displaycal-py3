"""Tests for DisplayCAL.wexpect.split_command_line."""

import pytest

from DisplayCAL.wexpect import split_command_line


@pytest.mark.parametrize(
    ("command_line", "expected"),
    [
        ("getappname", ["getappname"]),
        ("setcfg argyll.dir C:\\\\Argyll", ["setcfg", "argyll.dir", "C:\\Argyll"]),
        # Single quotes keep embedded spaces (regressed by the SIM108 refactor,
        # which assigned the accumulated argument to ``state`` instead).
        ("notify 'a b' silent", ["notify", "a b", "silent"]),
        ("notify 'x'", ["notify", "x"]),
        ("'only quoted'", ["only quoted"]),
        ("a 'b c' 'd'", ["a", "b c", "d"]),
        ("'it\\'s'", ["it\\s"]),
        ('notify "a b" sticky', ["notify", "a b", "sticky"]),
        ('say "a \\" b"', ["say", 'a " b']),
        ("a   b", ["a", "b"]),
    ],
)
def test_split_command_line(command_line, expected):
    """Quoted, escaped and plain arguments are split like a POSIX shell."""
    assert split_command_line(command_line) == expected
