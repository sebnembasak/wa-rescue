import http.client
import json
from pathlib import Path
import re
import threading
import time

import pytest

from warescue import crypt15, service
from warescue.ui.jobs import JobRunner, JobState
from warescue.ui.server import TOKEN_HEADER, WizardServer
from warescue.ui.session import Session, display_path
from conftest import ROOT_KEY


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def session(tmp_path):
    return Session.start(tmp_path / "ws")


@pytest.fixture
def server(session):
    srv = WizardServer(session, idle_timeout=3600, watchdog_interval=0.05)
    thread = threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    yield srv
    srv.stop()
    thread.join(5)
    srv.server_close()


def request(srv, method, path, *, token=True, headers=None, host=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    sent = {"Host": host or f"127.0.0.1:{srv.port}"}
    if token:
        sent[TOKEN_HEADER] = srv.session.token if token is True else token
    if body is not None:
        sent["Content-Length"] = str(len(body))
    sent.update(headers or {})
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for name, value in sent.items():
        conn.putheader(name, value)
    conn.endheaders(body)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response, body


def api(srv, method, path, **kw):
    response, body = request(srv, method, path, **kw)
    return response.status, json.loads(body)


def test_page_is_served_with_strict_csp(server):
    response, body = request(server, "GET", "/", token=False)
    assert response.status == 200
    csp = response.getheader("Content-Security-Policy")
    nonce = re.search(r"'nonce-([^']+)'", csp)[1]
    html = body.decode("utf-8")
    assert f'<script nonce="{nonce}">' in html and "__CSP_NONCE__" not in html
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("X-Frame-Options") == "DENY"


def test_page_makes_no_external_requests(server):
    _, body = request(server, "GET", "/", token=False)
    assert not re.search(rb"(?:src|href)\s*=\s*[\"']?(?:https?:)?//", body)
    assert b"@import" not in body and b"url(http" not in body


def test_page_nonce_changes_per_request(server):
    first = request(server, "GET", "/", token=False)[0].getheader("Content-Security-Policy")
    second = request(server, "GET", "/", token=False)[0].getheader("Content-Security-Policy")
    assert first != second


def test_binds_to_loopback_only(server):
    assert server.server_address[0] == "127.0.0.1"
    assert server.url == f"http://127.0.0.1:{server.port}/#t={server.session.token}"


@pytest.mark.parametrize("token", [False, "", "wrong-token"])
def test_api_requires_token(server, token):
    status, body = api(server, "GET", "/api/session", token=token)
    assert status == 401 and body["error"] == "bad_token"


def test_session_with_token(server):
    status, body = api(server, "GET", "/api/session")
    assert status == 200
    assert body["workspace"] == display_path(server.session.workspace)
    assert body["has_key"] is False


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://127.0.0.1:1"])
def test_foreign_origin_is_refused(server, origin):
    status, body = api(server, "GET", "/api/session", headers={"Origin": origin})
    assert status == 403 and body["error"] == "bad_origin"


def test_own_origin_is_accepted(server):
    for host in ("127.0.0.1", "localhost"):
        status, _ = api(server, "GET", "/api/session", headers={"Origin": f"http://{host}:{server.port}"})
        assert status == 200


def test_cross_site_fetch_metadata_is_refused(server):
    status, _ = api(server, "GET", "/api/session", headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403


def test_page_opens_from_a_link_anywhere(server):
    response, _ = request(server, "GET", "/", token=False, headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status == 200


@pytest.mark.parametrize("host", ["evil.example", f"evil.example:{0}", "127.0.0.1"])
def test_foreign_host_is_refused(server, host):
    response, body = request(server, "GET", "/", token=False, host=host)
    assert response.status == 403 and json.loads(body)["error"] == "bad_host"
    status, _ = api(server, "GET", "/api/session", host=host)
    assert status == 403


def test_unknown_paths(server):
    assert api(server, "GET", "/api/nope")[0] == 404
    assert api(server, "GET", "/index.html", token=False)[0] == 404
    assert api(server, "POST", "/api/session")[0] == 404


def test_key_never_appears_in_responses(server):
    server.session.set_key(ROOT_KEY.hex())
    status, body = api(server, "GET", "/api/session")
    assert status == 200 and body["has_key"] is True
    raw = json.dumps(body)
    assert ROOT_KEY.hex() not in raw and ROOT_KEY.hex().upper() not in raw


def test_job_progress_and_result(server):
    release = threading.Event()

    def task(report):
        report(0.5, "halfway")
        release.wait(5)
        return {"messages": 6}

    job = server.jobs.submit("demo", task)
    assert wait_for(lambda: server.jobs.get(job.id).message == "halfway")
    status, body = api(server, "GET", f"/api/job/{job.id}")
    assert status == 200
    assert body == {"id": job.id, "kind": "demo", "state": "running", "progress": 0.5,
                    "message": "halfway", "result": None, "error": None, "error_code": None}
    release.set()
    assert wait_for(lambda: server.jobs.get(job.id).finished)
    status, body = api(server, "GET", f"/api/job/{job.id}")
    assert body["state"] == "done" and body["result"] == {"messages": 6} and body["progress"] == 1.0


def test_unknown_job(server):
    status, body = api(server, "GET", "/api/job/doesnotexist")
    assert status == 404 and body["error"] == "unknown_job"


def test_expected_errors_are_shown_unexpected_ones_are_not():
    runner = JobRunner()

    def wrong_key(_):
        raise crypt15.Crypt15Error("authentication failed: wrong key")

    def bug(_):
        raise RuntimeError("internal detail /secret/path")

    failed = runner.submit("decrypt", wrong_key)
    crashed = runner.submit("merge", bug)
    after = runner.submit("ok", lambda _: 1)
    runner.shutdown()
    assert runner.get(failed.id).error == "authentication failed: wrong key"
    assert runner.get(crashed.id).state is JobState.FAILED
    assert runner.get(crashed.id).error == "unexpected error (RuntimeError)"
    assert runner.get(after.id).result == 1


def test_jobs_run_one_at_a_time():
    runner = JobRunner()
    running = []
    overlap = []

    def task(_):
        overlap.append(len(running))
        running.append(1)
        time.sleep(0.02)
        running.pop()

    for _ in range(4):
        runner.submit("t", task)
    runner.shutdown()
    assert overlap == [0, 0, 0, 0]


def test_finish_wipes_key_and_stops(session):
    srv = WizardServer(session, watchdog_interval=0.05)
    thread = threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    session.set_key(ROOT_KEY.hex())
    status, body = api(srv, "POST", "/api/finish")
    assert status == 200 and body["ok"] is True
    thread.join(5)
    assert not thread.is_alive()
    assert not session.has_key
    srv.server_close()


def test_idle_timeout_stops_server(session):
    srv = WizardServer(session, idle_timeout=0.1, watchdog_interval=0.02)
    thread = threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and srv.stopping
    srv.server_close()


def test_idle_timeout_waits_for_running_job(session):
    srv = WizardServer(session, idle_timeout=0.05, watchdog_interval=0.02)
    release = threading.Event()
    srv.jobs.submit("long", lambda _: release.wait(5))
    thread = threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    time.sleep(0.3)
    assert thread.is_alive()
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    srv.server_close()


def test_empty_workspace_is_removed_on_close(session):
    srv = WizardServer(session)
    srv.server_close()
    assert not session.workspace.exists()


def test_used_workspace_is_kept_on_close(session):
    (session.workspace / "msgstore.db").write_bytes(b"x")
    srv = WizardServer(session)
    srv.server_close()
    assert (session.workspace / "msgstore.db").exists()


def test_session_key_handling(session):
    with pytest.raises(service.ServiceError):
        session.key()
    with pytest.raises(crypt15.Crypt15Error):
        session.set_key("1234")
    session.set_key(" ".join(ROOT_KEY.hex()[i:i + 4] for i in range(0, 64, 4)))
    assert session.key() == ROOT_KEY
    buffer = session._key
    session.clear_key()
    assert not session.has_key and buffer == bytearray(32)
    assert set(session.snapshot()) == {"workspace", "has_key", "state"}


def test_display_path_shortens_home(monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert display_path(tmp_path / "warescue-workspace" / "x") == "~/warescue-workspace/x"
    assert display_path(Path("/elsewhere/x")) == str(Path("/elsewhere/x"))


def test_how_it_works_page_is_served_offline(server):
    response, body = request(server, "GET", "/how-it-works", token=False)
    assert response.status == 200
    nonce = re.search(r"'nonce-([^']+)'", response.getheader("Content-Security-Policy"))[1]
    html = body.decode("utf-8")
    assert f'<script nonce="{nonce}">' in html
    assert not re.search(r"""(?:src|href)\s*=\s*["']?(?:https?:)?//""", html) and "@import" not in html
    assert "1a 02 00 00" in html
    assert 'lang="tr"' in html and 'lang="en"' in html
