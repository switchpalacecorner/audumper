#!/usr/bin/env python3
r"""au PC-link USB transport for Linux.

The PC-link is an MDLM interface (class 02 subclass 0A). cdc_acm binds only
subclass 02 (ACM), so it declines this one and no driver is needed: the
interface is driven as a raw bulk pair through libusb.

A model that exposed its PC-link as real ACM would be bound by cdc_acm and
would need a tty path instead. None does, and open_bulk() detaches a bound
kernel driver anyway, reattaching it on exit.

open_link() returns an object with .write() .read(n) .flush(), which is
the whole surface aulink.Master needs. Identity comes from the model
manifest; `pid` is a list so a model with more than one candidate mode
reports which answered instead of failing on a coin flip.
"""
import sys

REQUIRED = ('name', 'vid', 'pid', 'intf', 'ep_out', 'ep_in', 'mode')

#   How bytes move. Every handset so far is an MDLM bulk pair, but the
#   manifest names it so a model that is reached some other way needs a
#   new entry in TRANSPORTS and no change to anything above.
DEFAULT_TRANSPORT = 'bulk'

#   Descriptor-layout notes. Off for an ordinary run: the chooser takes the
#   first bulk pair at or after the manifest's interface and that is what it
#   uses, so a manifest whose intf/ep numbers are merely UNCONFIRMED would
#   otherwise warn twice on every run -- once at the push, once at the listen.
VERBOSE = False


def _num(v):
    """JSON has no hex, so a manifest may write 0x0D1C as a string."""
    return int(v, 0) if isinstance(v, str) else int(v)


def spec(usb):
    """-> a complete transport spec from the model manifest's `usb` block."""
    if not usb:
        raise ValueError('no usb block in the model manifest')
    missing = [k for k in REQUIRED if k not in usb]
    if missing:
        raise ValueError('model manifest usb block is missing %s'
                         % ', '.join(missing))
    d = dict(usb)
    d.setdefault('transport', DEFAULT_TRANSPORT)
    if not isinstance(d['pid'], (list, tuple)):
        d['pid'] = [d['pid']]
    d['vid'] = _num(d['vid'])
    d['pid'] = [_num(p) for p in d['pid']]
    for k in ('intf', 'ep_out', 'ep_in'):
        d[k] = _num(d[k])
    return d


def ids(usb=None):
    """-> 'NAME (vvvv:pppp[/pppp...])', for messages."""
    s = spec(usb)
    return '%s (%04x:%s)' % (s['name'], s['vid'],
                             '/'.join('%04x' % p for p in s['pid']))


# -------------------------------------------------------------- bulk backend
class BulkLink(object):
    """The three pyserial methods, over a libusb bulk pair."""

    def __init__(self, dev, intf, ep_out, ep_in, detached):
        self.dev, self.intf = dev, intf
        self.ep_out, self.ep_in = ep_out, ep_in
        self._detached = detached
        self._buf = bytearray()

    def write(self, b):
        for i in range(0, len(b), 64):
            self.dev.write(self.ep_out, b[i:i + 64], timeout=3000)
        return len(b)

    def flush(self):
        pass

    def read(self, n=4096):
        """Short-timeout read, the shape aulink.Master.fill() expects."""
        if not self._buf:
            try:
                # 4096 not 512: every round trip is a window with no
                # URB outstanding, i.e. backpressure on the handset.
                self._buf += bytes(self.dev.read(self.ep_in, 4096, timeout=50))
            except Exception:
                pass
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def close(self):
        import usb.util
        try:
            usb.util.release_interface(self.dev, self.intf)
        except Exception:
            pass
        if self._detached:
            try:
                self.dev.attach_kernel_driver(self.intf)
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class NoBulkPair(Exception):
    """The device is on the bus, but not laid out the way the manifest says."""


def _find_dev(s):
    """-> (device, pid) for the first candidate PID on the bus."""
    import usb.core
    for p in s['pid']:
        dev = usb.core.find(idVendor=s['vid'], idProduct=p)
        if dev is not None:
            return dev, p
    return None, None



def open_bulk(usb):
    s = spec(usb)
    dev, pid = _find_dev(s)
    if dev is None:
        return None
    if len(s['pid']) > 1:
        print('  [bulk] %s answered on PID %04x' % (s['name'], pid))
    cfg = dev.get_active_configuration()

    # The PC-link function is comm interface MI_02 plus its data interface;
    # the bulk pair lives on the data one. Take the first interface at or
    # after the PC-link one carrying both a bulk IN and a bulk OUT.
    chosen = None
    for intf in cfg:
        if intf.bInterfaceNumber < s['intf']:
            continue
        ins = [e.bEndpointAddress for e in intf
               if e.bEndpointAddress & 0x80 and (e.bmAttributes & 3) == 2]
        outs = [e.bEndpointAddress for e in intf
                if not (e.bEndpointAddress & 0x80) and (e.bmAttributes & 3) == 2]
        if ins and outs:
            chosen = (intf.bInterfaceNumber, outs[0], ins[0])
            break
    if chosen is None:
        print('!! %s is on the bus, but has no bulk pair at or after '
              'interface %d.' % (s['name'], s['intf']))
        print('   Check intf/ep_out/ep_in in this model\'s model.json,')
        print('   and that the phone is in the right USB mode:')
        print('     %s' % s['mode'])
        print('   Another mode can enumerate on the same PID.')
        raise NoBulkPair(s['name'])
    n, ep_out, ep_in = chosen
    if VERBOSE and (n, ep_out, ep_in) != (s['intf'], s['ep_out'], s['ep_in']):
        print('  !! layout differs from the one this model expects '
              '(intf %d, OUT 0x%02X, IN 0x%02X); using intf %d, OUT 0x%02X, '
              'IN 0x%02X.'
              % (s['intf'], s['ep_out'], s['ep_in'], n, ep_out, ep_in))
        print('     Continuing, but fix model.json before trusting a '
              'silent handset.')

    detached = False
    try:
        if dev.is_kernel_driver_active(n):
            dev.detach_kernel_driver(n)
            detached = True
            print('  detached kernel driver from interface %d '
                      '(reattached on exit)' % n)
    except Exception as e:
        print('  (interface %d: %s)' % (n, e))

    print('[bulk] intf %d  OUT 0x%02X  IN 0x%02X' % (n, ep_out, ep_in))
    return BulkLink(dev, n, ep_out, ep_in, detached)


# ------------------------------------------------------------------ frontend
TRANSPORTS = {'bulk': None}         # filled below; open_bulk is defined above


def open_link(usb):
    """-> a link to the handset, or None. `usb` is the model's transport
    block; `usb.transport` picks the mechanism."""
    if not sys.platform.startswith('linux'):
        print('!! this transport is for Linux')
        return None
    kind = spec(usb)['transport']
    if kind not in TRANSPORTS:
        print('!! unknown usb.transport %r; installed: %s'
              % (kind, ', '.join(sorted(TRANSPORTS))))
        return None

    try:
        __import__('usb.core')          # not `import`: it would shadow `usb`
    except ImportError:
        print('!! pyusb is missing. From the tool directory:')
        print('     sudo python3 -m venv .venv')
        print('     sudo .venv/bin/pip install -r requirements.txt')
        print('   then run with  sudo .venv/bin/python  rather than python3.')
        return None

    try:
        link = TRANSPORTS[kind](usb)
    except NoBulkPair:
        return None                     # open_bulk said exactly what is wrong
    if link is None:
        print('!! %s not found on USB.' % ids(usb))
        print('   Check: the phone is at standby, and its USB mode is')
        print('          %s' % spec(usb)['mode'])
        print('          (NOT mass storage -- that shows a disk, not the'
              ' port)')
    return link


TRANSPORTS['bulk'] = open_bulk
