#!/usr/bin/env python3
r"""The block format every payload sends.

    +0  magic | seq      which stream this block belongs to
    +4  tag              seq-dependent: a unit index, or a file offset
    +8  length           body bytes, always a multiple of 4
    +C  checksum         sum of the body's 32-bit words, mod 2^32
"""
import struct

MAX_BODY = 1 << 20


def wordsum(body):
    return sum(struct.unpack('<%dI' % (len(body) // 4), body)) & 0xFFFFFFFF


def parse(buf, magic):
    """Yield (seq, tag, ok, body) for every block in buf.

    buf may be bytes or an mmap; captures run to hundreds of MB. magic is the
    top half of the header word, a property of the payload, from the manifest.
    """
    magic &= 0xFFFF0000
    n = len(buf)
    i = 0
    while i + 16 <= n:
        w, tag, ln, chk = struct.unpack_from('<IIII', buf, i)
        if (w & 0xFFFF0000) != magic or ln == 0 or ln > MAX_BODY or ln % 4:
            i += 1
            continue
        if i + 16 + ln > n:
            break                       # a block cut short by the end of file
        body = bytes(buf[i + 16:i + 16 + ln])
        yield (w & 0xFFFF), tag, wordsum(body) == chk, body
        i += 16 + ln
