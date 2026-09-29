#!/usr/bin/env python3
r"""Rebuild a filesystem from an offset-addressed capture.

phone_fs/ holds only the handset's own files and directories; everything
about the run goes in sibling files.

    manifest.tsv   path, type, mode, size, got, state, ondisk
    holes.tsv      missing byte ranges
    missing.tsv    files whose content never arrived
    tree.txt       the tree as the handset reported it

File size comes from the fstat record, not the readdir entry: readdir reports
one size for every entry in a directory.

A file whose content never arrived is not written. an empty placeholder
would read as real data, so these files are listed in missing.tsv. A genuinely empty
file is created.

Case collisions: on a case-insensitive host the first spelling seen keeps the
name and later ones get a ~caseN suffix, recorded in manifest.tsv.
"""
import os

from . import blocks

S_IFMT, S_IFDIR, S_IFREG, S_IFLNK = 0xF000, 0x4000, 0x8000, 0xA000


def _rec(body):
    import struct
    depth, mode, size = struct.unpack_from('<III', body, 0)
    nul = body.find(b'\x00', 12)
    end = nul if nul >= 0 else len(body)
    return depth, mode, size, body[12:end].decode('latin1')


def _kind(mode):
    return {S_IFDIR: 'dir', S_IFREG: 'file', S_IFLNK: 'link'}.get(
        mode & S_IFMT, 'other')


class _Namer(object):
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.host = {'': self.root}
        self.taken = {}

    def path(self, phone):
        parent, _, name = phone.rpartition('/')
        hp = self.host.get(parent)
        if hp is None:
            hp = self.path(parent) if parent else self.root
            self.host[parent] = hp
        key = (hp, name.lower())
        if key not in self.taken:
            clash = sum(1 for (p, _n) in self.taken
                        if p == hp and self.taken[(p, _n)].lower() == name.lower())
            self.taken[key] = name if not clash else '%s~case%d' % (name, clash + 1)
        out = os.path.join(hp, self.taken[key])
        self.host[phone] = out
        return out


def build(capture_path, outdir, seq, magic):
    """Returns a summary dict. Writes phone_fs/ and the sibling reports."""
    import mmap
    S = {k: int(v) for k, v in seq.items()}
    entries, fsize, chunks = [], {}, {}
    cur = None
    nblk = nbad = 0

    cf = open(capture_path, 'rb')
    buf = mmap.mmap(cf.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        for s, tag, ok, body in blocks.parse(buf, magic):
            nblk += 1
            if not ok:
                nbad += 1
                continue
            if s == S['entry']:
                entries.append(_rec(body))
            elif s == S['file']:
                _d, _m, sz, cur = _rec(body)
                fsize[cur] = sz                  # efs_fstat's size
                chunks.setdefault(cur, {})
            elif s == S['data'] and cur is not None:
                chunks[cur][tag] = body          # tag IS the offset in the file
    finally:
        buf.close()
        cf.close()

    root = os.path.join(outdir, 'phone_fs')
    os.makedirs(root, exist_ok=True)
    namer = _Namer(root)

    for _dep, mode, _sz, path in entries:
        if _kind(mode) == 'dir':
            os.makedirs(namer.path(path), exist_ok=True)

    rows, holes, missing = [], [], []
    nfile = ncomp = npart = 0
    got_total = size_total = 0
    for dep, mode, rdsize, path in entries:
        k = _kind(mode)
        if k != 'file':
            hp = namer.path(path)
            alias = os.path.relpath(hp, root).replace(os.sep, '/')
            rows.append((path, k, mode, rdsize, 0, 'n/a',
                         '' if alias == path.lstrip('/') else alias))
            continue
        nfile += 1
        size = fsize.get(path, rdsize)
        size_total += size
        data = bytearray(size)
        have = bytearray(size)
        for off, body in sorted(chunks.get(path, {}).items()):
            if off >= size:
                continue
            end = min(off + len(body), size)
            data[off:end] = body[:end - off]
            for i in range(off, end):
                have[i] = 1
        got = sum(have)
        got_total += got

        i = 0
        while i < size:
            if have[i]:
                i += 1
                continue
            j = i
            while j < size and not have[j]:
                j += 1
            holes.append((path, i, j - i))
            i = j

        hp = namer.path(path)
        if got or size == 0:
            os.makedirs(os.path.dirname(hp), exist_ok=True)
            with open(hp, 'wb') as fh:
                fh.write(bytes(data))
            if got == size:
                state = 'complete'
                ncomp += 1
            else:
                state = 'PARTIAL'
                npart += 1
        else:
            state = 'EMPTY'
            missing.append((path, size))
        alias = os.path.relpath(hp, root).replace(os.sep, '/')
        rows.append((path, k, mode, size, got, state,
                     '' if alias == path.lstrip('/') else alias))

    def write(name, header, lines):
        with open(os.path.join(outdir, name), 'w', encoding='utf-8') as fh:
            fh.write(header + '\n')
            for t in lines:
                fh.write(t + '\n')

    write('manifest.tsv', 'path\ttype\tmode\tsize\tgot\tstate\tondisk',
          ['%s\t%s\t0x%04X\t%d\t%d\t%s\t%s' % r for r in rows])
    write('holes.tsv', 'path\toffset\tlength', ['%s\t%d\t%d' % h for h in holes])
    write('missing.tsv', 'path\tsize', ['%s\t%d' % m for m in missing])
    write('tree.txt', '# the tree as the handset reported it',
          ['%-5s %2d %10d  %s%s' % (_kind(m), dep, fsize.get(p, sz),
                                    '  ' * dep, p)
           for dep, m, sz, p in entries])

    ndir = sum(1 for r in rows if r[1] == 'dir')
    renamed = [(r[0], r[6]) for r in rows if r[6]]
    print('  %d entries: %d dir, %d file, %d other'
          % (len(rows), ndir, nfile, len(rows) - ndir - nfile))
    print('  %d of %d files complete (%.1f%%), %d partial, %d empty'
          % (ncomp, nfile, 100.0 * ncomp / max(1, nfile), npart,
             len(missing)))
    print('  %d of %d bytes (%.2f%%)'
          % (got_total, size_total,
             100.0 * got_total / max(1, size_total)))
    if nbad:
        print('  !! %d block(s) failed their checksum and were discarded'
              % nbad)
    for a, b in renamed[:8]:
        print('  renamed for this host (case sensitivity issue): %s -> %s' % (a, b))
    return {'entries': len(rows), 'dirs': ndir, 'files': nfile,
            'complete': ncomp, 'partial': npart, 'empty': len(missing),
            'bytes_got': got_total, 'bytes_expected': size_total,
            'holes': len(holes), 'bad_blocks': nbad,
            'renamed': renamed}
