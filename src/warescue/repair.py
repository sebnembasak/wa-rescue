"""Fix chats where messages written after a restore are hidden mid-history.

WhatsApp gives a new message sort_id = _id. A merge that hands out sort_ids
above the table's highest _id therefore makes every later message sort
*before* the merged ones. It shows in the chat list but not at the bottom of
the conversation.

Repair: rows created by the phone after the merge (_id above the baseline's
maximum) are moved to the end of their chat in timestamp order when they are
hidden mid-history or share a sort_id with an older row, and the
AUTOINCREMENT counter is raised past the highest sort_id so future messages
land at the end on their own.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .merge import MergeError, _fix_chat_pointers


class RepairError(MergeError):
    pass


def baseline_max_id(baseline: sqlite3.Connection, current: sqlite3.Connection) -> int:
    """Highest _id of the merged database we restored from, after checking
    that the phone kept row ids unchanged (otherwise "_id > baseline" would
    not mean "written after the restore")."""
    top = baseline.execute("SELECT _id, key_id FROM message ORDER BY _id DESC LIMIT 5").fetchall()
    if not top:
        raise RepairError("baseline database has no messages", "baseline_empty")
    same = sum(
        1 for rid, key in top
        if current.execute("SELECT 1 FROM message WHERE _id = ? AND key_id = ?", (rid, key)).fetchone()
    )
    if same < 3:
        raise RepairError("row ids differ between baseline and current backup; "
                          "the baseline is not the database this phone was restored from",
                          "baseline_mismatch")
    return top[0][0]


@dataclass
class RepairPlan:
    baseline_max_id: int
    displaced: dict[int, list[int]] = field(default_factory=dict)   # chat -> row ids, chronological
    sequence_before: int = 0
    max_sort: int = 0

    @property
    def needs_sequence_bump(self) -> bool:
        return self.sequence_before < self.max_sort


def message_sequence(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'message'").fetchone()
    return row[0] if row else 0


def bump_sequence(db: sqlite3.Connection) -> None:
    """Make the next AUTOINCREMENT _id (and so WhatsApp's next sort_id)
    larger than every sort_id in the table."""
    (max_sort,) = db.execute("SELECT max(sort_id) FROM message").fetchone()
    db.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'message' AND seq < ?",
               (max_sort, max_sort))


def plan_repair(db: sqlite3.Connection, baseline_max_id: int) -> RepairPlan:
    plan = RepairPlan(baseline_max_id)
    plan.sequence_before = message_sequence(db)
    (plan.max_sort,) = db.execute("SELECT max(sort_id) FROM message").fetchone()

    new_rows = db.execute(
        """SELECT chat_row_id, _id, sort_id FROM message WHERE _id > ?
            ORDER BY chat_row_id, timestamp, _id""", (baseline_max_id,)).fetchall()
    by_chat: dict[int, list[tuple[int, int]]] = {}
    for chat, rid, sort in new_rows:
        by_chat.setdefault(chat, []).append((rid, sort))

    # sort_ids the phone handed out that also belong to an older (merged) row
    post_sorts = {sort for _, _, sort in new_rows}
    clashing: set[int] = set()
    if post_sorts:
        for (sort,) in db.execute("SELECT sort_id FROM message WHERE _id <= ? AND sort_id >= ?",
                                  (baseline_max_id, min(post_sorts))):
            if sort in post_sorts:
                clashing.add(sort)

    for chat, rows in by_chat.items():
        (old_max,) = db.execute(
            "SELECT max(sort_id) FROM message WHERE chat_row_id = ? AND _id <= ?",
            (chat, baseline_max_id)).fetchone()
        hidden = old_max is not None and min(sort for _, sort in rows) < old_max
        if hidden or any(sort in clashing for _, sort in rows):
            plan.displaced[chat] = [rid for rid, _ in rows]   # move the chat's whole post-restore tail
    return plan


def apply_repair(db: sqlite3.Connection, plan: RepairPlan) -> None:
    with db:
        next_sort = plan.max_sort + 1
        for chat, ids in plan.displaced.items():
            for rid in ids:
                db.execute("UPDATE message SET sort_id = ? WHERE _id = ?", (next_sort, rid))
                next_sort += 1
            (newest,) = db.execute("SELECT max(timestamp) FROM message WHERE chat_row_id = ?",
                                   (chat,)).fetchone()
            _fix_chat_pointers(db, chat, newest)
        bump_sequence(db)
