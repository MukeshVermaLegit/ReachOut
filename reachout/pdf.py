"""Minimal PDF text extraction, standard library only.

Public CVs, conference programmes, academic papers and committee minutes are
where contact details actually get published in the open - and they are almost
all PDFs. Pulling text out of them needs no dependency: PDF content streams are
usually Flate-compressed, and zlib is in the standard library.

This is deliberately not a full PDF implementation. It ignores fonts, encodings
and layout, and aims only to recover enough readable text to find email
addresses and phone numbers. Scanned/image-only PDFs yield nothing, correctly.
"""

from __future__ import annotations

import re
import zlib

_STREAM_RE = re.compile(rb"stream\r?\n?(.*?)endstream", re.S)
_STREAM_START = re.compile(rb"stream\r\n|stream\n|stream\r")
_LENGTH_RE = re.compile(rb"/Length\s+(\d+)\b")
_TEXT_OPS = re.compile(rb"\b(?:Tj|TJ|BT)\b")
_OCTAL_RE = re.compile(rb"\\([0-7]{1,3})")

_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
            b"(": b"(", b")": b")", b"\\": b"\\"}

MAX_PDF_BYTES = 12_000_000


def _iter_streams(data: bytes):
    """Yield raw stream bytes, using /Length to find the real end.

    Scanning for the next "endstream" looks simpler but breaks badly: compressed
    image data frequently contains those bytes, which truncates one stream and
    desynchronises every stream after it. On a typical paper that loses the whole
    title page - which is exactly where the contact details are.
    """
    for match in _STREAM_START.finditer(data):
        body_start = match.end()
        window = data[max(0, match.start() - 2048):match.start()]
        length_match = None
        for length_match in _LENGTH_RE.finditer(window):
            pass  # keep the last /Length before this stream keyword
        if length_match:
            size = int(length_match.group(1))
            if 0 < size <= len(data) - body_start:
                chunk = data[body_start:body_start + size]
                # Sanity-check that the declared length lands on "endstream".
                tail = data[body_start + size:body_start + size + 20]
                if b"endstream" in tail:
                    yield chunk
                    continue
        end = data.find(b"endstream", body_start)
        if end != -1:
            yield data[body_start:end]


def _inflate(chunk: bytes) -> bytes:
    """Decompress a stream, tolerating the several ways PDFs get this wrong."""
    for attempt in (lambda d: zlib.decompress(d),
                    lambda d: zlib.decompress(d, -zlib.MAX_WBITS),
                    lambda d: zlib.decompressobj().decompress(d)):
        try:
            out = attempt(chunk)
            if out:
                return out
        except zlib.error:
            continue
    return b""


def _read_literal(data: bytes, index: int) -> tuple[bytes, int]:
    """Read a PDF literal string '(...)', honouring escapes and nesting."""
    out = bytearray()
    depth = 1
    length = len(data)
    while index < length:
        char = data[index:index + 1]
        if char == b"\\":
            nxt = data[index + 1:index + 2]
            if nxt in _ESCAPES:
                out += _ESCAPES[nxt]
                index += 2
                continue
            match = _OCTAL_RE.match(data, index)
            if match:
                out.append(int(match.group(1), 8) & 0xFF)
                index = match.end()
                continue
            index += 2  # backslash before newline: line continuation
            continue
        if char == b"(":
            depth += 1
        elif char == b")":
            depth -= 1
            if depth == 0:
                return bytes(out), index + 1
        out += char
        index += 1
    return bytes(out), index


def _read_hex(data: bytes, index: int) -> tuple[bytes, int]:
    end = data.find(b">", index)
    if end == -1:
        return b"", len(data)
    digits = re.sub(rb"[^0-9A-Fa-f]", b"", data[index:end])
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("ascii")), end + 1
    except ValueError:
        return b"", end + 1


def _decode(raw: bytes) -> str:
    """PDF simple fonts are close enough to Latin-1 for our purposes.

    UTF-16BE strings carry a byte-order mark, so those are handled properly.
    """
    if raw[:2] in (b"\xfe\xff",):
        try:
            return raw[2:].decode("utf-16-be", "replace")
        except Exception:
            pass
    return raw.decode("latin-1", "replace")


# Inside a TJ array, a sufficiently negative kern means the renderer moved far
# enough right to be a word gap. PDFs rarely store spaces as actual space bytes.
_KERN_RE = re.compile(rb"-?\d+(?:\.\d+)?")
WORD_GAP = 120.0


def _text_from_content(content: bytes) -> str:
    """Pull the shown strings out of a content stream, in order."""
    pieces: list[str] = []
    index, length = 0, len(content)
    array_depth = 0
    while index < length:
        char = content[index:index + 1]
        if char == b"[":
            array_depth += 1
            index += 1
            continue
        if char == b"]":
            array_depth = max(0, array_depth - 1)
            index += 1
            continue
        if array_depth and (char.isdigit() or char == b"-"):
            match = _KERN_RE.match(content, index)
            if match:
                try:
                    if float(match.group(0)) <= -WORD_GAP:
                        pieces.append(" ")
                except ValueError:
                    pass
                index = match.end()
                continue
        if char == b"(":
            raw, index = _read_literal(content, index + 1)
            pieces.append(_decode(raw))
            continue
        if char == b"<" and content[index + 1:index + 2] != b"<":
            raw, index = _read_hex(content, index + 1)
            pieces.append(_decode(raw))
            continue
        # Positioning operators end a run of text; treat them as whitespace so
        # words from different lines do not fuse into one token.
        if char == b"T" and content[index:index + 2] in (b"Td", b"TD", b"T*", b"Tw"):
            pieces.append("\n")
            index += 2
            continue
        if char in (b"'", b'"'):
            pieces.append("\n")
            index += 1
            continue
        index += 1
    return "".join(pieces)


def _mostly_printable(chunk: bytes, threshold: float = 0.80) -> bool:
    """Content streams are ASCII operators; image and font data is not."""
    if not chunk:
        return False
    good = sum(1 for byte in chunk if 32 <= byte <= 126 or byte in (9, 10, 13))
    return good / len(chunk) >= threshold


def extract_text(data: bytes, max_streams: int = 400) -> str:
    """Best-effort plain text from a PDF byte string."""
    if not data or not data.lstrip()[:5].startswith(b"%PDF"):
        return ""
    if len(data) > MAX_PDF_BYTES:
        data = data[:MAX_PDF_BYTES]

    out: list[str] = []
    for count, chunk in enumerate(_iter_streams(data)):
        if count >= max_streams:
            break
        content = _inflate(chunk)
        if not content:
            # Some PDFs store content streams uncompressed.
            content = chunk if _TEXT_OPS.search(chunk[:4000]) else b""
        if not content or not _TEXT_OPS.search(content[:8000]):
            continue
        if not _mostly_printable(content[:4000]):
            continue  # image or font data that happens to contain "Tj"/"BT"
        try:
            out.append(_text_from_content(content))
        except Exception:
            continue

    text = "\n".join(out)
    # Collapse the ragged whitespace that positioning operators leave behind.
    text = re.sub(r"[ \t\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def looks_like_pdf(data: bytes) -> bool:
    return bool(data) and data.lstrip()[:5].startswith(b"%PDF")
