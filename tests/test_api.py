import json
import threading
from urllib.parse import quote

import pytest

from warescue.ui import api as wizard
from warescue.ui.session import display_path
from conftest import ROOT_KEY, build_backup
from test_merge import EXPORT, db_path  # noqa: F401
from test_server import api, request, server, session, wait_for  # noqa: F401

KEY_SHOWN = " ".join(ROOT_KEY.hex()[i:i + 4] for i in range(0, 64, 4))


@pytest.fixture
def backup_bytes(db_path):
    return build_backup(db_path.read_bytes())


def upload(srv, data, name="msgstore.db.crypt15"):
    return api(srv, "POST", "/api/backup", body=data,
               headers={"X-Filename": quote(name), "Content-Type": "application/octet-stream"})


def post_json(srv, path, payload):
    return api(srv, "POST", path, body=json.dumps(payload).encode(),
               headers={"Content-Type": "application/json"})


def decrypt(srv, key=KEY_SHOWN):
    status, body = post_json(srv, "/api/key", {"key": key})
    assert status == 202
    assert wait_for(lambda: srv.jobs.get(body["job"]).finished)
    return api(srv, "GET", f"/api/job/{body['job']}")[1]


def session_state(srv):
    return api(srv, "GET", "/api/session")[1]


def test_upload_backup(server, backup_bytes):
    status, body = upload(server, backup_bytes, "msgstore-2026.db.crypt15")
    assert status == 200
    assert body == {"name": "msgstore-2026.db.crypt15", "size": len(backup_bytes),
                    "app_version": "2.24.0.0", "md5_verified": True}
    workspace = server.session.workspace
    assert (workspace / wizard.BACKUP_FILE).read_bytes() == backup_bytes
    assert not (workspace / f"{wizard.BACKUP_FILE}.part").exists()
    state = session_state(server)
    assert state["max_step"] == 2 and state["state"]["backup"] == body


def test_crypt14_is_refused_by_name(server, backup_bytes):
    status, body = upload(server, backup_bytes, "msgstore.db.crypt14")
    assert status == 400 and body["error"] == "crypt14"
    assert not (server.session.workspace / wizard.BACKUP_FILE).exists()


def test_non_backup_is_refused(server):
    status, body = upload(server, b"\xff" + b"not a backup" * 10)
    assert status == 400 and body["error"] == "not_crypt15"
    assert [p.name for p in server.session.workspace.iterdir()] == [".warescue-workspace"]
    assert session_state(server)["max_step"] == 1


def test_upload_needs_content_length(server):
    status, body = api(server, "POST", "/api/backup")
    assert status == 411 and body["error"] == "bad_request"


def test_upload_refused_while_busy(server, backup_bytes):
    release = threading.Event()
    server.jobs.submit("long", lambda _: release.wait(5))
    try:
        status, body = upload(server, backup_bytes)
        assert status == 409 and body["error"] == "busy"
    finally:
        release.set()


def test_new_upload_resets_later_steps(server, backup_bytes):
    upload(server, backup_bytes)
    assert decrypt(server)["state"] == "done"
    upload(server, backup_bytes)
    state = session_state(server)
    assert state["max_step"] == 2 and "database" not in state["state"]
    assert not state["has_key"]
    assert not (server.session.workspace / wizard.DATABASE_FILE).exists()


def test_key_before_backup(server):
    status, body = post_json(server, "/api/key", {"key": KEY_SHOWN})
    assert status == 409 and body["error"] == "no_backup"


@pytest.mark.parametrize("key", ["1234", "zz" * 32, ROOT_KEY.hex() + "00"])
def test_malformed_key(server, backup_bytes, key):
    upload(server, backup_bytes)
    status, body = post_json(server, "/api/key", {"key": key})
    assert status == 400 and body["error"] == "bad_key_format"
    assert not server.session.has_key


def test_key_must_be_a_string(server, backup_bytes):
    upload(server, backup_bytes)
    assert post_json(server, "/api/key", {"key": 123})[1]["error"] == "bad_request"
    status, body = api(server, "POST", "/api/key", body=b"{not json")
    assert status == 400 and body["error"] == "bad_request"


def test_wrong_key(server, backup_bytes):
    upload(server, backup_bytes)
    job = decrypt(server, "00" * 32)
    assert job["state"] == "failed" and job["error_code"] == "wrong_key"
    assert not server.session.has_key
    assert not (server.session.workspace / wizard.DATABASE_FILE).exists()
    assert session_state(server)["max_step"] == 2


def test_right_key_decrypts(server, backup_bytes, db_path):
    upload(server, backup_bytes)
    job = decrypt(server)
    assert job["state"] == "done"
    assert job["result"] == {"messages": 4, "chats": 1, "size": len(db_path.read_bytes())}
    assert (server.session.workspace / wizard.DATABASE_FILE).read_bytes() == db_path.read_bytes()
    state = session_state(server)
    assert state["max_step"] == 3 and state["has_key"]
    assert state["state"]["database"] == job["result"]
    assert state["state"]["decrypt_job"] == job["id"]


def test_key_never_leaks(server, backup_bytes):
    upload(server, backup_bytes)
    job = decrypt(server)
    responses = json.dumps([job, session_state(server)])
    for form in (ROOT_KEY.hex(), ROOT_KEY.hex().upper(), KEY_SHOWN):
        assert form not in responses
    for path in server.session.workspace.rglob("*"):
        data = path.read_bytes()
        assert ROOT_KEY not in data and ROOT_KEY.hex().encode() not in data


def test_steps_are_unlocked_in_order(server, backup_bytes):
    assert post_json(server, "/api/step", {"step": 2})[1]["error"] == "step_locked"
    upload(server, backup_bytes)
    assert post_json(server, "/api/step", {"step": 2})[0] == 200
    assert session_state(server)["state"]["step"] == 2
    assert post_json(server, "/api/step", {"step": 3})[1]["error"] == "step_locked"
    decrypt(server)
    assert post_json(server, "/api/step", {"step": 3})[0] == 200


@pytest.mark.parametrize("step", [0, 7, "2", True, None])
def test_invalid_step(server, step):
    status, body = post_json(server, "/api/step", {"step": step})
    assert status == 400 and body["error"] == "bad_request"


def test_json_body_size_is_limited(server):
    status, body = api(server, "POST", "/api/step", body=b" " * (64 * 1024 + 1))
    assert status == 413 and body["error"] == "too_large"


def test_reload_keeps_progress(server, backup_bytes):
    upload(server, backup_bytes)
    post_json(server, "/api/step", {"step": 2})
    decrypt(server)
    _, page = request(server, "GET", "/", token=False)
    assert page.startswith(b"<!doctype html>")
    state = session_state(server)
    assert state["state"]["step"] == 2 and state["max_step"] == 3


@pytest.fixture
def opened(server, backup_bytes):
    upload(server, backup_bytes)
    decrypt(server)
    return server


def add_export(srv, text=EXPORT, name="WhatsApp Chat with O.txt", tz="Europe/Istanbul"):
    return api(srv, "POST", "/api/exports", body=text.encode("utf-8"),
               headers={"X-Filename": quote(name), "X-Timezone": tz})


def ready_export(srv, **settings):
    _, exp = add_export(srv)
    status, exp = post_json(srv, f"/api/exports/{exp['id']}",
                            {"me": "Ben", "phone": "0555 000 11 22", **settings})
    assert status == 200
    return exp


def test_export_needs_open_backup(server, backup_bytes):
    upload(server, backup_bytes)
    status, body = add_export(server)
    assert status == 409 and body["error"] == "no_database"


def test_add_export(opened):
    status, exp = add_export(opened)
    assert status == 200
    assert exp["total"] == 8 and exp["group"] is False and exp["me"] is None
    assert exp["senders"] == [{"name": "Ben", "count": 4}, {"name": "Kişi 1", "count": 4}]
    assert exp["first"] == "2024-10-07T22:50" and exp["last"] == "2026-10-02T16:09"
    assert (opened.session.workspace / "exports" / f"{exp['id']}.txt").read_text(encoding="utf-8") == EXPORT
    assert session_state(opened)["max_step"] == 3


def test_add_export_from_zip(opened):
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("_chat.txt", EXPORT)
    status, exp = api(opened, "POST", "/api/exports", body=buffer.getvalue(),
                      headers={"X-Filename": "WhatsApp Chat - O.zip"})
    assert status == 200 and exp["total"] == 8


@pytest.mark.parametrize("data, code", [(b"just some notes\n", "not_export"),
                                        (b"\xff\xfe\x00bad", "not_export"),
                                        (b"PK\x05\x06" + bytes(18), "bad_zip")])
def test_bad_exports(opened, data, code):
    status, body = api(opened, "POST", "/api/exports", body=data, headers={"X-Filename": "x.txt"})
    assert status == 400 and body["error"] == code
    assert list((opened.session.workspace / "exports").iterdir()) == []


def test_bad_timezone(opened):
    status, body = add_export(opened, tz="Mars/Olympus")
    assert status == 400 and body["error"] == "bad_timezone"


def test_group_export_is_flagged(opened):
    _, exp = add_export(opened, EXPORT + "1.5.2025 12:02 - Kişi 3: selam\n")
    assert exp["group"] is True


def test_settings_look_up_the_chat(opened):
    exp = ready_export(opened)
    assert exp["chat_error"] is None
    assert exp["chat"] == {"messages": 3, "first": "2024-10-07T22:50", "last": "2026-10-02T16:24",
                           "gap_start": "2024-10-07T22:52", "gap_end": "2026-10-02T16:24"}
    assert exp["suggested_since"] == exp["since"] == "2024-10-07" and exp["since_auto"]
    assert session_state(opened)["max_step"] == 4


def test_user_date_wins_over_suggestion(opened):
    exp = ready_export(opened, since=None)
    assert exp["since"] is None and not exp["since_auto"]
    _, exp = post_json(opened, f"/api/exports/{exp['id']}", {"phone": "5550001122"})
    assert exp["since"] is None


@pytest.mark.parametrize("phone, code", [("12 34", "phone_too_short"), ("+90 555 999 99 99", "chat_not_found")])
def test_phone_problems(opened, phone, code):
    _, exp = add_export(opened)
    _, exp = post_json(opened, f"/api/exports/{exp['id']}", {"me": "Ben", "phone": phone})
    assert exp["chat"] is None and exp["chat_error"] == code
    assert session_state(opened)["max_step"] == 3


def test_settings_validation(opened):
    _, exp = add_export(opened)
    path = f"/api/exports/{exp['id']}"
    assert post_json(opened, path, {"me": "Kişi 3"})[1]["error"] == "me_not_sender"
    assert post_json(opened, path, {"since": "07.10.2024"})[1]["error"] == "bad_request"
    assert post_json(opened, path, {"tz": "Nowhere"})[1]["error"] == "bad_timezone"
    assert post_json(opened, path, {"phone": 5550001122})[1]["error"] == "bad_request"
    assert post_json(opened, "/api/exports/000000000000", {"me": "Ben"})[0] == 404


def test_remove_export(opened):
    exp = ready_export(opened)
    status, _ = api(opened, "POST", f"/api/exports/{exp['id']}/delete")
    assert status == 200
    assert session_state(opened)["state"]["exports"] == []
    assert not (opened.session.workspace / "exports" / f"{exp['id']}.txt").exists()


def test_new_backup_drops_exports(opened, backup_bytes):
    ready_export(opened)
    upload(opened, backup_bytes)
    assert "exports" not in session_state(opened)["state"]
    assert not (opened.session.workspace / "exports").exists()


def preview(srv):
    status, body = api(srv, "POST", "/api/preview")
    assert status == 202
    assert wait_for(lambda: srv.jobs.get(body["job"]).finished)
    return api(srv, "GET", f"/api/job/{body['job']}")[1]


def test_preview_locked_until_exports_ready(opened):
    add_export(opened)
    status, body = api(opened, "POST", "/api/preview")
    assert status == 409 and body["error"] == "step_locked"


def test_preview(opened):
    ready_export(opened, since=None)
    job = preview(opened)
    assert job["state"] == "done" and job["result"]["ok"]
    chat = job["result"]["chats"][0]
    assert chat["error"] is None and chat["chat_id"] == 1
    assert (chat["to_insert"], chat["from_me"], chat["from_them"]) == (6, 3, 3)
    assert chat["kinds"] == {"text": 4, "media_omitted": 1, "deleted": 1}
    assert chat["skipped"] == {"already_in_db": 1, "duplicate_at_edge": 1}
    assert chat["gaps"] == [{"start": "2024-10-07T22:52", "end": "2026-10-02T16:24", "inserted": 6}]
    timeline = chat["timeline"]
    assert timeline[0] == ["2024-10", 2, 3] and timeline[-1] == ["2026-10", 1, 1]
    assert len(timeline) == 25 and ["2025-05", 0, 2] in timeline
    state = session_state(opened)
    assert state["max_step"] == 5 and state["state"]["preview"] == job["result"]


def test_preview_uses_since(opened):
    ready_export(opened, since="2024-10-08")
    chat = preview(opened)["result"]["chats"][0]
    assert chat["skipped"]["before_since"] == 3 and chat["to_insert"] == 5


def test_preview_flags_same_chat_twice(opened):
    ready_export(opened)
    ready_export(opened)
    result = preview(opened)["result"]
    assert not result["ok"]
    assert [c["error"] for c in result["chats"]] == [None, "same_chat"]
    assert session_state(opened)["max_step"] == 4


def test_preview_flags_nothing_to_add(opened):
    ready_export(opened, since="2030-01-01")
    chat = preview(opened)["result"]["chats"][0]
    assert chat["error"] == "nothing_to_insert" and chat["to_insert"] == 0


def test_changing_an_export_invalidates_preview(opened):
    exp = ready_export(opened)
    preview(opened)
    post_json(opened, f"/api/exports/{exp['id']}", {"since": None})
    state = session_state(opened)
    assert "preview" not in state["state"] and state["max_step"] == 4


def test_handler_crash_returns_json(opened, monkeypatch):
    from warescue import service

    def boom(*_):
        raise RuntimeError("bug")
    monkeypatch.setattr(service, "import_export", boom)
    status, body = add_export(opened)
    assert status == 500 and body["error"] == "unexpected"


@pytest.fixture
def previewed(opened):
    ready_export(opened)
    assert preview(opened)["result"]["ok"]
    return opened


def build(srv):
    status, body = api(srv, "POST", "/api/build")
    assert status == 202
    assert wait_for(lambda: srv.jobs.get(body["job"]).finished)
    return api(srv, "GET", f"/api/job/{body['job']}")[1]


def test_build_locked_before_preview(opened):
    ready_export(opened)
    status, body = api(opened, "POST", "/api/build")
    assert status == 409 and body["error"] == "step_locked"


def test_build(previewed, db_path):
    from warescue import crypt15
    job = build(previewed)
    assert job["state"] == "done"
    result = job["result"]
    workspace = previewed.session.workspace
    output = workspace / wizard.OUTPUT_DIR / wizard.BACKUP_FILE
    assert result == {"size": output.stat().st_size, "messages": 10, "added": 6, "chats": 1,
                      "path": display_path(output)}
    rebuilt, _, _ = crypt15.decrypt(output.read_bytes(), ROOT_KEY)
    assert rebuilt == (workspace / wizard.MERGED_FILE).read_bytes()
    assert (workspace / wizard.DATABASE_FILE).read_bytes() == db_path.read_bytes()
    state = session_state(previewed)
    assert state["max_step"] == 6 and state["state"]["build"] == result


def test_build_needs_key(previewed):
    previewed.session.clear_key()
    status, body = api(previewed, "POST", "/api/build")
    assert status == 409 and body["error"] == "no_key"


def test_download(previewed):
    build(previewed)
    response, body = request(previewed, "GET", "/api/download")
    assert response.status == 200
    assert response.getheader("Content-Disposition") == 'attachment; filename="msgstore.db.crypt15"'
    assert body == (previewed.session.workspace / wizard.OUTPUT_DIR / wizard.BACKUP_FILE).read_bytes()
    assert request(previewed, "GET", "/api/download", token=False)[0].status == 401


def test_download_before_build(previewed):
    status, body = api(previewed, "GET", "/api/download")
    assert status == 404 and body["error"] == "no_build"


def test_changing_exports_drops_build(previewed):
    build(previewed)
    exp = session_state(previewed)["state"]["exports"][0]
    post_json(previewed, f"/api/exports/{exp['id']}", {"since": None})
    state = session_state(previewed)
    assert "build" not in state["state"] and state["max_step"] == 4
    assert not (previewed.session.workspace / wizard.OUTPUT_DIR / wizard.BACKUP_FILE).exists()
    assert not (previewed.session.workspace / wizard.MERGED_FILE).exists()


@pytest.fixture
def built(previewed):
    assert build(previewed)["state"] == "done"
    return previewed


def test_checklist(built):
    assert post_json(built, "/api/checklist", {"item": "uninstall", "done": True})[0] == 200
    post_json(built, "/api/checklist", {"item": "media", "done": True})
    _, body = post_json(built, "/api/checklist", {"item": "uninstall", "done": False})
    assert body["checklist"] == ["media"]
    assert session_state(built)["state"]["checklist"] == ["media"]
    assert post_json(built, "/api/checklist", {"item": "dance", "done": True})[0] == 400


def test_cleanup_removes_plaintext_only(built):
    workspace = built.session.workspace
    status, body = api(built, "POST", "/api/cleanup")
    assert status == 200 and body["removed"] == 3
    left = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*") if p.is_file())
    assert left == [".warescue-workspace", "msgstore.db.crypt15", "new/msgstore.db.crypt15"]
    state = session_state(built)
    assert state["state"]["cleaned"] and state["max_step"] == 6 and not state["has_key"]
    assert post_json(built, "/api/step", {"step": 3})[1]["error"] == "cleaned"
    assert request(built, "GET", "/api/download")[0].status == 200


def test_wipe_needs_confirmation(built):
    status, body = post_json(built, "/api/wipe", {})
    assert status == 400 and built.session.workspace.exists()


def test_wipe_deletes_everything_and_stops(built):
    workspace = built.session.workspace
    status, _ = post_json(built, "/api/wipe", {"confirm": True})
    assert status == 200
    assert not workspace.exists()
    assert wait_for(lambda: built.stopping) and not built.session.has_key
