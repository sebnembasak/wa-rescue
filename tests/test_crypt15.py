import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from warescue import crypt15
from conftest import ROOT_KEY, build_backup


def test_handwritten_hkdf_matches_library():
    reference = HKDF(hashes.SHA256(), 32, salt=None, info=b"backup encryption").derive(ROOT_KEY)
    assert crypt15.derive_backup_key(ROOT_KEY) == reference


def test_parse_key_with_spaces():
    shown = " ".join(ROOT_KEY.hex()[i:i + 4] for i in range(0, 64, 4))
    assert crypt15.parse_root_key(shown) == ROOT_KEY


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize("md5", [True, False])
def test_decrypt_layout_variants(sqlite_bytes, flag, md5):
    db, layout, _ = crypt15.decrypt(build_backup(sqlite_bytes, feature_flag=flag, md5=md5), ROOT_KEY)
    assert db == sqlite_bytes
    assert layout.has_feature_flag is flag


def test_wrong_key_fails(sqlite_bytes):
    with pytest.raises(crypt15.Crypt15Error):
        crypt15.decrypt(build_backup(sqlite_bytes), bytes(32))


def test_encrypt_roundtrip_uses_fresh_iv(sqlite_bytes):
    template = build_backup(sqlite_bytes)
    rebuilt = crypt15.encrypt(sqlite_bytes + b"", ROOT_KEY, template)
    assert crypt15.has_md5_trailer(rebuilt)
    assert crypt15.parse_layout(rebuilt).iv != crypt15.parse_layout(template).iv
    assert crypt15.decrypt(rebuilt, ROOT_KEY)[0] == sqlite_bytes
