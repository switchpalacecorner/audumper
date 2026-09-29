#!/usr/bin/env python3
r"""audumper -- pull data off a phone through its document viewer.

    sudo python3 audumper.py --p MODEL COMMAND
    python3 audumper.py --list

Pushes the command's files over USB, tells you to open them on the handset,
waits for the stream and rebuilds the result into a new run folder.

The handset must be at standby in data-transfer mode, not mass storage, with
no transfer screen open. Leave it alone once the stream starts.

sudo is for raw USB access; see README.txt for the udev rule instead.
"""
import argparse
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from audlib import aulink                                         # noqa: E402
from audlib import capture as audcapture                           # noqa: E402
from audlib import fsbuild, recon                             # noqa: E402
from audlib import usbtty                                         # noqa: E402
from audlib.pushlink import PROTOCOLS                             # noqa: E402

DEFAULT_PROTOCOL = 'au-music-port'

PHONES = os.path.join(HERE, 'phones')

REQUIRED_MODEL_KEYS = ('usb', 'link', 'block_magic', 'file_location')


def _num(v):
    """JSON has no hex literals, so a manifest may write "0xCA030000"."""
    return int(v, 0) if isinstance(v, str) else int(v)


def model_spec(model):
    """-> the model's manifest. Every model must ship a complete one."""
    p = os.path.join(PHONES, model, 'model.json')
    if not os.path.exists(p):
        raise SystemExit('!! %s not found in supported models' % model)
    #   encoding is not optional: file_location may be Japanese, and a bare
    #   open() uses the locale, which sudo usually strips down to ASCII
    m = json.load(open(p, encoding='utf-8'))
    missing = [k for k in REQUIRED_MODEL_KEYS if k not in m]
    if missing:
        raise SystemExit('!! %s/model.json is missing %s'
                         % (model, ', '.join(missing)))
    link = dict(m['link'])
    for k in ('baud', 'framesize'):
        if k not in link:
            raise SystemExit('!! %s/model.json link is missing %s'
                             % (model, k))
        link[k] = _num(link[k])
    link.setdefault('protocol', DEFAULT_PROTOCOL)
    link.setdefault('handshake', True)
    link.setdefault('parity', False)
    link.setdefault('setup', None)      # None = the protocol's own sequence

    #   How the payload reaches the handset. 'push' is the au file push every
    #   model uses today; 'none' is for a model whose payload arrives some
    #   other way -- a USB-level bug, a dev command -- and which therefore
    #   needs no link setup and no operator file-opening step either.
    dl = dict(m.get('delivery') or {'kind': 'push'})
    dl.setdefault('kind', 'push')
    dl['param'] = _num(dl.get('param', 0x10))

    return {'usb': m['usb'],
            'file_location': m['file_location'],
            'block_magic': _num(m['block_magic']),
            'link': link,
            'delivery': dl}


def push_stem(command):
    """-> the name payloads are pushed under, from the command: NAND, FS."""
    s = command.upper().replace('DUMP_', '', 1)
    return ''.join(c for c in s if c.isalnum()) or 'FILE'


def push_name(source, i, command):
    """-> the name file i is pushed under, e.g. dump_nand #0 -> NAND01.MHT.

    Named after the COMMAND so the operator sees what they are opening. The
    EXTENSION is kept from the source: the data folder picks a viewer by
    extension, and an .MHT renamed to anything else is never offered to the
    document viewer at all.
    """
    return '%s%02d%s' % (push_stem(command), i + 1,
                         os.path.splitext(source)[1].upper())


def commands():
    """-> {model: {command: descriptor}} for everything installed."""
    out = {}
    if not os.path.isdir(PHONES):
        return out
    for model in sorted(os.listdir(PHONES)):
        md = os.path.join(PHONES, model)
        if not os.path.isdir(md):
            continue
        for cmd in sorted(os.listdir(md)):
            cj = os.path.join(md, cmd, 'command.json')
            if os.path.exists(cj):
                d = json.load(open(cj, encoding='utf-8'))
                d['_dir'] = os.path.join(md, cmd)
                d['_cmd'] = cmd
                out.setdefault(model, {})[cmd] = d
    return out


INVOCATION = 'sudo .venv/bin/python audumper.py'


def show_list(avail):
    print('audumper - installed commands\n')
    if not avail:
        print('  nothing under %s' % PHONES)
        return
    for model, cmds in avail.items():
        print('  %s' % model)
        for name, d in cmds.items():
            print('    %-12s %s' % (name, d.get('title', '')))
    print('\n  %s --p %s %s' % (INVOCATION, model, name))
    print('  %s --p %s --help      everything about one phone'
          % (INVOCATION, model))


def _mib(n):
    return '%.1f MiB' % (n / 1048576.0)


def show_model(model, mod, cmds):
    raw = json.load(open(os.path.join(PHONES, model, 'model.json'),
                         encoding='utf-8'))

    print('audumper - %s\n' % model)
    print('Device')
    print('  %s' % (raw.get('title') or usbtty.ids(mod['usb'])))
    print()

    print('Commands')
    if not cmds:
        print('  none installed for this phone')
    for name, d in cmds.items():
        print('  %-12s %s' % (name, d.get('title', '')))
        r = d.get('reconstruct')
        if isinstance(r, dict) and r.get('kind') == 'indexed':
            outs = ', '.join('%s (%s)' % (o['file'],
                                          _mib(int(r['units']) * o['stride']))
                             for o in r['outputs'])
            print('  %-12s produces %s' % ('', outs))
        elif isinstance(r, dict) and r.get('kind') == 'fs':
            print('  %-12s produces phone_fs/, the handset\'s files as a tree'
                  % '')
        print('  %-12s %s --p %s %s' % ('', INVOCATION, model, name))
        print()

    print('On the phone')
    print('  Navigate in settings to')
    print('    %s' % usbtty.spec(mod['usb'])['mode'])
    print('  then come back to standby with no transfer screen open.')
    print()
    print('  The pushed files appear at')
    print('    %s' % mod['file_location'])
    print('  Open them in the order the tool prints when it is ready.')


def expected_bytes(desc):
    """How many bytes the payload will deliver, or 0 if unknowable.

    A NAND dump is exactly one framed block per page plus one per spare area, so
    the geometry gives the answer. A filesystem sweep's size depends on what is
    on the phone, so there is no honest number and it gets none
    """
    r = desc.get('reconstruct')
    if isinstance(r, dict) and r.get('kind') == 'indexed':
        per = r.get('wire_bytes_per_unit')
        if per:
            return int(r['units']) * int(per)
        # no explicit figure: one framed record per distinct seq, sized by
        # the widest slice any output takes from it
        body = {}
        for o in r['outputs']:
            end = (o.get('take') or [0, o['stride']])[1] or o['stride']
            body[o['seq']] = max(body.get(o['seq'], 0), int(end))
        return int(r['units']) * sum(16 + b for b in body.values())
    return 0


def run(desc, outdir, framesize, mod, push=True):
    files = [os.path.join(desc['_dir'], f) for f in desc['push']]
    names = [push_name(os.path.basename(f), i, desc['_cmd'])
             for i, f in enumerate(files)]

    #   A model whose payload does not arrive as a pushed file has nothing to
    #   push and no link to set up -- it goes straight to listening. The
    #   command's open_order still says what the operator has to do, whatever
    #   that is for this route.
    delivered = mod['delivery']['kind'] != 'none'
    push = push and delivered

    if push:
        for f in files:
            if not os.path.exists(f):
                print('!! missing payload file: %s' % f)
                return 2

        link = mod['link']
        if link['protocol'] not in PROTOCOLS:
            print('!! unknown link.protocol %r; installed: %s'
                  % (link['protocol'], ', '.join(sorted(PROTOCOLS))))
            return 2
        if framesize is None:
            framesize = link['framesize']

        # ---- Every file goes over in one go, BEFORE anything is opened: a
        # push in the middle of a run re-handshakes the link and wrecks it.
        print('Connecting to %s...' % usbtty.ids(mod['usb']))
        conn = usbtty.open_link(mod['usb'])
        if conn is None:
            return 2
        try:
            p = PROTOCOLS[link['protocol']](conn)
            if link['handshake'] and not p.handshake():
                print('!! handshake failed. The phone must be at standby,'
                      ' with no transfer')
                print('   screen open, and its USB mode set to')
                print('     %s' % mod['usb']['mode'])
                print('   Re-run with -v to see the frames.')
                return 1
            # one init path; every value comes from the manifest
            ok = p.init(baud=link['baud'], framesize=framesize,
                        parity=link['parity'], setup=link['setup'])
            if not ok:
                print('!! init failed')
                return 1
            print()
            for i, f in enumerate(files):
                body = open(f, 'rb').read()
                if not p.put(names[i], body,
                             param=mod['delivery']['param']):
                    print('!! %s did not complete - nothing was run, safe to'
                          ' retry' % names[i])
                    return 1
                print('  pushed %-12s %7d bytes' % (names[i], len(body)))
                time.sleep(0.4)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---- tell them what to do, then start listening. The listener goes up
    # before they touch the handset, so the first bytes cannot be missed.
    cap = desc['capture']
    print()
    print('=' * 68)
    if push:
        print('  %d file(s) pushed: %s' % (len(names), ', '.join(names)))
    elif not delivered:
        print('  %s delivery: nothing to push.'
              % mod['delivery']['kind'])
    else:
        print('  --no-push: using the files already on the phone,')
        print('  %s' % ', '.join(names))
    print()
    if delivered:
        print('  On the phone: %s' % mod['file_location'])
        print('  Open them IN THIS ORDER:')
    print()
    for line in desc['open_order']:
        print('    %s' % line)
    print()
    print('=' * 68)
    print()

    link = usbtty.open_link(mod['usb'])
    if link is None:
        #   the only thing open_link cannot know: the push already landed
        print('   The files ARE on the phone. Fix the link and re-run;')
        print('   opening them without a listener does no harm.')
        return 2
    capf = os.path.join(outdir, 'capture.bin')
    try:
        total, why, synced = audcapture.capture(
            link, capf, cap['start_marker'], cap['stop_marker'],
            quiet_stop=float(cap.get('quiet_stop', 90)),
            expected=expected_bytes(desc),
            waiting='  Open the files in order now. Awaiting trigger')
    finally:
        try:
            link.close()
        except Exception:
            pass

    if not synced or total == 0:
        print()
        print('!! nothing arrived. The payload never ran, and no run folder')
        print('   was created. Check that both files were opened, the')
        print('   EMF first, and that the cable stayed in.')
        return 1

    # ---- reconstruct
    print()
    print('Reconstructing...')
    how = desc['reconstruct']
    magic = mod['block_magic']
    kind = how.get('kind') if isinstance(how, dict) else how
    if kind == 'fs':
        seq = how.get('seq') if isinstance(how, dict) else desc['seq']
        summary = fsbuild.build(capf, outdir, seq, magic=magic)
    elif kind == 'indexed':
        summary = recon.build(capf, outdir, how, magic=magic)
    else:
        print('!! unknown reconstruct kind %r' % kind)
        return 2

    summary['capture_bytes'] = total
    summary['capture_stopped'] = why

    if kind == 'indexed':
        clean = (why == 'end marker' and summary.get('complete')
                 and not summary.get('bad_blocks'))
    else:
        # For a sweep 'complete' is a COUNT of files, not a flag, and an empty
        # file is a fact about the phone. Only the end marker and the block
        # checksums say anything about the decode.
        clean = why == 'end marker' and not summary.get('bad_blocks')

    if clean:
        try:
            os.remove(capf)
            print('  capture.bin removed - it decoded cleanly')
        except OSError as e:
            print('  could not remove capture.bin: %s' % e)
    else:
        print('  capture.bin KEPT: the run was not clean, error occurred during reconstruction.')

    print()
    print('Done -> %s' % outdir)
    if why != 'end marker':
        print('  NOTE: the stream stopped on %s, not the end marker, so this'
              % why)
        print('        run may be incomplete. capture.bin is kept either way.')
    return 0


def main():
    #   add_help=False so that --help can be answered AFTER --p is known:
    #   argparse's own help action fires during parse_args and exits.
    ap = argparse.ArgumentParser(
        prog='audumper', add_help=False,
        description='Dump AU phones over a standard USB data cable.',
        epilog='--p MODEL --help prints that phone\'s USB identity, the mode '
               'to put it in, where the files land and what each of its '
               'commands does.')
    ap.add_argument('-h', '--help', action='store_true',
                    help='this message, or one phone\'s if --p is given')
    ap.add_argument('-p', '--p', '--phone', dest='model', metavar='MODEL',
                    help='which phone (see --list)')
    ap.add_argument('command', nargs='?', help='e.g. dump_fs, dump_nand')
    ap.add_argument('--list', action='store_true',
                    help='show the installed models and commands')
    ap.add_argument('--out', default='.', metavar='DIR',
                    help='where to create the run folder (default: here)')
    ap.add_argument('--framesize', type=lambda s: int(s, 0), default=None,
                    help="override the model manifest's frame size")
    ap.add_argument('-v', '--verbose', action='store_true',
                    help='log every frame on the wire')
    ap.add_argument('--no-push', dest='push', action='store_false',
                    help='listen only; the files are already on the phone')
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    aulink.VERBOSE = a.verbose
    usbtty.VERBOSE = a.verbose

    avail = commands()
    if a.help:
        if not a.model:
            ap.print_help()
            return 0
        if a.model not in avail:
            print('!! no such model %r. Installed: %s'
                  % (a.model, ', '.join(avail) or 'none'))
            return 2
        show_model(a.model, model_spec(a.model), avail[a.model])
        return 0
    if a.list:
        show_list(avail)
        return 0
    if not a.model or not a.command:
        print('!! a model and a command are required')
        print()
        show_list(avail)
        return 2
    if a.model not in avail:
        print('!! no such model %r. Installed: %s'
              % (a.model, ', '.join(avail) or 'none'))
        return 2
    if a.command not in avail[a.model]:
        print('!! %r has no command %r. It has: %s'
              % (a.model, a.command, ', '.join(avail[a.model])))
        return 2

    desc = avail[a.model][a.command]
    mod = model_spec(a.model)
    stamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    outdir = os.path.abspath(os.path.join(
        a.out, 'audumper_%s_%s_%s' % (stamp, a.model, a.command)))

    print('audumper  %s %s' % (a.model, a.command))
    print('  %s' % desc.get('title', ''))
    print()
    return run(desc, outdir, a.framesize, mod, push=a.push)


if __name__ == '__main__':
    sys.exit(main())
