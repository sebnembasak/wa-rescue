"""JSON endpoints of the wizard. Each handler takes the server and the
request and returns a Response or raises ApiError; all real work is done
by `warescue.service`.

Wizard state kept in the session (survives a page reload):
    step          the step the page shows
    backup        summary of the uploaded backup (step 1)
    decrypt_job   id of the latest decrypt job (step 2)
    database      summary of the decrypted database (step 2)
    exports       one record per added chat export and its settings (step 3)
    preview_job   id of the latest preview job (step 4)
    preview       per-chat dry-run results (step 4)
    build_job     id of the latest build job (step 5)
    build         summary of the new encrypted backup (step 5)
    checklist     restore steps the user ticked off (step 6)
    cleaned       true once the unencrypted files were deleted (step 6)
"""
from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from datetime import date
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import __version__, crypt15, merge, service
from .jobs import Progress
from .session import Session, display_path
from .web import ApiError, Request, Response

if TYPE_CHECKING:
    from .server import WizardServer

BACKUP_FILE = "msgstore.db.crypt15"
DATABASE_FILE = "msgstore.db"
MAX_BACKUP_BYTES = 4 * 1024 ** 3
UNSUPPORTED_SUFFIXES = (".crypt12", ".crypt14")
EXPORTS_DIR = "exports"
MERGED_FILE = "msgstore-merged.db"
OUTPUT_DIR = "new"
CHECKLIST = ("media", "copy", "uninstall", "folder", "put", "install", "restore", "key", "fresh")
STEP_COUNT = 6
MIN_PHONE_DIGITS = 7

Handler = Callable[["WizardServer", Request], Response]


def max_step(session: Session) -> int:
    """The furthest step the user may open, given what is done so far."""
    if session.state("cleaned"):
        return STEP_COUNT
    if session.state("backup") is None:
        return 1
    if session.state("database") is None:
        return 2
    exports = session.state("exports") or []
    if not exports or not all(_export_ready(e) for e in exports):
        return 3
    preview = session.state("preview")
    if preview is None or not preview["ok"]:
        return 4
    if session.state("build") is None:
        return 5
    return 6


def _refuse_while_busy(server: WizardServer) -> None:
    if server.jobs.busy:
        raise ApiError(HTTPStatus.CONFLICT, "busy", "another operation is still running")


# session

def get_session(server: WizardServer, _: Request) -> Response:
    session = server.session
    return Response.json({"version": __version__, "max_step": max_step(session),
                          **session.snapshot()})


def post_step(server: WizardServer, request: Request) -> Response:
    step = request.json().get("step")
    if not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= STEP_COUNT:
        raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "step must be a number from 1 to 6")
    if step > max_step(server.session):
        raise ApiError(HTTPStatus.CONFLICT, "step_locked", "finish the earlier steps first")
    if server.session.state("cleaned") and step < STEP_COUNT:
        raise ApiError(HTTPStatus.CONFLICT, "cleaned", "the working files were deleted")
    server.session.update_state(step=step)
    return Response.json({"step": step})


def post_finish(server: WizardServer, _: Request) -> Response:
    server.stop()
    return Response.json({"ok": True, "workspace": display_path(server.session.workspace)})


def get_job(server: WizardServer, request: Request) -> Response:
    job = server.jobs.get(request.match["id"]) if request.match else None
    if job is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "unknown_job", "no such job")
    return Response.json(job.to_json())


# backup

def post_backup(server: WizardServer, request: Request) -> Response:
    """Receive the .crypt15 file as the raw request body."""
    _refuse_while_busy(server)
    name = unquote(request.headers.get("X-Filename", ""))[:255] or BACKUP_FILE
    if name.lower().endswith(UNSUPPORTED_SUFFIXES):
        raise ApiError(HTTPStatus.BAD_REQUEST, "crypt14",
                       "only end-to-end encrypted (.crypt15) backups are supported")

    session = server.session
    partial = session.workspace / f"{BACKUP_FILE}.part"
    request.save_body(partial, MAX_BACKUP_BYTES)
    info = service.inspect_backup(partial)
    if info.layout is None:
        partial.unlink(missing_ok=True)
        raise ApiError(HTTPStatus.BAD_REQUEST, "not_crypt15", "this file is not a crypt15 backup")

    (session.workspace / DATABASE_FILE).unlink(missing_ok=True)
    _remove_exports(session)
    _forget_results(session)
    session.clear_key()
    partial.replace(session.workspace / BACKUP_FILE)
    backup = {"name": name, "size": info.size, "app_version": info.app_version,
              "md5_verified": info.md5_verified}
    session.reset_state(step=1, backup=backup)
    return Response.json(backup)


# key

def post_key(server: WizardServer, request: Request) -> Response:
    """Take the key and start decrypting in the background."""
    text = request.json().get("key")
    if not isinstance(text, str):
        raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "key must be a string")
    session = server.session
    if session.state("backup") is None:
        raise ApiError(HTTPStatus.CONFLICT, "no_backup", "add the backup file first")
    _refuse_while_busy(server)
    try:
        session.set_key(text)
    except crypt15.Crypt15Error:
        raise ApiError(HTTPStatus.BAD_REQUEST, "bad_key_format", "the key must be 64 hex digits") from None

    (session.workspace / DATABASE_FILE).unlink(missing_ok=True)
    session.remove_state("database")
    _forget_results(session)
    job = server.jobs.submit("decrypt", lambda report: _decrypt(session, report))
    session.update_state(decrypt_job=job.id)
    return Response.json({"job": job.id}, HTTPStatus.ACCEPTED)


def _decrypt(session: Session, report: Progress) -> dict[str, Any]:
    report(None, "decrypting")
    workspace = session.workspace
    try:
        result = service.decrypt_backup(workspace / BACKUP_FILE, workspace / DATABASE_FILE,
                                        session.key(), overwrite=True)
    except crypt15.WrongKeyError:
        session.clear_key()
        raise ApiError(HTTPStatus.BAD_REQUEST, "wrong_key", "the key does not match this backup") from None
    except crypt15.Crypt15Error:
        session.clear_key()
        raise
    if result.summary is None:
        (workspace / DATABASE_FILE).unlink(missing_ok=True)
        session.clear_key()
        raise ApiError(HTTPStatus.BAD_REQUEST, "not_sqlite",
                       "the backup decrypted, but it does not contain a message database")

    counts = result.summary.row_counts
    database = {"messages": counts.get("message", 0), "chats": counts.get("chat", 0),
                "size": result.size}
    session.update_state(database=database)
    return database


# chats

def _exports(session: Session) -> list[dict[str, Any]]:
    return list(session.state("exports") or [])


def _export_path(session: Session, export_id: str) -> Path:
    return session.workspace / EXPORTS_DIR / f"{export_id}.txt"


def _remove_exports(session: Session) -> None:
    folder = session.workspace / EXPORTS_DIR
    if folder.is_dir():
        for path in folder.iterdir():
            path.unlink()
        folder.rmdir()


def _export_ready(export: dict[str, Any]) -> bool:
    return (export["me"] is not None and not export["group"]
            and export["chat"] is not None and export["chat_error"] is None)


def _find_export(session: Session, request: Request) -> tuple[list[dict[str, Any]], int]:
    exports = _exports(session)
    export_id = request.match["id"] if request.match else ""
    for index, export in enumerate(exports):
        if export["id"] == export_id:
            return exports, index
    raise ApiError(HTTPStatus.NOT_FOUND, "unknown_export", "no such export")


def _save_exports(session: Session, exports: list[dict[str, Any]]) -> None:
    session.update_state(exports=exports)
    _forget_results(session)


def _output_path(session: Session) -> Path:
    return session.workspace / OUTPUT_DIR / BACKUP_FILE


def _forget_results(session: Session) -> None:
    session.remove_state("preview", "build", "checklist")
    (session.workspace / MERGED_FILE).unlink(missing_ok=True)
    _output_path(session).unlink(missing_ok=True)


def _valid_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ApiError(HTTPStatus.BAD_REQUEST, "bad_timezone", f"unknown time zone {name!r}") from None
    return name


def _local(ms: int | None, tz: str) -> str | None:
    """Local "YYYY-MM-DDTHH:MM" for display; None for open gap ends."""
    if ms is None or ms in (0, merge.FAR_FUTURE):
        return None
    return merge.ms_to_utc(ms).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")


def post_export(server: WizardServer, request: Request) -> Response:
    """Receive a chat export (.txt, or an iPhone .zip) as the raw request body."""
    _refuse_while_busy(server)
    session = server.session
    if session.state("database") is None:
        raise ApiError(HTTPStatus.CONFLICT, "no_database", "open the backup first")
    name = unquote(request.headers.get("X-Filename", ""))[:255] or "_chat.txt"
    tz = _valid_timezone(request.headers.get("X-Timezone") or service.DEFAULT_TZ)

    folder = session.workspace / EXPORTS_DIR
    folder.mkdir(exist_ok=True)
    export_id = secrets.token_hex(6)
    upload = folder / f"{export_id}.upload"
    target = _export_path(session, export_id)
    request.save_body(upload, service.MAX_EXPORT_BYTES)
    try:
        service.import_export(upload, target)
        summary = service.parse_export(target, tz)
    except service.ServiceError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "bad_zip", str(exc)) from None
    except ValueError:   # not UTF-8, or a date line that is not a real date
        target.unlink(missing_ok=True)
        raise ApiError(HTTPStatus.BAD_REQUEST, "not_export", "the file is not a WhatsApp chat export") from None
    if not summary.senders:
        target.unlink(missing_ok=True)
        raise ApiError(HTTPStatus.BAD_REQUEST, "not_export", "no chat messages found in this file")

    export = {
        "id": export_id, "name": name, "tz": tz,
        "total": sum(summary.senders.values()),
        "first": summary.first.strftime("%Y-%m-%dT%H:%M"),
        "last": summary.last.strftime("%Y-%m-%dT%H:%M"),
        "senders": [{"name": n, "count": c} for n, c in summary.senders.most_common()],
        "group": len(summary.senders) > 2,
        "me": None, "phone": "", "since": None, "since_auto": True,
        "chat": None, "chat_error": None, "suggested_since": None,
    }
    _save_exports(session, [*_exports(session), export])
    return Response.json(export)


def _lookup_chat(session: Session, export: dict[str, Any]) -> None:
    """Fill in the backup's chat for the export's phone number, and the
    suggested "only add messages after" date."""
    export.update(chat=None, chat_error=None, suggested_since=None)
    digits = "".join(ch for ch in export["phone"] if ch.isdigit())
    if not digits:
        return
    if len(digits) < MIN_PHONE_DIGITS:
        export["chat_error"] = "phone_too_short"
        return
    try:
        info = service.describe_chat(session.workspace / DATABASE_FILE, export["phone"])
    except merge.MergeError as exc:
        export["chat_error"] = exc.code
        return
    tz = export["tz"]
    gap = info.largest_gap
    export["chat"] = {
        "messages": info.messages, "first": _local(info.first_ms, tz), "last": _local(info.last_ms, tz),
        "gap_start": _local(gap.start_ms, tz) if gap else None,
        "gap_end": _local(gap.end_ms, tz) if gap else None,
    }
    suggested = info.suggested_since(tz)
    export["suggested_since"] = suggested.isoformat() if suggested else None
    if export["since_auto"]:
        export["since"] = export["suggested_since"]


def post_export_settings(server: WizardServer, request: Request) -> Response:
    """Update any of: me, phone, since ("YYYY-MM-DD" or null), tz."""
    _refuse_while_busy(server)
    session = server.session
    exports, index = _find_export(session, request)
    export = dict(exports[index])
    body = request.json()

    if "me" in body:
        me = body["me"]
        if me is not None and me not in {s["name"] for s in export["senders"]}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "me_not_sender", "that name is not a sender in the export")
        export["me"] = me
    if "since" in body:
        since = body["since"] or None
        if since is not None:
            try:
                since = date.fromisoformat(since).isoformat()
            except (TypeError, ValueError):
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "since must be YYYY-MM-DD") from None
        export.update(since=since, since_auto=False)
    relookup = False
    if "tz" in body:
        if not isinstance(body["tz"], str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "tz must be a string")
        relookup = body["tz"] != export["tz"]
        export["tz"] = _valid_timezone(body["tz"])
    if "phone" in body:
        if not isinstance(body["phone"], str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "phone must be a string")
        relookup = relookup or body["phone"].strip() != export["phone"]
        export["phone"] = body["phone"].strip()[:32]
    if relookup:
        _lookup_chat(session, export)

    exports[index] = export
    _save_exports(session, exports)
    return Response.json(export)


def post_export_delete(server: WizardServer, request: Request) -> Response:
    _refuse_while_busy(server)
    session = server.session
    exports, index = _find_export(session, request)
    _export_path(session, exports.pop(index)["id"]).unlink(missing_ok=True)
    _save_exports(session, exports)
    return Response.json({"ok": True})


# preview
def post_preview(server: WizardServer, _: Request) -> Response:
    """Dry-run every export against the decrypted backup in the background."""
    session = server.session
    if max_step(session) < 4:
        raise ApiError(HTTPStatus.CONFLICT, "step_locked", "finish the earlier steps first")
    _refuse_while_busy(server)
    _forget_results(session)
    exports = _exports(session)
    job = server.jobs.submit("preview", lambda report: _preview(session, exports, report))
    session.update_state(preview_job=job.id)
    return Response.json({"job": job.id}, HTTPStatus.ACCEPTED)


def _months(first: str, last: str) -> list[str]:
    year, month = map(int, first.split("-"))
    end = tuple(map(int, last.split("-")))
    out = []
    while (year, month) <= end:
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def _merge_request(session: Session, export: dict[str, Any]) -> service.MergeRequest:
    tz = export["tz"]
    summary = service.parse_export(_export_path(session, export["id"]), tz)
    since = date.fromisoformat(export["since"]) if export["since"] else None
    return service.MergeRequest(summary.messages, export["phone"], export["me"], tz, since=since)


def _preview_one(session: Session, export: dict[str, Any], seen: set[int]) -> dict[str, Any]:
    outcome: dict[str, Any] = {"export_id": export["id"], "name": export["name"],
                               "phone": export["phone"], "error": None}
    tz = export["tz"]
    request = _merge_request(session, export)
    database = session.workspace / DATABASE_FILE
    try:
        result = service.plan_merge(database, request)
    except merge.MergeError as exc:
        outcome["error"] = exc.code
        return outcome

    plan = result.plan
    if plan.chat_id in seen:
        outcome["error"] = "same_chat"
        return outcome
    seen.add(plan.chat_id)
    if not plan.rows:
        outcome["error"] = "nothing_to_insert"

    existing = service.chat_by_month(database, plan.chat_id, tz)
    added = result.added_by_month()
    months = sorted({*existing, *added})
    outcome.update(
        chat_id=plan.chat_id, to_insert=len(plan.rows),
        from_me=result.from_me, from_them=result.from_them,
        kinds=dict(result.kinds.most_common()), skipped=dict(plan.skipped.most_common()),
        gaps=[{"start": _local(g.start_ms, tz), "end": _local(g.end_ms, tz), "inserted": g.inserted}
              for g in plan.filled],
        timeline=[[m, existing.get(m, 0), added.get(m, 0)] for m in _months(months[0], months[-1])]
        if months else [],
    )
    return outcome


def _preview(session: Session, exports: list[dict[str, Any]], report: Progress) -> dict[str, Any]:
    seen: set[int] = set()
    chats = []
    for done, export in enumerate(exports):
        report(done / len(exports), export["name"])
        chats.append(_preview_one(session, export, seen))
    preview = {"chats": chats, "ok": bool(chats) and all(c["error"] is None for c in chats)}
    session.update_state(preview=preview)
    return preview


#  build

def post_build(server: WizardServer, _: Request) -> Response:
    """Merge every export into a copy of the backup and encrypt it, in the background."""
    session = server.session
    if max_step(session) < 5 or session.state("cleaned"):
        raise ApiError(HTTPStatus.CONFLICT, "step_locked", "finish the earlier steps first")
    _refuse_while_busy(server)
    if not session.has_key:
        raise ApiError(HTTPStatus.CONFLICT, "no_key", "enter the key again in step 2")
    session.remove_state("build", "checklist")
    exports = _exports(session)
    job = server.jobs.submit("build", lambda report: _build(session, exports, report))
    session.update_state(build_job=job.id)
    return Response.json({"job": job.id}, HTTPStatus.ACCEPTED)


def _build(session: Session, exports: list[dict[str, Any]], report: Progress) -> dict[str, Any]:
    workspace = session.workspace
    merged, output = workspace / MERGED_FILE, _output_path(session)
    merged.unlink(missing_ok=True)
    output.unlink(missing_ok=True)

    def progress(stage: str, done: int, total: int) -> None:
        # Merging dominates; encrypting and the read-back check take the rest.
        if stage == "merge":
            report(0.8 * done / total, f"merge:{done + 1}:{total}")
        else:
            report(0.8, "encrypt")

    requests = [_merge_request(session, export) for export in exports]
    try:
        result = service.build_backup(workspace / DATABASE_FILE, requests, workspace / BACKUP_FILE,
                                      merged_database=merged, output=output,
                                      root_key=session.key(), progress=progress)
    except merge.MergeError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, exc.code, str(exc)) from None
    counts = service.summarize_database(merged).row_counts
    build = {"size": result.backup.size, "messages": counts.get("message", 0),
             "added": sum(len(m.plan.rows) for m in result.merges), "chats": len(result.merges),
             "path": display_path(output)}
    session.update_state(build=build)
    return build


def get_download(server: WizardServer, _: Request) -> Response:
    output = _output_path(server.session)
    if server.session.state("build") is None or not output.is_file():
        raise ApiError(HTTPStatus.NOT_FOUND, "no_build", "build the new backup first")
    return Response.download(output, BACKUP_FILE)


# restore and clean up

def post_checklist(server: WizardServer, request: Request) -> Response:
    body = request.json()
    item, done = body.get("item"), body.get("done")
    if item not in CHECKLIST or not isinstance(done, bool):
        raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "unknown checklist item")
    session = server.session
    ticked = set(session.state("checklist") or [])
    if done:
        ticked.add(item)
    else:
        ticked.discard(item)
    session.update_state(checklist=[i for i in CHECKLIST if i in ticked])
    return Response.json({"checklist": session.state("checklist")})


def post_cleanup(server: WizardServer, _: Request) -> Response:
    """Delete every unencrypted copy of the chats and forget the key. Encrypted backups stay."""
    session = server.session
    if session.state("build") is None:
        raise ApiError(HTTPStatus.CONFLICT, "step_locked", "build the new backup first")
    _refuse_while_busy(server)
    workspace = session.workspace
    plaintext = [workspace / DATABASE_FILE, workspace / MERGED_FILE]
    removed = sum(1 for path in plaintext if path.is_file())
    for path in plaintext:
        path.unlink(missing_ok=True)
    folder = workspace / EXPORTS_DIR
    if folder.is_dir():
        removed += sum(1 for _ in folder.iterdir())
    _remove_exports(session)
    session.clear_key()                  # nothing left that needs it
    session.update_state(cleaned=True, step=STEP_COUNT)
    return Response.json({"removed": removed})


def post_wipe(server: WizardServer, request: Request) -> Response:
    """Delete the whole workspace, encrypted backups included and stop."""
    if request.json().get("confirm") is not True:
        raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "confirm must be true")
    _refuse_while_busy(server)
    removed = service.cleanup_workspace(server.session.workspace)
    server.stop()
    return Response.json({"removed": removed})


ROUTES: tuple[tuple[str, re.Pattern[str], Handler], ...] = tuple(
    (method, re.compile(f"^{pattern}$"), handler) for method, pattern, handler in (
        ("GET", "/api/session", get_session),
        ("POST", "/api/step", post_step),
        ("POST", "/api/finish", post_finish),
        ("GET", r"/api/job/(?P<id>[A-Za-z0-9_-]{1,64})", get_job),
        ("POST", "/api/backup", post_backup),
        ("POST", "/api/key", post_key),
        ("POST", "/api/exports", post_export),
        ("POST", r"/api/exports/(?P<id>[0-9a-f]{12})", post_export_settings),
        ("POST", r"/api/exports/(?P<id>[0-9a-f]{12})/delete", post_export_delete),
        ("POST", "/api/preview", post_preview),
        ("POST", "/api/build", post_build),
        ("GET", "/api/download", get_download),
        ("POST", "/api/checklist", post_checklist),
        ("POST", "/api/cleanup", post_cleanup),
        ("POST", "/api/wipe", post_wipe),
    )
)
