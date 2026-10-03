"""Fill a gap in a one-to-one chat of a decrypted msgstore.db with messages
from a WhatsApp text export.

Strategy (learned from a real WhatsApp 2.26 database):
  * the chat is found through its phone-number jid a separate LID chat is
    refused for now so we never split history across two chats
  * gaps are every silence of at least --min-gap-days (default 30) between
    two messages. Short silences are left alone on purpose: inside history
    that was already backed up, a message present only in the export is
    usually one you deleted "for me", not one that was lost
    plus the open head (before the first message) and the open tail (after
    the last one). The tail matters: if nobody wrote after the restore, the
    missing years are not *between* two messages at all
  * only export messages that fall inside a gap are inserted. Near gap
    edges, messages whose (from_me, text) already exist are skipped
  * WhatsApp orders a chat by sort_id. New rows are merged into the existing
    order by timestamp, and everything from the first new row onward gets
    fresh sort_ids above the table maximum. Existing relative order is kept
  * the input database is never modified; we work on a copy
"""
from __future__ import annotations

import shutil
from bisect import bisect_left
import sqlite3
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .chatexport import ExportMessage

MINUTE_MS = 60_000
EDGE_WINDOW_MS = 10 * MINUTE_MS
STATUS_OUTGOING_READ = 13
STATUS_INCOMING = 0


class MergeError(Exception):
    """`code` names the failure for callers that explain it in their own words."""

    def __init__(self, message: str, code: str = "merge_failed") -> None:
        super().__init__(message)
        self.code = code


FAR_FUTURE = 2**62


@dataclass
class Gap:
    start_ms: int           # last message before the gap (0 = open head)
    end_ms: int             # first message after the gap (FAR_FUTURE = open tail)
    inserted: int = 0


@dataclass
class NewRow:
    from_me: int
    timestamp: int
    text: str
    kind: str


@dataclass
class Plan:
    chat_id: int
    jid_id: int
    gaps: list[Gap]
    rows: list[NewRow]
    skipped: Counter = field(default_factory=Counter)

    @property
    def filled(self) -> list[Gap]:
        return [g for g in self.gaps if g.inserted]


def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# --- discovery --------------------------------------------------------------

def find_chat(db: sqlite3.Connection, phone_suffix: str) -> tuple[int, int]:
    digits = "".join(ch for ch in phone_suffix if ch.isdigit())
    if len(digits) < 7:
        raise MergeError("give at least the last 7 digits of the phone number", "phone_too_short")
    rows = db.execute(
        """SELECT c._id, j._id FROM jid j JOIN chat c ON c.jid_row_id = j._id
            WHERE j.server = 's.whatsapp.net' AND j.user LIKE '%' || ?""",
        (digits,),
    ).fetchall()
    if len(rows) != 1:
        raise MergeError(f"expected exactly one chat for ...{digits}, found {len(rows)}",
                         "chat_not_found" if not rows else "chat_ambiguous")
    chat_id, jid_id = rows[0]

    lid_chat = db.execute(
        """SELECT c._id, (SELECT count(*) FROM message m WHERE m.chat_row_id = c._id)
             FROM jid_map jm JOIN chat c ON c.jid_row_id = jm.lid_row_id
            WHERE jm.jid_row_id = ?""",
        (jid_id,),
    ).fetchone()
    if lid_chat and lid_chat[1]:
        raise MergeError(f"contact also has a LID chat (#{lid_chat[0]}) with messages; not supported yet",
                         "lid_chat")
    return chat_id, jid_id


def find_gaps(db: sqlite3.Connection, chat_id: int, min_gap_days: float = 30) -> list[Gap]:
    ts = [t for (t,) in db.execute(
        "SELECT timestamp FROM message WHERE chat_row_id = ? AND timestamp > 0 ORDER BY timestamp",
        (chat_id,))]
    if not ts:
        return [Gap(0, FAR_FUTURE)]
    min_gap = int(min_gap_days * 86_400_000)
    inner = [Gap(a, b) for a, b in zip(ts, ts[1:]) if b - a >= min_gap]
    return [Gap(0, ts[0]), *inner, Gap(ts[-1], FAR_FUTURE)]


# planning

def render(msg: ExportMessage, skip_media: bool) -> str | None:
    if msg.kind == "system":
        return None
    if msg.kind == "media_omitted":
        return None if skip_media else f"📎 {msg.media_label or 'Medya'} (yedekte yok)"
    if msg.kind in ("media", "contact"):
        icon = "👤" if msg.kind == "contact" else "📎"
        head = f"{icon} {msg.media_file}"
        return f"{head}\n{msg.text}" if msg.text else head
    if msg.kind == "deleted":
        return "🚫 Bu mesaj silindi"
    return msg.text or None


def _spread_within_minute(messages: list[ExportMessage]) -> list[int]:
    """Exports only have minutes. Spread messages of the same minute across
    whole seconds so their order survives."""
    by_minute: dict[int, list[int]] = defaultdict(list)
    for i, m in enumerate(messages):
        by_minute[int(m.time.timestamp() * 1000)].append(i)
    out = [0] * len(messages)
    for minute_ms, idxs in by_minute.items():
        for k, i in enumerate(idxs):
            out[i] = minute_ms + (k * 60 // len(idxs)) * 1000
    return out


def _fit_into_gap(rows: list[NewRow], gap: Gap) -> None:
    """Make timestamps strictly increasing and strictly inside (start, end).

    Export minutes can overlap a gap edge, e.g. three messages at 14:05 when
    the gap ends at 14:05:10. A forward pass pushes values up past the start
    and past their predecessor. A backward pass pulls values down below the
    end and below their successor. Order is never changed.
    """
    lo, hi = gap.start_ms + 1, gap.end_ms - 1
    if hi - lo + 1 < len(rows):
        raise MergeError("not enough room inside a gap for the new timestamps", "no_room")
    prev = lo - 1
    for r in rows:
        r.timestamp = prev = max(r.timestamp, prev + 1)
    nxt = hi + 1
    for r in reversed(rows):
        r.timestamp = nxt = min(r.timestamp, nxt - 1)


@dataclass
class EdgeIndex:
    texts: set[tuple[int, str]]          # (from_me, text) of text messages near the edges
    blank_minutes: set[tuple[int, int]]  # (from_me, minute) of media/system rows (no text)

    def contains(self, from_me: int, text: str, kind: str, minute: int) -> bool:
        if (from_me, text) in self.texts:
            return True
        # A photo or sticker has no text in the DB but a placeholder in the
        # export, match those by sender and minute instead.
        return kind != "text" and (from_me, minute) in self.blank_minutes


def edge_index(db: sqlite3.Connection, chat_id: int, gap: Gap) -> EdgeIndex:
    end = min(gap.end_ms, FAR_FUTURE - EDGE_WINDOW_MS)
    rows = db.execute(
        """SELECT from_me, text_data, timestamp FROM message
            WHERE chat_row_id = ?
              AND (timestamp BETWEEN ? AND ? OR timestamp BETWEEN ? AND ?)""",
        (chat_id, gap.start_ms - EDGE_WINDOW_MS, gap.start_ms, end, end + EDGE_WINDOW_MS),
    ).fetchall()
    texts = {(fm, txt) for fm, txt, _ in rows if txt is not None}
    blanks = {(fm, ts - ts % MINUTE_MS) for fm, txt, ts in rows if txt is None}
    return EdgeIndex(texts, blanks)


def plan_merge(db, messages: list[ExportMessage], phone: str, me: str,
               skip_media: bool = False, min_gap_days: float = 30,
               since_ms: int | None = None) -> Plan:
    chat_id, jid_id = find_chat(db, phone)
    gaps = find_gaps(db, chat_id, min_gap_days)

    senders = {m.sender for m in messages if m.sender is not None}
    if me not in senders:
        raise MergeError(f"--me {me!r} is not a sender in the export", "me_not_sender")
    if len(senders) > 2:
        raise MergeError("export has more than two senders; group chats are not supported", "group_chat")

    plan = Plan(chat_id, jid_id, gaps, [])
    starts = [g.start_ms for g in gaps]
    edges: dict[int, EdgeIndex] = {}
    times = _spread_within_minute(messages)
    per_gap: dict[int, list[NewRow]] = {}

    passed: set[int] = set()   # gaps whose boundary DB message we already met in the export

    for msg, ts in zip(messages, times):
        minute = int(msg.time.timestamp() * 1000)
        if since_ms is not None and minute < since_ms:
            plan.skipped["before_since"] += 1
            continue
        # last gap that starts before this minute ends
        gi = bisect_left(starts, minute + MINUTE_MS) - 1
        if gi < 0 or not minute < gaps[gi].end_ms:
            plan.skipped["already_in_db"] += 1
            continue
        # The minute may straddle the DB message that ends gap gi-1 and
        # starts gap gi. Until that message shows up in the export, we are
        # still before it, i.e. in gap gi-1.
        use = gi
        if (minute <= gaps[gi].start_ms and gi not in passed and gi > 0
                and minute < gaps[gi - 1].end_ms):
            use = gi - 1
        gap = gaps[use]
        text = render(msg, skip_media)
        if text is None:
            plan.skipped[msg.kind] += 1
            continue
        from_me = int(msg.sender == me)
        on_edge = minute <= gap.start_ms or minute + MINUTE_MS >= gap.end_ms - EDGE_WINDOW_MS
        if on_edge:
            if use not in edges:
                edges[use] = edge_index(db, chat_id, gap)
            if edges[use].contains(from_me, text, msg.kind, minute):
                plan.skipped["duplicate_at_edge"] += 1
                if minute <= gaps[gi].start_ms:
                    passed.add(gi)
                continue
        gap.inserted += 1
        per_gap.setdefault(use, []).append(NewRow(from_me, ts, text, msg.kind))

    for gi, rows in per_gap.items():
        _fit_into_gap(rows, gaps[gi])
        plan.rows.extend(rows)
    plan.rows.sort(key=lambda r: r.timestamp)
    return plan


# applying

def _insert_rows(db, plan: Plan, first_id: int) -> None:
    def values():
        for offset, r in enumerate(plan.rows):
            rid = first_id + offset
            if r.from_me:
                received, receipt, status = 0, r.timestamp, STATUS_OUTGOING_READ
            else:
                received, receipt, status = r.timestamp + 500, -1, STATUS_INCOMING
            yield (rid, plan.chat_id, r.from_me, uuid.uuid4().hex.upper(), 0, status,
                   0, 0, 0, 0, r.timestamp, received, receipt, 0, r.text, 0, 0, rid, 0, 0)

    db.executemany(
        """INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, status,
               broadcast, recipient_count, origination_flags, origin, timestamp,
               received_timestamp, receipt_server_timestamp, message_type, text_data,
               starred, lookup_tables, sort_id, message_add_on_flags, view_mode)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        values(),
    )


def _resort_chat(db, chat_id: int, new_rows: list[tuple[int, int]]) -> None:
    """Merge new (id, ts) rows into the chat's existing sort order by
    timestamp and renumber sort_id from the first new row onward."""
    first_new_id = new_rows[0][0]
    existing = db.execute(
        "SELECT _id, timestamp FROM message WHERE chat_row_id = ? AND _id < ? ORDER BY sort_id, _id",
        (chat_id, first_new_id)).fetchall()

    order: list[int] = []
    first_new_pos = None
    j = 0
    for row_id, ts in existing:
        while j < len(new_rows) and ts and ts > 0 and new_rows[j][1] < ts:
            first_new_pos = len(order) if first_new_pos is None else first_new_pos
            order.append(new_rows[j][0])
            j += 1
        order.append(row_id)
    if j < len(new_rows):
        first_new_pos = len(order) if first_new_pos is None else first_new_pos
        order.extend(rid for rid, _ in new_rows[j:])

    (max_sort,) = db.execute("SELECT max(sort_id) FROM message").fetchone()
    db.executemany("UPDATE message SET sort_id = ? WHERE _id = ?",
                   ((max_sort + 1 + k, rid) for k, rid in enumerate(order[first_new_pos:])))


def _fix_chat_pointers(db, chat_id: int, newest_ts: int) -> None:
    last_id, last_sort = db.execute(
        "SELECT _id, sort_id FROM message WHERE chat_row_id = ? ORDER BY sort_id DESC LIMIT 1",
        (chat_id,)).fetchone()
    shown = db.execute(
        """SELECT _id, sort_id FROM message WHERE chat_row_id = ? AND text_data IS NOT NULL
            ORDER BY sort_id DESC LIMIT 1""", (chat_id,)).fetchone() or (last_id, last_sort)
    row = db.execute("SELECT sort_timestamp FROM chat WHERE _id = ?", (chat_id,)).fetchone()
    if row is None:
        return
    (sort_ts,) = row
    db.execute(
        """UPDATE chat SET display_message_row_id = ?, display_message_sort_id = ?,
                  last_message_row_id = ?, last_message_sort_id = ?,
                  last_read_message_row_id = ?, last_read_message_sort_id = ?,
                  sort_timestamp = ? WHERE _id = ?""",
        (shown[0], shown[1], last_id, last_sort, last_id, last_sort,
         max(sort_ts or 0, newest_ts), chat_id),
    )


def apply_merge(source: Path, target: Path, plan_fn) -> Plan:
    if target.exists():
        raise MergeError(f"{target} exists; refusing to overwrite", "output_exists")
    shutil.copyfile(source, target)
    db = sqlite3.connect(target)
    try:
        plan = plan_fn(db)
        if not plan.rows:
            raise MergeError("nothing to insert", "nothing_to_insert")
        with db:  # single transaction, all or nothing
            (max_id,) = db.execute("SELECT max(_id) FROM message").fetchone()
            first_id = max_id + 1
            _insert_rows(db, plan, first_id)
            new_rows = [(first_id + i, r.timestamp) for i, r in enumerate(plan.rows)]
            _resort_chat(db, plan.chat_id, new_rows)
            _fix_chat_pointers(db, plan.chat_id, plan.rows[-1].timestamp)
            # WhatsApp gives new messages sort_id = _id: push the AUTOINCREMENT
            # counter past our sort_ids or later messages sort mid-history.
            (max_sort,) = db.execute("SELECT max(sort_id) FROM message").fetchone()
            db.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'message' AND seq < ?",
                       (max_sort, max_sort))

        (check,) = db.execute("PRAGMA quick_check").fetchone()
        if check != "ok":
            raise MergeError(f"sqlite quick_check failed: {check}", "quick_check_failed")
    except Exception:
        db.close()
        target.unlink(missing_ok=True)
        raise
    db.close()
    return plan
