"""Shared packet format for the optical (QR stream) transfer.

Every QR code carries one binary packet:

    magic  "DQ"   2 bytes
    version       1 byte
    type          1 byte   (0 = metadata, 1 = data chunk)
    session       4 bytes  (identifies one file + chunk size; frames from other sessions are ignored)
    index         uint32   (chunk number, 0-based; 0 for metadata)
    total         uint32   (number of data chunks)
    payload       N bytes
    crc32         uint32   (over everything before it)

The metadata packet holds JSON: file name, size, SHA-256 and chunk size.
"""

import hashlib
import json
import os
import struct
import zlib

MAGIC = b"DQ"
VERSION = 1
T_META = 0
T_DATA = 1
HEADER = struct.Struct(">2sBB4sII")
OVERHEAD = HEADER.size + 4  # header + crc
META_EVERY = 25  # sender repeats the metadata frame every N data frames


def make_packet(ptype: int, session: bytes, index: int, total: int, payload: bytes) -> bytes:
    body = HEADER.pack(MAGIC, VERSION, ptype, session, index, total) + payload
    return body + struct.pack(">I", zlib.crc32(body))


def parse_packet(raw: bytes):
    """Return (type, session, index, total, payload) or None if invalid."""
    if not raw or len(raw) < OVERHEAD:
        return None
    body, crc = raw[:-4], struct.unpack(">I", raw[-4:])[0]
    if zlib.crc32(body) != crc:
        return None
    magic, ver, ptype, session, index, total = HEADER.unpack_from(body)
    if magic != MAGIC or ver != VERSION or ptype not in (T_META, T_DATA):
        return None
    return ptype, session, index, total, body[HEADER.size:]


class FileEncoder:
    """Splits a file into packets."""

    def __init__(self, path: str, chunk_size: int):
        with open(path, "rb") as f:
            self.data = f.read()
        self.name = os.path.basename(path)
        self.chunk_size = chunk_size
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.session = hashlib.sha256(self.data + chunk_size.to_bytes(4, "big")).digest()[:4]
        self.total = max(1, -(-len(self.data) // chunk_size))
        meta = {"name": self.name, "size": len(self.data), "sha256": self.sha256, "chunk": chunk_size}
        self._meta = make_packet(T_META, self.session, 0, self.total,
                                 json.dumps(meta, separators=(",", ":")).encode())

    def meta_packet(self) -> bytes:
        return self._meta

    def data_packet(self, i: int) -> bytes:
        chunk = self.data[i * self.chunk_size:(i + 1) * self.chunk_size]
        return make_packet(T_DATA, self.session, i, self.total, chunk)

    def sequence(self, only=None):
        """List of ('meta', 0) / ('data', i) frames for one pass."""
        indices = sorted(i for i in only if 0 <= i < self.total) if only else range(self.total)
        seq = []
        for n, i in enumerate(indices):
            if n % META_EVERY == 0:
                seq.append(("meta", 0))
            seq.append(("data", i))
        return seq or [("meta", 0)]


class Assembler:
    """Collects packets on the receiver side."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.session = None
        self.total = None
        self.meta = None
        self.chunks = {}

    def add(self, raw: bytes) -> str:
        """Returns 'new', 'dup', 'meta', 'foreign' or 'bad'."""
        p = parse_packet(raw)
        if p is None:
            return "bad"
        ptype, session, index, total, payload = p
        if self.session is None:
            self.session, self.total = session, total
        elif session != self.session:
            return "foreign"
        if ptype == T_META:
            if self.meta is None:
                try:
                    self.meta = json.loads(payload.decode())
                except ValueError:
                    return "bad"
            return "meta"
        if index >= self.total:
            return "bad"
        if index in self.chunks:
            return "dup"
        self.chunks[index] = payload
        return "new"

    def complete(self) -> bool:
        return self.meta is not None and self.total is not None and len(self.chunks) == self.total

    def missing(self):
        if self.total is None:
            return []
        return [i for i in range(self.total) if i not in self.chunks]

    def assemble(self):
        """Returns (data, computed_sha256, ok)."""
        data = b"".join(self.chunks[i] for i in range(self.total))
        sha = hashlib.sha256(data).hexdigest()
        ok = sha == self.meta.get("sha256") and len(data) == self.meta.get("size")
        return data, sha, ok


def format_ranges(indices) -> str:
    """[1,2,3,7,9,10] -> '1-3, 7, 9-10'"""
    out = []
    indices = sorted(indices)
    i = 0
    while i < len(indices):
        j = i
        while j + 1 < len(indices) and indices[j + 1] == indices[j] + 1:
            j += 1
        out.append(str(indices[i]) if i == j else f"{indices[i]}-{indices[j]}")
        i = j + 1
    return ", ".join(out)


def parse_ranges(text: str):
    """'1-3, 7' -> {1,2,3,7}. Raises ValueError on bad input."""
    result = set()
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            result.update(range(int(a), int(b) + 1))
        else:
            result.add(int(part))
    return result


def short_hash(sha: str) -> str:
    """Easy-to-compare-by-eye form: first 8 and last 8 hex chars."""
    return f"{sha[:8].upper()} … {sha[-8:].upper()}"
