import hashlib
import os
import sqlite3
import zlib

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from warescue import crypt15
from warescue.protowire import encode_field

ROOT_KEY = bytes(range(32))


def build_backup(database: bytes, *, feature_flag=True, md5=True, key=ROOT_KEY) -> bytes:
    """Synthetic crypt15 file. Never use real backups in tests."""
    iv = os.urandom(16)
    header = (
        encode_field(1, 1)
        + encode_field(3, encode_field(1, iv))
        + encode_field(4, encode_field(1, b"2.24.0.0"))
    )
    prefix = bytes([len(header)]) + (b"\x01" if feature_flag else b"") + header
    sealed = AESGCM(crypt15.derive_backup_key(key)).encrypt(iv, zlib.compress(database), None)
    out = prefix + sealed
    return out + hashlib.md5(out).digest() if md5 else out


@pytest.fixture
def sqlite_bytes(tmp_path):
    path = tmp_path / "fake.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE message (_id INTEGER PRIMARY KEY, text_data TEXT)")
        db.executemany("INSERT INTO message (text_data) VALUES (?)", [("merhaba",), ("deneme",)])
    return path.read_bytes()
