#!/usr/bin/env python3
r"""Rebuild files from a framed capture. Spec comes from the command manifest.

Each output names the record stream that feeds it (`seq`), the unit width in
that file (`stride`), and the slice of the record body to take (`take`). Two
outputs may share a seq and differ only in their slice, which is how a payload
that packs data and spare into one record is handled; bytes outside every
slice are dropped.

`coverage` marks the outputs whose completeness decides the gap list and the
"complete" verdict. With one marked output that is simply its own coverage --
the long-standing behaviour. Mark SEVERAL and a unit counts as covered only
when EVERY marked output has it, which is what a payload dumping more than one
chip needs: the w53ca sends four streams (two chips, data and spare), and with
a single marked output a run that lost every page of the second chip would
still report complete, and audumper would delete the capture. The unit counts
`added`, `identical` and `differed` continue to describe the FIRST marked
output, so a manifest marking one output behaves exactly as before.

Optional: `fail_seq` names the record a payload sends instead of a unit it
could not read; `skip_record` matches a sentinel left in the buffer instead;
`expect` asserts known bytes at a known unit. See the installed
phones/*/*/command.json for worked examples.
"""
import os

from . import blocks


def _slice(body, take):
    if not take:
        return body
    #   A LIST OF [a, b] PAIRS gathers several ranges in order, for a
    #   controller that INTERLEAVES data and spare inside one codeword.
    #   SH002's raw read returns 4 x (464 data, 2 spare, 48 data, 10 ECC,
    #   4 pad), so neither `nand.bin` nor `oob.bin` is one contiguous run of
    #   the record and eight ranges per output is the only way to say so.
    #
    #   This is a new MANIFEST VOCABULARY case, not a change to an existing
    #   one: `take[0]` is an int for every `[a, b]` any shipped manifest
    #   carries, so ca003, w47t and w53ca fall straight through to the two
    #   lines below, unaltered. `tools/recon_slice_proof.py` checks that
    #   exhaustively against every `take` installed under `phones/`.
    if isinstance(take[0], (list, tuple)):
        return b''.join(body[a:b if b is not None else len(body)]
                        for a, b in take)
    a, b = take
    return body[a:b if b is not None else len(body)]


def build(capture_path, outdir, spec, magic, progress=True):
    import mmap

    units = int(spec['units'])
    outs = spec['outputs']
    fail_seq = spec.get('fail_seq')
    skip = spec.get('skip_record') or None
    expect = spec.get('expect') or None
    if skip:
        skip = dict(skip, head=bytes.fromhex(skip['head']),
                    tail=bytes.fromhex(skip['tail']))
    if expect:
        expect = dict(expect, bytes=bytes.fromhex(expect['bytes']))

    # one sink per output, and the streams each seq feeds
    sinks = []
    for o in outs:
        path = os.path.join(outdir, o['file'])
        with open(path, 'wb') as fh:
            fh.truncate(units * int(o['stride']))   # sparse; holes stay holes
        sinks.append({'o': o, 'path': path, 'fh': open(path, 'r+b'),
                      'cov': bytearray(units), 'seq': int(o['seq']),
                      'stride': int(o['stride']), 'take': o.get('take'),
                      'coverage': bool(o.get('coverage'))})
    by_seq = {}
    for s in sinks:
        by_seq.setdefault(s['seq'], []).append(s)

    # The outputs whose completeness decides the verdict, and the first of them,
    # which the unit counts describe. With one marked output -- every manifest
    # written before the w53ca -- `primary` IS that output and nothing below
    # behaves differently.
    covers = [s for s in sinks if s['coverage']] or [sinks[0]]
    primary = covers[0]

    added = redone = differ = bad = fill = 0
    fails = []
    expect_ok = None

    cf = open(capture_path, 'rb')
    buf = mmap.mmap(cf.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        for seq, idx, ok, body in blocks.parse(buf, magic):
            if fail_seq is not None and seq == fail_seq:
                fails.append((idx, int.from_bytes(body[:4], 'little')))
                continue
            targets = by_seq.get(seq)
            if not targets:
                continue
            if not ok or idx >= units:
                bad += 1
                continue
            if skip and seq == skip['seq'] and len(body) >= 4 \
                    and body[:len(skip['head'])] == skip['head'] \
                    and body[-len(skip['tail']):] == skip['tail']:
                fill += 1
                continue
            if expect and seq == expect['seq'] and idx == expect['index'] \
                    and expect_ok is None:
                a = expect.get('at', 0)
                expect_ok = body[a:a + len(expect['bytes'])] == expect['bytes']
            for s in targets:
                part = _slice(body, s['take'])
                if len(part) != s['stride']:
                    bad += 1
                    continue
                if s['cov'][idx]:
                    if s is primary:
                        s['fh'].seek(idx * s['stride'])
                        if s['fh'].read(s['stride']) == part:
                            redone += 1
                        else:
                            differ += 1
                    continue
                s['fh'].seek(idx * s['stride'])
                s['fh'].write(part)
                s['cov'][idx] = 1
                if s is primary:
                    added += 1
    finally:
        for s in sinks:
            s['fh'].close()
        buf.close()
        cf.close()

    # A unit is covered only when EVERY marked output has it. With one marked
    # output this is that output's own bitmap, unchanged and not even copied.
    cov = primary['cov']
    if len(covers) > 1:
        cov = bytearray(units)
        for i in range(units):
            for c in covers:
                if not c['cov'][i]:
                    break
            else:
                cov[i] = 1
    gaps = []
    i = 0
    while i < units:
        if cov[i]:
            i += 1
            continue
        j = i
        while j < units and not cov[j]:
            j += 1
        gaps.append((i, j - 1, j - i))
        i = j

    if progress:
        for first, last, n in gaps[:20]:
            print('  gap: pages %d..%d (%d)' % (first, last, n))
        if len(gaps) > 20:
            print('  ... %d more gap(s)' % (len(gaps) - 20))
        if fails and fails[-1][1]:
            print('  the payload reported %d failed read(s)' % fails[-1][1])
        for s in sinks:
            have = sum(s['cov'])
            print('  %-10s %d / %d units (%.2f%%, %.0f MiB)'
                  % (s['o']['file'], have, units, 100.0 * have / units,
                     have * s['stride'] / 1048576.0))
        if expect_ok is not None:
            print('  %s -- %s' % (expect.get('label', 'expected bytes'),
                                  'YES' if expect_ok else 'NO'))
        print('  %d units written, %d re-sent identically, %d DIFFERED,'
              ' %d bad, %d failed reads' % (added, redone, differ, bad, fill))
        if differ:
            print('  a differing unit means the flash changed between reads.')
            print('  expected inside a live filesystem, not the firmware area.')
        if not gaps:
            print('  full dump completed!')
        else:
            print('  %d gap(s); the largest is %d units at %d'
                  % (len(gaps), max(g[2] for g in gaps),
                     max(gaps, key=lambda g: g[2])[0]))

    out = {'pages': sum(cov), 'total_pages': units,
           'added': added, 'identical': redone, 'differed': differ,
           'bad_blocks': bad, 'failed_reads': fill, 'gaps': len(gaps),
           'complete': not gaps}
    for s in sinks:
        if not s['coverage']:
            out.setdefault('oob_pages', sum(s['cov']))
    if expect_ok is not None:
        out['expect_ok'] = expect_ok
    return out
