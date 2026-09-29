#!/usr/bin/env python3
"""au Music Port frame protocol, PC as master.

The handshake:

    8E                          -> nibble digits, 0xAA          (TelNoGet)
    B3 0D + 36-byte PcCmnState  -> 6C 0D + 36 bytes             (CmnStateGet)
    D0 01 05                    -> D0 01 06                     (BinMove)
    05 (ENQ)                    -> 06 (ACK)
    then framed Set/Get with the PC driving.

Phone must be IDLE: USB接続設定 = データ転送, back at standby, データ転送モード
NOT open.  In that state the whitelist state byte at *(ctx+8)+0x11 is still 3.

The init Set values:

    Set ProtocolVersion  param 00  value 00 01 00 00
    Set BaudRate         param 01  value 00 03 84 00   (230400)
    Set FrameSize        param 03  value 00 00 10 00   (4096)
    Set ParityBit        param 02  value 00            (one byte)
    Get Profile          param 04  value 00 00         (two bytes)
"""
import time

ENQ, ACK, EOT, NAK, STX, ETX, ETB, WACK, SNAK = (
    0x05, 0x06, 0x04, 0x15, 0x02, 0x03, 0x17, 0x07, 0x18)
CTL_NAMES = {ENQ: "ENQ", ACK: "ACK", EOT: "EOT", NAK: "NAK",
             WACK: "WACK", SNAK: "SNAK", 0x08: "0x08(?)"}

OP_SET, OP_PUT = 0x00, 0x01
P_PROTOCOL, P_BAUD, P_PARITY, P_FRAMESIZE = 0, 1, 2, 3

PARAM_NAMES = {0: "ProtocolVersion", 1: "BaudRate", 2: "ParityBit",
               3: "FrameSize", 4: "Profile", 0x10: "File", 0x11: "Dir",
               0x12: "FileInfo", 0x13: "DirInfo", 0x14: "FileList",
               0x15: "DirList", 0x20: "PIM", 0x21: "LockNo",
               0xB0: "ExFile", 0xB1: "ExDir", 0xB2: "ExFileInfo",
               0xB3: "Ex3", 0xB4: "Ex4", 0xB5: "Ex5"}

PC_CMN_STATE = bytearray(36)
PC_CMN_STATE[1] = 0x02
PC_CMN_STATE[3] = 0x07
PC_CMN_STATE[5] = 0x0A
PC_CMN_STATE[9] = 0x07
for _i in range(0x0A, 0x14):
    PC_CMN_STATE[_i] = 0x0F

#   Frame-by-frame logging. Off for an ordinary run
VERBOSE = False

TELNO_END = 0xAA        # terminates the TelNoGet digit stream

#   How long a stream may go quiet before drain() gives up waiting for a
#   terminator. Far longer than any plausible inter-byte gap, so it cannot cut
#   a live trickle short, but it stops a model that sends no terminator from
#   burning the whole limit on every step.
IDLE_GAP = 3.0

TELNO_MAP = {0x91: '1', 0x92: '2', 0x93: '3', 0x94: '4', 0x95: '5',
             0x96: '6', 0x97: '7', 0x98: '8', 0x99: '9', 0x9A: '0',
             0x9B: '*', 0x9C: '#', 0x9D: 'A', 0x9E: 'P', 0x9F: '-'}


def bcc_of(f):
    n = ((f[2] << 8) | f[3]) + 2
    return (~sum(f[:n])) & 0xFFFF


def build(op, param, value=b""):
    data = bytes([op, param]) + value
    ln = len(data) + 4
    f = bytearray([0x01, 0x00, (ln >> 8) & 0xFF, ln & 0xFF, STX])
    f += data
    f.append(ETX)
    v = bcc_of(f)
    f += bytes([(v >> 8) & 0xFF, v & 0xFF])
    return bytes(f)


class Master:
    def __init__(self, ser):
        self.ser = ser
        self.buf = bytearray()
        self.log = bytearray()
        self.framesize = 4096

    def w(self, b, label=""):
        self.ser.write(b); self.ser.flush()
        if VERBOSE:
            show = b.hex() if len(b) <= 48 else b[:44].hex() + "..."
            print("  -> %-14s %s" % (label, show), flush=True)

    def fill(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            c = self.ser.read(4096)
            if c:
                self.buf += c
                self.log += c
                return True
            time.sleep(0.005)
        return False

    def take(self, timeout=5.0):
        """Return (bytes, kind) where kind is ctl|param|data|d0|junk."""
        end = time.time() + timeout
        while True:
            if self.buf:
                b0 = self.buf[0]
                if b0 == 0xD0:
                    if len(self.buf) >= 3:
                        f = bytes(self.buf[:3]); del self.buf[:3]
                        return f, "d0"
                elif b0 == 0x01:
                    if len(self.buf) >= 4:
                        ln = (self.buf[2] << 8) | self.buf[3]
                        if len(self.buf) >= 4 + ln:
                            f = bytes(self.buf[:4 + ln]); del self.buf[:4 + ln]
                            return f, ("data" if f[1] == 0x80 else "param")
                else:
                    f = bytes(self.buf[:1]); del self.buf[:1]
                    return f, "ctl"
            if time.time() >= end:
                return None, None
            if not self.fill(0.25):
                continue

    def expect_ctl(self, timeout=5.0):
        f, k = self.take(timeout)
        if f is None:
            if VERBOSE:
                print("  <- (silence)", flush=True)
            return None
        if VERBOSE:
            if k == "ctl":
                print("  <- %s" % CTL_NAMES.get(f[0], hex(f[0])),
                      flush=True)
            else:
                print("  <- %s %s" % (k, f[:40].hex()), flush=True)
        return f

    # ---- handshake -------------------------------------------------------
    def drain(self, until=None, first=4.0, quiet=0.8, limit=20.0):
        deadline = time.time() + limit
        seen = bool(self.buf)
        last = time.time()
        while time.time() < deadline:
            n = len(self.buf)
            self.fill(0.1)
            grew = len(self.buf) > n
            if grew:
                seen = True
                last = time.time()
            if until is not None:
                if until(bytes(self.buf)):
                    break
                if seen and not grew and time.time() - last >= IDLE_GAP:
                    break                # stopped without a terminator
                continue
            if not grew and time.time() - last >= (quiet if seen else first):
                break
        rx = bytes(self.buf)
        self.buf.clear()
        return rx

    def _step(self, send, label, want, name):
        self.w(send, label)
        rx = self.drain(until=lambda b: want in b)
        i = rx.find(want)
        extra = ''
        if i > 0:
            digits = ''.join(TELNO_MAP.get(b, '') for b in rx[:i])
            extra = ("   (%d leading byte(s) were an earlier step's tail%s)"
                     % (i, ', digits %r' % digits if digits else ''))
        if VERBOSE:
            print('  <- %s   %s%s'
                  % (rx.hex(),
                     '%s at +%d' % (name, i) if i >= 0 else 'no ' + name,
                     extra), flush=True)
        return i >= 0

    def telno(self):
        """TelNoGet. The reply is a STREAM of digit bytes ending in 0xAA, not
        one packet. Reading it as one packet desyncs everything after it."""
        self.w(b"\x8e", "8E")
        rx = self.drain(until=lambda b: TELNO_END in b)
        digits = "".join(TELNO_MAP.get(b, "") for b in rx)
        junk = [b for b in rx if b not in TELNO_MAP and b != TELNO_END]
        if VERBOSE:
            print("  <- %s   digits=%r%s%s"
                  % (rx.hex(), digits,
                     '  0xAA terminator' if TELNO_END in rx else
                     '  NO 0xAA -- the stream may still be running',
                     '  unexpected %s' % bytes(junk).hex() if junk else ''),
                  flush=True)
        return TELNO_END in rx or bool(digits)

    def cmnstate(self):
        return self._step(b"\xb3\x0d" + bytes(PC_CMN_STATE), "B3 0D+36",
                          b"\x6c\x0d", "6C 0D")

    def binmove(self):
        return self._step(b"\xd0\x01\x05", "D0 01 05",
                          b"\xd0\x01\x06", "D0 01 06")

    def handshake(self):
        if VERBOSE:
            print("[handshake]")
        stale = self.drain(first=0.5, quiet=0.4, limit=3.0)
        if stale and VERBOSE:
            print("  (discarded %d stale byte(s): %s)"
                  % (len(stale), stale.hex()), flush=True)
        ok1 = self.telno()
        ok2 = self.cmnstate()
        ok3 = self.binmove()
        if ok3 and not VERBOSE:
            print("[handshake] ok")
        else:
            print("[handshake] telno=%s cmnstate=%s binmove=%s"
                  % (ok1, ok2, ok3))
        return ok3

    # ---- framed layer ----------------------------------------------------
    def send_param(self, op, param, value=b""):
        label = "%s %s" % ({0: "Set", 1: "Put", 2: "Get"}.get(op, hex(op)),
                           PARAM_NAMES.get(param, hex(param)))
        self.w(build(op, param, value), label)
        return self.expect_ctl()
