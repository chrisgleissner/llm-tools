"""Bracketed-paste newline normalisation: a lone CR is the terminal's newline."""

from __future__ import annotations

import pytest

from llm_tools.bridge_input import BracketedPaste, normalize_paste_newlines


def decode(*chunks: bytes) -> str | None:
    decoder = BracketedPaste()
    result = None
    for chunk in chunks:
        result = decoder.feed(chunk)
    return result


def paste(payload: bytes) -> bytes:
    return BracketedPaste.START + payload + BracketedPaste.END


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"a\rb\rc", "a\nb\nc"),
        (b"a\r\nb\r\n", "a\r\nb\r\n"),
        (b"a\nb\n", "a\nb\n"),
        (b"a\r", "a\n"),
        (b"\r\r\n\r", "\n\r\n\n"),
        (b"one\rtwo\r\nthree\nfour\r", "one\ntwo\r\nthree\nfour\n"),
        (b"\x1b[A\tx\r\x1b[<0;1;18M", "\x1b[A\tx\n\x1b[<0;1;18M"),
        ("é\r\U0001F600".encode(), "é\n\U0001F600"),
        (b"", ""),
    ],
)
def test_decoder_normalises_only_lone_cr(payload: bytes, expected: str) -> None:
    assert decode(paste(payload)) == expected


def test_cr_at_chunk_boundary_followed_by_lf_stays_crlf() -> None:
    assert decode(BracketedPaste.START + b"a\r", b"\nb\r", b"c" + BracketedPaste.END) == "a\r\nb\nc"


def test_cr_split_from_end_marker_is_a_newline() -> None:
    assert decode(BracketedPaste.START + b"a\r", BracketedPaste.END) == "a\n"


def test_byte_at_a_time_matches_whole_paste() -> None:
    data = paste(b"a\rb\r\nc\n\rd\r")
    decoder = BracketedPaste()
    result = None
    for index in range(len(data)):
        result = decoder.feed(data[index:index + 1]) or result
    assert result == decode(data) == "a\nb\r\nc\n\nd\n"


def test_normalize_helper_passthrough() -> None:
    assert normalize_paste_newlines("no controls") == "no controls"
    assert normalize_paste_newlines("a\rb") == "a\nb"
