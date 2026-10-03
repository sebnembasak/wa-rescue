"""crypt15 (end-to-end encrypted WhatsApp backup) container format.

Layout, as reconstructed from inspecting real files:

    [1 byte ] header length N
    [1 byte ] optional 0x01 "feature table present" flag
    [N bytes] protobuf header (field 3.1 holds the 16-byte AES-GCM IV)
    [ ...   ] AES-256-GCM ciphertext of zlib(SQLite database)
    [16     ] GCM authentication tag
    [16     ] optional MD5 of everything before it

The AES key is not the 64-hex-digit key the user sees. It is derived from it
with HKDF-SHA256 (zero salt, info = b"backup encryption", 32 bytes).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import zlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import protowire

SQLITE_MAGIC = b"SQLite format 3\x00"
TAG_LEN = 16
MD5_LEN = 16
FEATURE_FLAG = 0x01
IV_PATH = (3, 1)


class Crypt15Error(Exception):
    pass


class WrongKeyError(Crypt15Error):
    """The GCM tag did not verify: almost always a key from another backup."""


def parse_root_key(text: str) -> bytes:
    """Accept the key as WhatsApp displays it: 64 hex digits, any spacing."""
    cleaned = "".join(text.split()).replace("-", "").lower()
    if len(cleaned) != 64:
        raise Crypt15Error(f"key must be 64 hex digits, got {len(cleaned)}")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise Crypt15Error("key contains non-hex characters") from exc


def derive_backup_key(root_key: bytes, info: bytes = b"backup encryption") -> bytes:
    """HKDF-SHA256 per RFC 5869, written out by hand.

    32 output bytes = exactly one expand block, so expand collapses to a
    single HMAC over (info || 0x01).
    """
    if len(root_key) != 32:
        raise Crypt15Error("root key must be 32 bytes")
    prk = hmac.new(b"\x00" * 32, root_key, hashlib.sha256).digest()      # extract
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()        # expand


@dataclass
class Layout:
    header_size: int
    has_feature_flag: bool
    header: bytes
    fields: list[protowire.Field]
    iv: bytes
    payload_offset: int

    @property
    def prefix_len(self) -> int:
        return self.payload_offset


def parse_layout(data: bytes) -> Layout:
    if len(data) < 2 + TAG_LEN:
        raise Crypt15Error("file too small to be a crypt15 backup")

    header_size = data[0]
    has_flag = data[1] == FEATURE_FLAG
    start = 2 if has_flag else 1
    header = data[start:start + header_size]
    if len(header) != header_size:
        raise Crypt15Error("header length byte points past end of file")

    try:
        fields = protowire.decode(header)
    except protowire.ProtoError as exc:
        raise Crypt15Error(f"header is not valid protobuf ({exc}); run `inspect`") from exc

    iv_field = protowire.find(fields, *IV_PATH)
    if iv_field is None or not isinstance(iv_field.value, bytes):
        raise Crypt15Error("no IV at header field 3.1; run `inspect` and check the dump")
    if len(iv_field.value) not in (12, 16):
        raise Crypt15Error(f"unexpected IV length {len(iv_field.value)}")

    return Layout(header_size, has_flag, header, fields, iv_field.value, start + header_size)


def has_md5_trailer(data: bytes) -> bool:
    return hashlib.md5(data[:-MD5_LEN]).digest() == data[-MD5_LEN:]


def _candidate_bodies(data: bytes, layout: Layout) -> list[tuple[bytes, str]]:
    body = data[layout.payload_offset:]
    with_md5 = (body[:-MD5_LEN], "ciphertext + tag + md5")
    without = (body, "ciphertext + tag")
    if has_md5_trailer(data):
        return [with_md5]
    # Checksum didn't verify; try both shapes and let GCM decide.
    return [with_md5, without]


def decrypt(data: bytes, root_key: bytes) -> tuple[bytes, Layout, str]:
    layout = parse_layout(data)
    aes = AESGCM(derive_backup_key(root_key))

    for blob, label in _candidate_bodies(data, layout):
        try:
            plain = aes.decrypt(layout.iv, blob, None)  # verifies the tag
        except InvalidTag:
            continue
        return _decompress(plain), layout, label

    raise WrongKeyError(
        "authentication failed: wrong key, or the trailer layout differs "
        "from what we expect (run `inspect`)"
    )


def _decompress(plain: bytes) -> bytes:
    if plain.startswith(SQLITE_MAGIC):
        return plain
    try:
        return zlib.decompress(plain)
    except zlib.error as exc:
        raise Crypt15Error(f"decrypted OK but zlib failed: {exc}") from exc


def encrypt(database: bytes, root_key: bytes, template: bytes) -> bytes:
    """Re-encrypt `database` reusing the header of an existing backup.

    A fresh IV is generated and patched into the header in place (same
    length, so the header size byte stays valid). GCM must never reuse an IV
    with the same key.
    """
    layout = parse_layout(template)
    if layout.header.count(layout.iv) != 1:
        raise Crypt15Error("cannot locate IV uniquely inside header")

    new_iv = os.urandom(len(layout.iv))
    prefix = bytearray(template[:layout.payload_offset])
    iv_at = (layout.payload_offset - layout.header_size) + layout.header.index(layout.iv)
    prefix[iv_at:iv_at + len(new_iv)] = new_iv

    sealed = AESGCM(derive_backup_key(root_key)).encrypt(
        new_iv, zlib.compress(database), None
    )
    out = bytes(prefix) + sealed
    if has_md5_trailer(template):
        out += hashlib.md5(out).digest()
    return out
