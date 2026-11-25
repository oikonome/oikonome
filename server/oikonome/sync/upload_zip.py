"""The one door an uploaded ZIP is opened through.

`zipfile.ZipFile()` reads the whole central directory and builds an object
per record before any caller can look at what it holds. A hand-made archive
of tens of megabytes can list millions of members, and parsing that costs
gigabytes in the process that serves every household — before a size rule,
a role check or a step-up has run. So the bound is read here, from the end
of the bytes themselves, before ZipFile is ever constructed: the
end-of-central-directory record (and its zip64 locator and record) say how
many members the archive lists and how large its central directory is, and
an archive beyond what an export can hold is refused unopened.

The record is located exactly as `zipfile` locates it (the last 22 bytes,
else the last signature in the trailing comment window), so the
central-directory size checked here is the size `zipfile` will then read.
Anything this reader cannot settle — no record, several disks, a zip64
record that is not where its locator says, a directory that does not fit
the file — is refused rather than handed to `zipfile` to guess at.

Every reader of uploaded ZIP bytes calls `open_uploaded`; a source-walking
test fails on a `ZipFile(` opened for reading anywhere else.
"""

from __future__ import annotations

import io
import mmap
import struct
import zipfile

# The most members an uploaded archive may list. The household export
# writes one CSV per table plus its README, manifest and restore note —
# a couple of hundred at most — and a re-zipped copy (its folder, a Mac's
# __MACOSX twins, directory entries) at most doubles that. This is many
# times either.
MAX_ARCHIVE_MEMBERS = 5_000

# The largest central directory an uploaded archive may carry. An export's
# is tens of kilobytes (a record is 46 bytes plus its name and extra
# field). Even a directory that understates its member count cannot hold
# more than this / 46 records, so this bounds the parse on its own.
MAX_CENTRAL_DIRECTORY_BYTES = 2 * 1024 * 1024

_EOCD = struct.Struct("<4s4H2LH")          # 22 bytes
_EOCD_SIG = b"PK\x05\x06"
_LOC64 = struct.Struct("<4sLQL")           # 20 bytes
_LOC64_SIG = b"PK\x06\x07"
_EOCD64 = struct.Struct("<4sQ2H2L4Q")      # 56 bytes
_EOCD64_SIG = b"PK\x06\x06"
_MAX_COMMENT = (1 << 16) - 1

UNREADABLE = ("this file isn't a readable ZIP archive — download the "
              "export again and upload it as it came")
TOO_MANY = ("this ZIP lists {n} files — an Oikonome export holds a few "
            "hundred, so it isn't one (the limit is {cap})")
TOO_LARGE_DIRECTORY = ("this ZIP's file list is {mb} MB — far larger than "
                       "an Oikonome export's, so it isn't one")


class ArchiveRefused(ValueError):
    """An uploaded ZIP refused before it was opened. A ValueError, so every
    door that reports a refusal's sentence reports this one."""


class _Tail:
    """Length and slices of the upload, whatever form it arrived in, without
    copying the whole of it: bytes, a memory map, or a seekable file."""

    def __init__(self, data):
        self._data = data
        if isinstance(data, (bytes, bytearray, memoryview, mmap.mmap)):
            self.size = len(data)
            self._file = None
        else:
            self._file = data
            pos = data.tell()
            self.size = data.seek(0, io.SEEK_END)
            data.seek(pos)

    def read(self, start: int, n: int) -> bytes:
        if start < 0 or n <= 0 or start >= self.size:
            return b""
        if self._file is None:
            return bytes(self._data[start:start + n])
        pos = self._file.tell()
        try:
            self._file.seek(start)
            return self._file.read(n)
        finally:
            self._file.seek(pos)


def _end_record(tail: _Tail):
    """(entries this disk, entries total, directory size, directory offset,
    where the record sits, disk number, directory disk), read where
    `zipfile` reads it; None when there is none."""
    last = tail.read(tail.size - _EOCD.size, _EOCD.size)
    if (len(last) == _EOCD.size and last[:4] == _EOCD_SIG
            and last[-2:] == b"\x00\x00"):
        pos, rec = tail.size - _EOCD.size, last
    else:
        lo = max(tail.size - _MAX_COMMENT - _EOCD.size, 0)
        window = tail.read(lo, _MAX_COMMENT + _EOCD.size)
        at = window.rfind(_EOCD_SIG)
        if at < 0:
            return None
        rec = window[at:at + _EOCD.size]
        if len(rec) != _EOCD.size:
            return None
        pos = lo + at
    (_sig, disk, cd_disk, here, total, cd_size, cd_offset,
     _comment) = _EOCD.unpack(rec)
    return [here, total, cd_size, cd_offset, pos, disk, cd_disk]


def central_directory_problem(data) -> str | None:
    """Why an uploaded ZIP is refused before it is opened, or None."""
    try:
        tail = _Tail(data)
    except (OSError, ValueError, TypeError):
        return UNREADABLE
    if tail.size < _EOCD.size:
        return UNREADABLE
    end = _end_record(tail)
    if end is None:
        return UNREADABLE
    here, total, cd_size, cd_offset, pos, disk, cd_disk = end
    # the zip64 locator sits immediately before the classic record when
    # there is one; `zipfile` then takes every figure from the zip64 record
    loc = tail.read(pos - _LOC64.size, _LOC64.size)
    if len(loc) == _LOC64.size and loc[:4] == _LOC64_SIG:
        _sig, loc_disk, rec_at, disks = _LOC64.unpack(loc)
        if loc_disk != 0 or disks > 1:
            return UNREADABLE
        expect = pos - _LOC64.size - _EOCD64.size
        # only the layout every writer produces: the record directly before
        # its locator, no extensible data, nothing prepended
        if expect < 0 or rec_at != expect:
            return UNREADABLE
        rec = tail.read(expect, _EOCD64.size)
        if len(rec) != _EOCD64.size or rec[:4] != _EOCD64_SIG:
            return UNREADABLE
        (_sig, rec_size, _made, _need, disk, cd_disk, here, total,
         cd_size, cd_offset) = _EOCD64.unpack(rec)
        if rec_size + 12 != _EOCD64.size:
            return UNREADABLE
        pos = expect
    if disk != 0 or cd_disk != 0 or here != total:
        return UNREADABLE
    if total > MAX_ARCHIVE_MEMBERS:
        return TOO_MANY.format(n=f"{total:,}", cap=f"{MAX_ARCHIVE_MEMBERS:,}")
    if cd_size > MAX_CENTRAL_DIRECTORY_BYTES:
        return TOO_LARGE_DIRECTORY.format(mb=max(cd_size // (1024 * 1024), 1))
    # `zipfile` reads the directory from just before the record; it has to
    # fit there, and the offset it declares has to fit before that
    start = pos - cd_size
    if start < 0 or cd_offset > start:
        return UNREADABLE
    return None


def check(data) -> None:
    """Raise ArchiveRefused when the upload is beyond what an export holds."""
    problem = central_directory_problem(data)
    if problem:
        raise ArchiveRefused(problem)


def open_uploaded(data) -> zipfile.ZipFile:
    """The uploaded archive opened for reading, after `check`. Bytes and
    memory maps are read in place; a seekable file is handed over as is."""
    check(data)
    src = data if (hasattr(data, "seek")
                   and not isinstance(data, (bytes, bytearray))) \
        else io.BytesIO(data)
    return zipfile.ZipFile(src)
