import pytest

from warescue import cli
from conftest import ROOT_KEY, build_backup
from test_merge import EXPORT, db_path  # noqa: F401


@pytest.fixture
def files(db_path, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(cli.KEY_ENV, ROOT_KEY.hex())
    (tmp_path / "msgstore.crypt15").write_bytes(build_backup(db_path.read_bytes()))
    (tmp_path / "chat.txt").write_text(EXPORT, encoding="utf-8")
    return tmp_path


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_decrypt(files, capsys):
    code, out, _ = run(capsys, "decrypt", "msgstore.crypt15", "dec.db")
    assert code == 0
    assert out.splitlines()[1:] == [
        "tables         : 5",
        "  message      : 4 rows",
        "  chat         : 1 rows",
        "  jid          : 2 rows",
    ]
    code, _, err = run(capsys, "decrypt", "msgstore.crypt15", "dec.db")
    assert code == 1 and err == "dec.db exists; use --force to overwrite\n"


def test_wrong_key(files, capsys, monkeypatch):
    monkeypatch.setenv(cli.KEY_ENV, "00" * 32)
    code, _, err = run(capsys, "decrypt", "msgstore.crypt15", "dec.db")
    assert code == 2 and err.startswith("error: authentication failed")
    assert not (files / "dec.db").exists()


def test_merge_dry_run_output(files, capsys):
    run(capsys, "decrypt", "msgstore.crypt15", "dec.db")
    code, out, _ = run(capsys, "merge", "dec.db", "chat.txt", "--phone", "5550001122",
                       "--me", "Ben", "--dry-run")
    assert code == 0
    assert out.splitlines() == [
        "chat           : #1 (jid #5)",
        "gaps filled    : 1  (Europe/Istanbul)",
        "  2024-10-07 22:52 -> 2026-10-02 16:24         6 msgs",
        "to insert      : 6  (you: 3, them: 3)",
        "  by kind      : text=4, media_omitted=1, deleted=1",
        "skipped        : already_in_db=1, duplicate_at_edge=1",
        "dry run: nothing written",
    ]


def test_merge_encrypt_repair(files, capsys):
    run(capsys, "decrypt", "msgstore.crypt15", "dec.db")
    code, out, _ = run(capsys, "merge", "dec.db", "chat.txt", "--phone", "5550001122",
                       "--me", "Ben", "--out", "merged.db")
    assert code == 0 and out.endswith("written        : merged.db  (sqlite quick_check ok)\n")
    code, _, err = run(capsys, "merge", "dec.db", "chat.txt", "--phone", "5550001122",
                       "--me", "Ben", "--out", "merged.db")
    assert code == 2 and err == "error: merged.db exists; refusing to overwrite\n"

    code, out, _ = run(capsys, "encrypt", "merged.db", "msgstore.crypt15", "out/new.crypt15")
    assert code == 0 and out.startswith("encrypted -> out/new.crypt15  [")
    code, _, err = run(capsys, "encrypt", "chat.txt", "msgstore.crypt15", "x.crypt15")
    assert code == 1 and err == "chat.txt is not a SQLite database\n"

    code, out, _ = run(capsys, "repair", "merged.db", "--baseline", "dec.db", "--dry-run")
    assert code == 0
    assert "  chat #1             6 message(s) moved to the end" in out.splitlines()


def test_inspect_unparsable_file(files, capsys):
    (files / "junk.bin").write_bytes(b"\xff" + b"x" * 40)
    code, out, err = run(capsys, "inspect", "junk.bin")
    assert code == 2 and out.startswith("file size      : 41 bytes\n")
    assert err == "error: header length byte points past end of file\n"


def test_parse_warns_on_unknown_me(files, capsys):
    code, out, _ = run(capsys, "parse", "chat.txt", "--me", "Nobody")
    assert code == 1 and "matches no sender" in out.splitlines()[-1]
