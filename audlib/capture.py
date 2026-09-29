#!/usr/bin/env python3
r"""Receive the phone's stream to a file, framed by the payload's markers.

Waiting for the start marker keeps anything left on the wire from the
file transfer step out of the dump. stopping on the end marker reads completion
rather than inferring it from silence. A marker can arrive split across two
reads, so both searches keep a tail.

Nothing else is interpreted here. Flushing is rare on purpose: the link has
no backpressure, and syscalls per read are enough to overrun the kernel's USB
buffer and lose a burst.
"""
import os
import sys
import time

FLUSH_EVERY = 4 << 20


def _mb(n):
    return n / 1048576.0


WAIT_DOTS = ('.  ', ' . ', '  .')
WAIT_EVERY = 0.35
#   There is no timeout before the stream starts -- the operator may take as
#   long as they like to find and open the files -- so the way out has to be
#   on screen.
WAIT_HINT = '   (ctrl-C to cancel)'


def capture(link, out_path, start_marker, stop_marker, quiet_stop=90.0,
            expected=0, waiting=None):
    """Record to out_path. Returns (bytes_written, why_it_stopped, synced).

    out_path and its directory are created only when the first byte is
    recorded, so a run that never reaches the handset leaves nothing behind.

    expected is how many bytes the payload will send, and turns the progress
    line into a percentage. Pass it ONLY when it is derived rather than guessed
    -- a NAND dump sends one block per page and one per spare area, so the chip
    geometry gives the number exactly. A filesystem sweep's size depends on
    what is on the handset, so it has no expected value and shows MB only. A
    percentage against a guess is a worse readout than no percentage.
    """
    start = start_marker.encode('latin1') if start_marker else b''
    stop = stop_marker.encode('latin1') if stop_marker else b''
    synced = not start
    pre = bytearray()
    tail = bytearray()
    dropped = total = flushed = 0
    painted = 0.0
    wait_at, wait_n = 0.0, 0
    t0 = time.time()
    t_first = t_last = None
    why = 'interrupted'

    def stop_waiting():
        """Erase the animated line, once, when there is something to replace
        it with. Stopping it merely because SOME bytes arrived leaves the
        screen blank until the start marker shows up, which may be a while."""
        if waiting and wait_n:
            width = len(waiting) + len(WAIT_DOTS[0]) + len(WAIT_HINT)
            sys.stdout.write('\r%s\r' % (' ' * width))
            sys.stdout.flush()

    f = None
    try:
        while True:
            now = time.time()
            if total and quiet_stop and now - t_last > quiet_stop:
                why = '%.0fs of quiet' % quiet_stop
                break
            chunk = link.read(65536)
            if not chunk:
                # No sleep: the bulk read already blocks 50 ms and
                # returns empty on timeout, so the device paces this
                # loop. Sleeping would leave the IN endpoint with no
                # URB outstanding and drop bytes.
                if waiting and now - wait_at > WAIT_EVERY:
                    wait_at = now
                    sys.stdout.write('\r%s%s%s'
                                     % (waiting, WAIT_DOTS[wait_n % 3],
                                        WAIT_HINT))
                    sys.stdout.flush()
                    wait_n += 1
                continue

            if not synced:
                pre += chunk
                at = pre.find(start)
                if at < 0:
                    if len(pre) > len(start):
                        keep = len(start) - 1
                        dropped += len(pre) - keep
                        del pre[:len(pre) - keep]
                    continue
                synced = True
                dropped += at
                chunk = bytes(pre[at:])
                pre = bytearray()
                stop_waiting()
                waiting = None
                print('  [%.1fs] stream started%s'
                      % (now - t0,
                         '' if not dropped else
                         ', %d stray byte(s) discarded' % dropped))
            if t_first is None:
                stop_waiting()          # no start marker: the data IS the cue
                waiting = None
                t_first = now
            t_last = now
            if f is None:
                d = os.path.dirname(out_path)
                if d:
                    os.makedirs(d, exist_ok=True)
                f = open(out_path, 'wb')
            total += len(chunk)
            f.write(chunk)
            if total - flushed >= FLUSH_EVERY:
                f.flush()
                flushed = total
            if now - painted >= 0.5:
                rate = total / max(0.001, now - t_first)
                if expected:
                    sys.stdout.write(
                        '\r  %8.1f / %.1f MB  %5.1f%%  %6.0f KB/s  '
                        % (_mb(total), _mb(expected),
                           100.0 * total / expected, rate / 1024.0))
                else:
                    sys.stdout.write('\r  %8.1f MB  %6.0f KB/s  '
                                     % (_mb(total), rate / 1024.0))
                sys.stdout.flush()
                painted = now

            if stop:
                tail += chunk
                if stop in tail:
                    why = 'end marker'
                    break
                if len(tail) > len(stop):
                    del tail[:len(tail) - (len(stop) - 1)]
    except KeyboardInterrupt:
        why = 'interrupted by Ctrl-C'
    finally:
        if f is not None:
            f.close()

    span = (t_last - t_first) if (t_first and t_last) else 0.0
    rate = (total / span / 1024.0) if span else 0.0
    if expected:
        print('\r  %8.1f / %.1f MB  %5.1f%%  %6.0f KB/s  -- stopped: %s'
              % (_mb(total), _mb(expected), 100.0 * total / expected,
                 rate, why))
    else:
        print('\r  %8.1f MB  %6.0f KB/s  -- stopped: %s'
              % (_mb(total), rate, why))
    if start and not synced:
        print('  !! the start marker never arrived, so nothing was'
              ' recorded.')
    return total, why, synced
