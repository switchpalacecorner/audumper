#!/usr/bin/env python3
r"""Push files into the handset's data folder over the au Music Port protocol.

Frames are aulink.py; the USB identity and baud come from the model manifest.

    Put File   param 0x10   -> internal data folder

The phone must be at standby in data-transfer mode, not mass storage. A
push re-handshakes the link, so push everything before opening anything.
"""
from . import aulink as A

P_FILE = 0x10        # internal data folder

BAUD_DEFAULT = 0x00038400        # 230400; the manifest overrides it


class Pusher(A.Master):

    def wait_ctl(self):
        """expect_ctl, but WACK (0x07) means 'wait', not 'no'."""
        for _ in range(40):
            r = self.expect_ctl(15.0)
            if r is None:
                return None
            if r[0] == A.WACK:
                continue
            return r
        return None

    def put(self, name, body, param=P_FILE):
        nb = name.encode('ascii')
        val = nb + b'\x00' + len(body).to_bytes(4, 'big')
        fr = A.build(A.OP_PUT, param, val)
        assert len(fr) == len(nb) + 0x0F, (len(fr), len(nb) + 0x0F)
        if A.VERBOSE:
            where = 'internal' if param == P_FILE else 'EXTERNAL(SD)'
            print('  Put %-12s name=%r %d bytes'
                  % (where, name, len(body)))
        self.w(fr, 'Put File')
        r = self.wait_ctl()
        if r is None:
            print('  !! no response to the Put File frame')
            return False
        if r[0] != A.ACK:
            print('  !! %s -- refused before any body'
                  % A.CTL_NAMES.get(r[0], hex(r[0])))
            return False

        chunk = self.framesize - 8
        n = max(len(body), 1)
        total = (n + chunk - 1) // chunk
        for i in range(0, n, chunk):
            piece = body[i:i + chunk]
            last = (i + chunk >= len(body))
            ln = len(piece) + 4
            f = bytearray([0x01, 0x80, (ln >> 8) & 0xFF, ln & 0xFF, A.STX])
            f += piece
            f.append(A.ETX if last else A.ETB)
            v = A.bcc_of(f)
            f += bytes([(v >> 8) & 0xFF, v & 0xFF])
            self.ser.write(bytes(f))
            self.ser.flush()
            r = self.wait_ctl()
            idx = i // chunk + 1
            if r is None:
                print('  !! SILENCE after body frame %d/%d' % (idx, total))
                return False
            if r[0] != A.ACK:
                print('  !! %s on body frame %d/%d'
                      % (A.CTL_NAMES.get(r[0], hex(r[0])), idx, total))
                return False
        if A.VERBOSE:
            print('     %d frame(s) ACKed' % total)
        return True

    #   `link.setup` in the manifest may name these instead of relying on
    #   the default sequence below. Parameter names, not numbers: a manifest
    #   should not have to know the protocol's opcodes.
    PARAMS = {'protocol': A.P_PROTOCOL, 'baud': A.P_BAUD,
              'framesize': A.P_FRAMESIZE, 'parity': A.P_PARITY}
    OPS = {'set': A.OP_SET, 'put': A.OP_PUT}

    def default_setup(self, baud, framesize, parity):
        """The Set sequence every handset checked so far accepts.

        Parity is off by default: they answer SNAK to it.
        """
        seq = [('protocol', 'set', 'protocol', bytes([0x00, 0x01, 0x00, 0x00])),
               ('baud', 'set', 'baud', baud.to_bytes(4, 'big')),
               ('framesize', 'set', 'framesize', framesize.to_bytes(4, 'big'))]
        if parity:
            seq.append(('parity', 'set', 'parity', bytes([0x00])))
        return seq

    def init(self, baud=BAUD_DEFAULT, framesize=0x1000, parity=False,
             setup=None):
        """ENQ, then the setup sequence"""
        if A.VERBOSE:
            print('[init] baud %d, framesize 0x%X%s'
                  % (baud, framesize, ', parity' if parity else ''))
        self.w(bytes([A.ENQ]), 'ENQ')
        if self.expect_ctl(4.0) is None:
            return False
        raw = setup or self.default_setup(baud, framesize, parity)
        seq = [(n, self.OPS[o] if isinstance(o, str) else o,
                self.PARAMS[p] if isinstance(p, str) else p,
                bytes.fromhex(v) if isinstance(v, str) else v)
               for n, o, p, v in raw]
        for name, op, pm, val in seq:
            r = self.send_param(op, pm, val)
            if r is None or r[0] != A.ACK:
                print('     !! %s not ACKed' % name)
                return False
        self.framesize = framesize
        return True


#   `link.protocol` selects one of these. One entry today; a model reached
#   by some other means adds its own class here rather than editing run().
PROTOCOLS = {'au-music-port': Pusher}
