"""Operations shared by the command line and the browser wizard.

Every function takes plain values and returns a typed result. Nothing here
prints, prompts or reads the environment; presentation belongs to the
callers. The backup key is always passed in as bytes and never stored.
"""
from __future__ import annotations

import shutil
import sqlite3
import time as clock
import zipfile
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import chatexport, crypt15, merge, protowire, repair
from .chatexport import ExportMessage

DEFAULT_TZ = "Europe/Istanbul"
HEAD_BYTES = 64
SUMMARY_TABLES = ("message", "chat", "jid")
APP_VERSION_PATH = (4, 1)
WORKSPACE_MARKER = ".warescue-workspace"
MAX_EXPORT_BYTES = 512 * 1024 ** 2

# Called as progress(stage, done, total) with stage "merge" or "encrypt".
BuildProgress = Callable[[str, int, int], None]


class ServiceError(Exception):
    """A request the service refuses; the message says why."""


class OutputExistsError(ServiceError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} exists")
        self.path = path


class NotSqliteError(ServiceError):
    def __init__(self, path: str | Path) -> None:
        super().__init__(f"{path} is not a SQLite database")
        self.path = path


class RoundTripError(ServiceError):
    pass


# helpers

def ensure_new_output(path: str | Path, overwrite: bool = False) -> None:
    if Path(path).exists() and not overwrite:
        raise OutputExistsError(Path(path))


def is_sqlite_file(path: str | Path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(len(crypt15.SQLITE_MAGIC)) == crypt15.SQLITE_MAGIC


def _connect_readonly(path: str | Path) -> sqlite3.Connection:
    # A file: URI built from an absolute path survives spaces, '#' and '?'
    # in folder names, and Windows drive letters.
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)


def _write_new(path: Path, data: bytes) -> None:
    """Write `data`, leaving no partial file behind if writing fails."""
    try:
        path.write_bytes(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _quick_check(db: sqlite3.Connection) -> None:
    (check,) = db.execute("PRAGMA quick_check").fetchone()
    if check != "ok":
        raise merge.MergeError(f"sqlite quick_check failed: {check}", "quick_check_failed")


# inspect

@dataclass(frozen=True)
class BackupInfo:
    size: int
    head: bytes                      # first bytes of the file, for a hex dump
    md5_verified: bool
    layout: crypt15.Layout | None    # None when the container could not be parsed
    layout_error: str | None = None

    @property
    def app_version(self) -> str | None:
        if self.layout is None:
            return None
        found = protowire.find(self.layout.fields, *APP_VERSION_PATH)
        if found is None or not isinstance(found.value, bytes):
            return None
        return found.value.decode("ascii", errors="replace")


def inspect_backup(path: str | Path) -> BackupInfo:
    data = Path(path).read_bytes()
    md5_verified = crypt15.has_md5_trailer(data)
    try:
        layout = crypt15.parse_layout(data)
    except crypt15.Crypt15Error as exc:
        return BackupInfo(len(data), data[:HEAD_BYTES], md5_verified, None, str(exc))
    return BackupInfo(len(data), data[:HEAD_BYTES], md5_verified, layout)


# decrypt

@dataclass(frozen=True)
class DatabaseSummary:
    tables: tuple[str, ...]
    row_counts: dict[str, int]       # only for SUMMARY_TABLES that exist


@dataclass(frozen=True)
class DecryptResult:
    output: Path
    size: int
    layout_label: str                # which trailer shape authenticated
    summary: DatabaseSummary | None  # None when the payload is not SQLite

    @property
    def is_sqlite(self) -> bool:
        return self.summary is not None


def summarize_database(path: str | Path) -> DatabaseSummary:
    with closing(_connect_readonly(path)) as db:
        tables = tuple(name for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
        counts = {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                  for name in SUMMARY_TABLES if name in tables}
    return DatabaseSummary(tables, counts)


def decrypt_backup(source: str | Path, output: str | Path, root_key: bytes,
                   *, overwrite: bool = False) -> DecryptResult:
    output = Path(output)
    ensure_new_output(output, overwrite)
    database, _, label = crypt15.decrypt(Path(source).read_bytes(), root_key)
    _write_new(output, database)
    summary = summarize_database(output) if database.startswith(crypt15.SQLITE_MAGIC) else None
    return DecryptResult(output, len(database), label, summary)


# parse

@dataclass(frozen=True)
class ExportSummary:
    messages: list[ExportMessage]
    total: int
    first: datetime | None
    last: datetime | None
    senders: Counter
    kinds: Counter
    edited: int


def parse_export(path: str | Path, tz: str = DEFAULT_TZ) -> ExportSummary:
    messages = chatexport.parse_file(path, tz)
    return ExportSummary(messages, **chatexport.summarize(messages))


def import_export(source: str | Path, target: str | Path) -> Path:
    """Move an exported chat into `target`. iPhones export a .zip holding
    `_chat.txt` (plus media when exported with media); take the text only."""
    source, target = Path(source), Path(target)
    if not zipfile.is_zipfile(source):
        source.replace(target)
        return target
    try:
        with zipfile.ZipFile(source) as archive:
            texts = [i for i in archive.infolist()
                     if not i.is_dir() and i.filename.lower().endswith(".txt")]
            chats = [i for i in texts if Path(i.filename).name.startswith("_chat")] or texts
            if len(chats) != 1:
                raise ServiceError("the zip file does not contain exactly one chat text file")
            if chats[0].file_size > MAX_EXPORT_BYTES:
                raise ServiceError("the chat text inside the zip file is too large")
            with archive.open(chats[0]) as inner, open(target, "wb") as out:
                shutil.copyfileobj(inner, out)
    except zipfile.BadZipFile as exc:
        target.unlink(missing_ok=True)
        raise ServiceError("the zip file is damaged") from exc
    finally:
        source.unlink(missing_ok=True)
    return target


# merge

@dataclass(frozen=True)
class MergeRequest:
    messages: Sequence[ExportMessage]
    phone: str
    me: str
    tz: str = DEFAULT_TZ
    skip_media: bool = False
    min_gap_days: float = 30
    since: date | None = None        # local midnight in `tz`

    @property
    def since_ms(self) -> int | None:
        if self.since is None:
            return None
        start = datetime.combine(self.since, time(), tzinfo=ZoneInfo(self.tz))
        return int(start.timestamp() * 1000)

    def plan(self, db: sqlite3.Connection) -> merge.Plan:
        return merge.plan_merge(db, list(self.messages), self.phone, self.me,
                                self.skip_media, self.min_gap_days, self.since_ms)


@dataclass(frozen=True)
class MergeResult:
    plan: merge.Plan
    tz: str
    output: Path | None = None       # None for a dry run

    @property
    def from_me(self) -> int:
        return sum(r.from_me for r in self.plan.rows)

    @property
    def from_them(self) -> int:
        return len(self.plan.rows) - self.from_me

    @property
    def kinds(self) -> Counter:
        return Counter(r.kind for r in self.plan.rows)

    def added_by_month(self) -> Counter:
        """New messages per local "YYYY-MM"."""
        return _by_month((r.timestamp for r in self.plan.rows), self.tz)


def _by_month(timestamps_ms, tz: str) -> Counter:
    zone = ZoneInfo(tz)
    return Counter(merge.ms_to_utc(ms).astimezone(zone).strftime("%Y-%m") for ms in timestamps_ms)


@dataclass(frozen=True)
class ChatInfo:
    chat_id: int
    messages: int
    first_ms: int | None
    last_ms: int | None
    largest_gap: merge.Gap | None    # the longest silence after the first message

    def suggested_since(self, tz: str) -> date | None:
        """Day the largest gap starts. A sensible default for "only add
        messages after", since older history is already in the backup."""
        if self.largest_gap is None:
            return None
        return merge.ms_to_utc(self.largest_gap.start_ms).astimezone(ZoneInfo(tz)).date()


def describe_chat(database: str | Path, phone: str, min_gap_days: float = 30) -> ChatInfo:
    """Find the one-to-one chat for `phone`; raises MergeError (with a code) if
    there is none, more than one, or a LID twin with messages."""
    with closing(_connect_readonly(database)) as db:
        chat_id, _ = merge.find_chat(db, phone)
        count, first, last = db.execute(
            "SELECT count(*), min(timestamp), max(timestamp) FROM message"
            " WHERE chat_row_id = ? AND timestamp > 0", (chat_id,)).fetchone()
        gaps = merge.find_gaps(db, chat_id, min_gap_days)
    now_ms = int(clock.time() * 1000)
    # The open head (before the first message) is not a loss, so it is skipped.
    candidates = [g for g in gaps if g.start_ms > 0]
    largest = max(candidates, key=lambda g: min(g.end_ms, now_ms) - g.start_ms, default=None)
    return ChatInfo(chat_id, count, first, last, largest)


def chat_by_month(database: str | Path, chat_id: int, tz: str = DEFAULT_TZ) -> Counter:
    """Messages already in the backup per local "YYYY-MM"."""
    with closing(_connect_readonly(database)) as db:
        rows = db.execute("SELECT timestamp FROM message WHERE chat_row_id = ? AND timestamp > 0",
                          (chat_id,))
        return _by_month((ts for (ts,) in rows), tz)


def plan_merge(database: str | Path, request: MergeRequest) -> MergeResult:
    """Dry run: what `apply_merge` would insert. Nothing is written."""
    with closing(_connect_readonly(database)) as db:
        return MergeResult(request.plan(db), request.tz)


def apply_merge(database: str | Path, output: str | Path, request: MergeRequest) -> MergeResult:
    """Write a merged copy of `database` to `output`; the input is never modified."""
    output = Path(output)
    return MergeResult(merge.apply_merge(Path(database), output, request.plan), request.tz, output)


def _check_distinct_chats(db: sqlite3.Connection, requests: Sequence[MergeRequest]) -> None:
    seen: dict[int, str] = {}
    for request in requests:
        chat_id, _ = merge.find_chat(db, request.phone)
        if chat_id in seen:
            raise ServiceError(f"{seen[chat_id]} and {request.phone} are the same chat "
                               f"(#{chat_id}); add one export per chat")
        seen[chat_id] = request.phone


def plan_merge_chain(database: str | Path, requests: Sequence[MergeRequest]) -> list[MergeResult]:
    """Dry runs for several chats. Each chat's plan only depends on that
    chat's own rows, so all plans are made against the same input."""
    with closing(_connect_readonly(database)) as db:
        _check_distinct_chats(db, requests)
        return [MergeResult(request.plan(db), request.tz) for request in requests]


def apply_merge_chain(database: str | Path, requests: Sequence[MergeRequest],
                      output: str | Path, progress: BuildProgress | None = None) -> list[MergeResult]:
    """Merge several exports one after another into a single new database.

    Intermediate copies live next to `output` and are always removed; on
    failure no output is left behind.
    """
    if not requests:
        raise ServiceError("no exports to merge")
    output = Path(output)
    ensure_new_output(output)
    with closing(_connect_readonly(database)) as db:
        _check_distinct_chats(db, requests)

    results: list[MergeResult] = []
    source = Path(database)
    intermediates: list[Path] = []
    try:
        for step, request in enumerate(requests, start=1):
            if progress:
                progress("merge", step - 1, len(requests))
            if step == len(requests):
                target = output
            else:
                target = output.with_name(f"{output.name}.step{step}")
                intermediates.append(target)
            results.append(apply_merge(source, target, request))
            source = target
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    finally:
        for path in intermediates:
            path.unlink(missing_ok=True)
    return results


# encrypt

@dataclass(frozen=True)
class EncryptResult:
    output: Path
    size: int


def encrypt_backup(database: str | Path, template: str | Path, output: str | Path,
                   root_key: bytes, *, overwrite: bool = False) -> EncryptResult:
    """Encrypt `database` with the header of `template` and verify the result
    decrypts back to the same bytes before writing it."""
    output = Path(output)
    ensure_new_output(output, overwrite)
    plain = Path(database).read_bytes()
    if not plain.startswith(crypt15.SQLITE_MAGIC):
        raise NotSqliteError(database)
    template_data = Path(template).read_bytes()

    # Prove the key is the one the template was made with before using it.
    crypt15.decrypt(template_data, root_key)
    sealed = crypt15.encrypt(plain, root_key, template_data)
    # Never hand the phone a file we cannot read back.
    if crypt15.decrypt(sealed, root_key)[0] != plain:
        raise RoundTripError("round-trip check failed; nothing written")
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_new(output, sealed)
    return EncryptResult(output, len(sealed))


@dataclass(frozen=True)
class BuildResult:
    merges: list[MergeResult]
    merged_database: Path
    backup: EncryptResult


def build_backup(database: str | Path, requests: Sequence[MergeRequest], template: str | Path,
                 *, merged_database: str | Path, output: str | Path, root_key: bytes,
                 progress: BuildProgress | None = None) -> BuildResult:
    """Run the merge chain, then encrypt the merged database for the phone."""
    ensure_new_output(output)
    merges = apply_merge_chain(database, requests, merged_database, progress)
    if progress:
        progress("encrypt", 0, 1)
    backup = encrypt_backup(merged_database, template, output, root_key)
    return BuildResult(merges, Path(merged_database), backup)


# repair

def plan_repair(database: str | Path, baseline: str | Path) -> repair.RepairPlan:
    """Find messages written after a restore that are hidden mid-chat.

    `baseline` is the merged database the phone was restored from.
    """
    with closing(_connect_readonly(baseline)) as base, closing(_connect_readonly(database)) as current:
        return repair.plan_repair(current, repair.baseline_max_id(base, current))


def apply_repair(database: str | Path, plan: repair.RepairPlan, output: str | Path) -> Path:
    """Write a repaired copy of `database` to `output`; the input is never modified."""
    output = Path(output)
    ensure_new_output(output)
    shutil.copyfile(database, output)
    try:
        with closing(sqlite3.connect(output)) as db:
            repair.apply_repair(db, plan)
            _quick_check(db)
    except BaseException as exc:
        output.unlink(missing_ok=True)
        if isinstance(exc, sqlite3.Error):
            raise repair.RepairError(str(exc)) from exc
        raise
    return output


# workspace

def default_workspace_root() -> Path:
    return Path.home() / "warescue-workspace"


def create_workspace(root: str | Path | None = None) -> Path:
    """Create a fresh, marked folder for uploaded and decrypted files."""
    root = Path(root) if root is not None else default_workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for attempt in range(100):
        path = root / (stamp if attempt == 0 else f"{stamp}-{attempt}")
        try:
            path.mkdir()
        except FileExistsError:
            continue
        (path / WORKSPACE_MARKER).write_text("created by warescue\n", encoding="utf-8")
        return path
    raise ServiceError(f"could not create a new workspace under {root}")


def cleanup_workspace(path: str | Path) -> int:
    """Delete a workspace created by `create_workspace`; returns the number
    of files removed. Folders without the marker are refused, so a wrong
    path can never wipe unrelated files."""
    path = Path(path)
    if not (path / WORKSPACE_MARKER).is_file():
        raise ServiceError(f"{path} is not a warescue workspace; refusing to delete it")
    removed = sum(1 for p in path.rglob("*") if p.is_file() and p.name != WORKSPACE_MARKER)
    shutil.rmtree(path)
    return removed
