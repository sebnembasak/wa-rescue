import sqlite3
from datetime import date

import pytest

from warescue import chatexport, crypt15, merge, repair, service
from conftest import ROOT_KEY, build_backup
from test_merge import EXPORT, add, db_path, ms  # noqa: F401

SECOND_EXPORT = """1.3.2023 10:00 - Ben: ilk
5.6.2025 18:30 - Kişi 2: geri döndüm
5.6.2025 18:31 - Ben: hoş geldin
"""


def request(export=EXPORT, phone="5550001122", **kw):
    msgs = chatexport.parse_lines(export.splitlines(keepends=True))
    return service.MergeRequest(msgs, phone, "Ben", **kw)


@pytest.fixture
def two_chats(db_path):
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO jid (_id, user, server, type) VALUES (7, '905550003344', 's.whatsapp.net', 0)")
        db.execute("INSERT INTO chat VALUES (3, 7, 300, 300, 300, 0, 300, 300, 300)")
        add(db, 300, 3, 1, ms(2023, 3, 1, 10, 0), "ilk")
    return db_path


@pytest.fixture
def backup(db_path, tmp_path):
    path = tmp_path / "msgstore.db.crypt15"
    path.write_bytes(build_backup(db_path.read_bytes()))
    return path


def texts(path, chat):
    with sqlite3.connect(path) as db:
        return [t for (t,) in db.execute(
            "SELECT coalesce(text_data, '<system>') FROM message WHERE chat_row_id = ? ORDER BY sort_id",
            (chat,))]


def test_inspect_backup(backup):
    info = service.inspect_backup(backup)
    assert info.size == backup.stat().st_size
    assert info.head == backup.read_bytes()[:service.HEAD_BYTES]
    assert info.md5_verified and info.layout_error is None
    assert info.app_version == "2.24.0.0"


def test_inspect_reports_unparsable_layout(tmp_path):
    junk = tmp_path / "junk.crypt15"
    junk.write_bytes(b"\xff" + b"x" * 40)
    info = service.inspect_backup(junk)
    assert info.layout is None and info.app_version is None
    assert "past end of file" in info.layout_error


def test_decrypt_backup_summarizes_database(backup, tmp_path):
    out = tmp_path / "dir with #hash" / "msgstore.db"
    out.parent.mkdir()
    result = service.decrypt_backup(backup, out, ROOT_KEY)
    assert result.output == out and result.size == out.stat().st_size
    assert result.is_sqlite
    assert result.summary.row_counts == {"message": 4, "chat": 1, "jid": 2}
    assert "sqlite_sequence" in result.summary.tables


def test_decrypt_backup_refuses_existing_output(backup, tmp_path):
    out = tmp_path / "decrypted.db"
    out.write_text("keep me")
    with pytest.raises(service.OutputExistsError):
        service.decrypt_backup(backup, out, ROOT_KEY)
    assert out.read_text() == "keep me"
    assert service.decrypt_backup(backup, out, ROOT_KEY, overwrite=True).is_sqlite


def test_decrypt_backup_wrong_key_writes_nothing(backup, tmp_path):
    out = tmp_path / "decrypted.db"
    with pytest.raises(crypt15.Crypt15Error):
        service.decrypt_backup(backup, out, bytes(32))
    assert not out.exists()


def test_decrypt_backup_non_sqlite_payload(tmp_path):
    src = tmp_path / "zip.crypt15"
    src.write_bytes(build_backup(b"PK\x03\x04 zip-style payload"))
    result = service.decrypt_backup(src, tmp_path / "out.bin", ROOT_KEY)
    assert not result.is_sqlite and result.summary is None


def test_parse_export(tmp_path):
    path = tmp_path / "chat.txt"
    path.write_text(EXPORT, encoding="utf-8")
    info = service.parse_export(path)
    assert info.total == len(info.messages) == 8
    assert dict(info.senders) == {"Ben": 4, "Kişi 1": 4}
    assert info.kinds["media_omitted"] == 1
    assert info.first.year == 2024 and info.last.year == 2026


def test_plan_merge_is_a_dry_run(db_path):
    before = db_path.read_bytes()
    result = service.plan_merge(db_path, request())
    assert db_path.read_bytes() == before
    assert result.output is None
    assert (result.from_me, result.from_them) == (3, 3)
    assert result.kinds == {"text": 4, "media_omitted": 1, "deleted": 1}


def test_since_is_local_midnight(db_path):
    req = request(since=date(2024, 10, 8))
    assert req.since_ms == ms(2024, 10, 8, 0, 0)
    result = service.plan_merge(db_path, req)
    assert result.plan.skipped["before_since"] == 3


def test_apply_merge_writes_copy(db_path, tmp_path):
    out = tmp_path / "merged.db"
    result = service.apply_merge(db_path, out, request())
    assert result.output == out
    assert texts(out, 1)[-2:] == ["son", "<system>"]


def test_merge_errors_propagate(db_path):
    with pytest.raises(merge.MergeError):
        service.plan_merge(db_path, service.MergeRequest(request().messages, "5550001122", "Kişi 3"))


def test_plan_merge_chain(two_chats):
    results = service.plan_merge_chain(two_chats, [request(), request(SECOND_EXPORT, "5550003344")])
    assert [r.plan.chat_id for r in results] == [1, 3]
    assert [r.text for r in results[1].plan.rows] == ["geri döndüm", "hoş geldin"]


def test_chain_refuses_same_chat_twice(two_chats, tmp_path):
    twice = [request(), request(phone="905550001122")]
    with pytest.raises(service.ServiceError, match="same chat"):
        service.plan_merge_chain(two_chats, twice)
    with pytest.raises(service.ServiceError, match="same chat"):
        service.apply_merge_chain(two_chats, twice, tmp_path / "merged.db")


def test_apply_merge_chain(two_chats, tmp_path):
    before = two_chats.read_bytes()
    out = tmp_path / "out" / "merged.db"
    out.parent.mkdir()
    results = service.apply_merge_chain(two_chats, [request(), request(SECOND_EXPORT, "5550003344")], out)
    assert two_chats.read_bytes() == before
    assert [r.output for r in results] == [out.with_name("merged.db.step1"), out]
    assert list(out.parent.iterdir()) == [out]
    assert texts(out, 1)[-2:] == ["son", "<system>"]
    assert texts(out, 3) == ["ilk", "geri döndüm", "hoş geldin"]


def test_apply_merge_chain_failure_leaves_nothing(two_chats, tmp_path):
    out = tmp_path / "out" / "merged.db"
    out.parent.mkdir()
    nothing_new = request("1.3.2023 10:00 - Ben: ilk\n", "5550003344")
    with pytest.raises(merge.MergeError, match="nothing to insert"):
        service.apply_merge_chain(two_chats, [request(), nothing_new], out)
    assert list(out.parent.iterdir()) == []


def test_apply_merge_chain_needs_requests(db_path, tmp_path):
    with pytest.raises(service.ServiceError):
        service.apply_merge_chain(db_path, [], tmp_path / "merged.db")


def test_encrypt_backup_roundtrip(db_path, backup, tmp_path):
    out = tmp_path / "new" / "msgstore.db.crypt15"
    result = service.encrypt_backup(db_path, backup, out, ROOT_KEY)
    assert result.size == out.stat().st_size
    assert crypt15.decrypt(out.read_bytes(), ROOT_KEY)[0] == db_path.read_bytes()


def test_encrypt_backup_refusals(db_path, backup, tmp_path):
    not_db = tmp_path / "notes.txt"
    not_db.write_text("hello")
    with pytest.raises(service.NotSqliteError):
        service.encrypt_backup(not_db, backup, tmp_path / "a.crypt15", ROOT_KEY)
    with pytest.raises(crypt15.Crypt15Error):
        service.encrypt_backup(db_path, backup, tmp_path / "b.crypt15", bytes(32))
    with pytest.raises(service.OutputExistsError):
        service.encrypt_backup(db_path, backup, backup, ROOT_KEY)
    assert not (tmp_path / "a.crypt15").exists() and not (tmp_path / "b.crypt15").exists()


def test_build_backup_end_to_end(db_path, backup, tmp_path):
    workspace = service.create_workspace(tmp_path / "ws")
    decrypted = service.decrypt_backup(backup, workspace / "msgstore.db", ROOT_KEY).output
    result = service.build_backup(decrypted, [request()], backup,
                                  merged_database=workspace / "merged.db",
                                  output=workspace / "msgstore.db.crypt15", root_key=ROOT_KEY)
    assert result.merges[0].plan.chat_id == 1
    restored = tmp_path / "restored.db"
    service.decrypt_backup(result.backup.output, restored, ROOT_KEY)
    assert texts(restored, 1)[-2:] == ["son", "<system>"]

    for path in workspace.rglob("*"):
        data = path.read_bytes()
        assert ROOT_KEY not in data and ROOT_KEY.hex().encode() not in data


def test_plan_and_apply_repair(db_path, tmp_path):
    base = service.apply_merge(db_path, tmp_path / "merged.db", request()).output
    current = tmp_path / "current.db"
    current.write_bytes(base.read_bytes())
    with sqlite3.connect(current) as db:
        db.execute("UPDATE sqlite_sequence SET seq = (SELECT max(_id) FROM message) WHERE name = 'message'")
        cur = db.execute("INSERT INTO message (chat_row_id, from_me, key_id, timestamp, message_type,"
                         " text_data) VALUES (1, 1, 'PHONE', ?, 0, 'sonra')", (ms(2026, 10, 3, 12, 0),))
        db.execute("UPDATE message SET sort_id = _id WHERE _id = ?", (cur.lastrowid,))

    plan = service.plan_repair(current, base)
    assert set(plan.displaced) == {1}
    before = current.read_bytes()
    out = service.apply_repair(current, plan, tmp_path / "repaired.db")
    assert current.read_bytes() == before
    assert texts(out, 1)[-1] == "sonra"
    with pytest.raises(service.OutputExistsError):
        service.apply_repair(current, plan, out)


def test_repair_refuses_wrong_baseline(db_path, tmp_path):
    base = service.apply_merge(db_path, tmp_path / "merged.db", request()).output
    with pytest.raises(repair.RepairError):
        service.plan_repair(db_path, base)


def test_workspace_lifecycle(tmp_path):
    first = service.create_workspace(tmp_path)
    second = service.create_workspace(tmp_path)
    assert first != second and first.parent == tmp_path
    (first / "msgstore.db").write_bytes(b"x")
    (first / "exports").mkdir()
    (first / "exports" / "chat.txt").write_text("y")
    assert service.cleanup_workspace(first) == 2
    assert not first.exists() and second.exists()


def test_cleanup_refuses_unmarked_folder(tmp_path):
    (tmp_path / "important.txt").write_text("keep")
    with pytest.raises(service.ServiceError):
        service.cleanup_workspace(tmp_path)
    assert (tmp_path / "important.txt").exists()


def test_describe_chat(db_path):
    info = service.describe_chat(db_path, "5550001122")
    assert (info.chat_id, info.messages) == (1, 3)
    assert info.first_ms == ms(2024, 10, 7, 22, 50)
    assert (info.largest_gap.start_ms, info.largest_gap.end_ms) == (ms(2024, 10, 7, 22, 52, 55),
                                                                    ms(2026, 10, 2, 16, 24))
    assert info.suggested_since("Europe/Istanbul") == date(2024, 10, 7)


def test_describe_chat_open_tail_is_a_candidate(db_path):
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM message WHERE _id = 200")
    info = service.describe_chat(db_path, "5550001122")
    assert info.largest_gap.end_ms == merge.FAR_FUTURE


@pytest.mark.parametrize("phone, code", [("123", "phone_too_short"), ("5559999999", "chat_not_found")])
def test_describe_chat_errors_carry_codes(db_path, phone, code):
    with pytest.raises(merge.MergeError) as caught:
        service.describe_chat(db_path, phone)
    assert caught.value.code == code


def test_lid_twin_with_messages_is_reported(db_path):
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO chat (_id, jid_row_id) VALUES (9, 6)")
        add(db, 400, 9, 0, ms(2025, 6, 1, 12, 0), "lid")
    with pytest.raises(merge.MergeError) as caught:
        service.describe_chat(db_path, "5550001122")
    assert caught.value.code == "lid_chat"


def test_months(db_path):
    assert service.chat_by_month(db_path, 1) == {"2024-10": 2, "2026-10": 1}
    result = service.plan_merge(db_path, request())
    assert result.added_by_month() == {"2024-10": 3, "2025-05": 2, "2026-10": 1}


def test_merge_error_codes(db_path):
    group = EXPORT + "1.5.2025 12:02 - Kişi 3: selam\n"
    for req, code in ((service.MergeRequest(request().messages, "5550001122", "Kişi 3"), "me_not_sender"),
                      (request(group), "group_chat")):
        with pytest.raises(merge.MergeError) as caught:
            service.plan_merge(db_path, req)
        assert caught.value.code == code


def test_import_plain_text(tmp_path):
    src = tmp_path / "upload"
    src.write_text(EXPORT, encoding="utf-8")
    out = service.import_export(src, tmp_path / "chat.txt")
    assert out.read_text(encoding="utf-8") == EXPORT and not src.exists()


def test_import_iphone_zip(tmp_path):
    import zipfile
    src = tmp_path / "upload"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr("_chat.txt", EXPORT)
        archive.writestr("00000012-PHOTO.jpg", b"\xff\xd8")
    out = service.import_export(src, tmp_path / "chat.txt")
    assert out.read_text(encoding="utf-8") == EXPORT and not src.exists()


def test_import_zip_without_chat(tmp_path):
    import zipfile
    src = tmp_path / "upload"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr("photo.jpg", b"\xff\xd8")
    with pytest.raises(service.ServiceError):
        service.import_export(src, tmp_path / "chat.txt")
    assert not src.exists() and not (tmp_path / "chat.txt").exists()
