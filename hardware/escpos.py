"""
ESC/P printer-stream cleanup for the BPS C1's report output.

The C1 drives its report through a receipt-printer command stream, not plain
text: alongside the report text it emits ESC/P setup commands and raster
(bitmap) blocks for logos/rules. A raster block is arbitrary binary data of a
length only the command header tells you, so it must be skipped by byte count
before the stream is decoded as text -- decoding first and filtering
afterwards can't reliably tell picture bytes from report bytes once they've
both become "characters".

This mirrors the cleanup logic of the standalone capture bridge that was
proven against a real C1 (see the BPS C1 connection spec): keep printable
ASCII and line breaks, turn a short list of known control bytes into spaces,
and drop everything else, including the full extent of any raster payload.
"""

from __future__ import annotations

import re

_ESC = 0x1B
_RASTER_CMD = ord("*")          # ESC * mode nL nH [raster bytes]
_ONE_ARG_CMDS = {ord("a"), ord("d"), ord("J")}
_NO_ARG_CMDS = {ord("@"), ord("i")}
_SPACE_BYTES = {0x02, 0x03, 0x04, 0x1C}
_WHITESPACE_RUN = re.compile(r"[ \t]+")


def strip_escpos(data: bytes) -> str:
    """Remove ESC/P commands and raster blocks, returning normalised text."""
    out = bytearray()
    i = 0
    n = len(data)

    while i < n:
        b = data[i]

        if b == _ESC:
            if i + 1 >= n:
                break  # a command byte was cut off mid-stream; nothing usable left
            cmd = data[i + 1]

            if cmd == _RASTER_CMD:
                if i + 4 >= n:
                    break
                mode, nl, nh = data[i + 2], data[i + 3], data[i + 4]
                columns = nl + 256 * nh
                factor = 3 if mode in (32, 33) else 1
                i += 5 + columns * factor
                continue

            if cmd in _ONE_ARG_CMDS:
                i += 3
                continue

            if cmd in _NO_ARG_CMDS:
                i += 2
                continue

            i += 2  # unrecognised ESC command: discard ESC + the byte after it
            continue

        if 32 <= b <= 126 or b in (0x0D, 0x0A):
            out.append(b)
        elif b in _SPACE_BYTES:
            out.append(0x20)
        # anything else (raw control bytes, high-byte noise) is dropped

        i += 1

    text = out.decode("ascii", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WHITESPACE_RUN.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)
