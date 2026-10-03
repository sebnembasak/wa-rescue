import sqlite3

import pytest

from warescue import chatexport, merge, repair
from test_merge import EXPORT, add, db_path, ms  # noqa: F401


def merged(db_path, tmp_path):
    msgs = chatexport.parse_lines(EXPORT.splitlines(keepends=True))
    out = tmp_path / "merged.db"
    merge.apply_merge(db_path, out, lambda db: merge.plan_merge(db, msgs, "5550001122", "Ben"))
    return out


def phone_writes(db, chat, ts, text):
    """Simulate WhatsApp: AUTOINCREMENT _id, sort_id = _id."""
    cur = db.execute("INSERT INTO message (chat_row_id, from_me, key_id, timestamp, message_type,"
                     " text_data) VALUES (?, 1, 'PHONE', ?, 0, ?)", (chat, ts, text))
    db.execute("UPDATE message SET sort_id = _id WHERE _id = ?", (cur.lastrowid,))


def last_text(db, chat=1):
    return db.execute("SELECT text_data FROM message WHERE chat_row_id = ? ORDER BY sort_id DESC LIMIT 1",
                      (chat,)).fetchone()[0]


def test_merge_now_bumps_counter(db_path, tmp_path):
    db = sqlite3.connect(merged(db_path, tmp_path))
    phone_writes(db, 1, ms(2026, 10, 3, 12, 0), "yeni")
    assert last_text(db) == "yeni"


def test_repair_moves_hidden_messages(db_path, tmp_path):
    base = merged(db_path, tmp_path)
    current = tmp_path / "current.db"
    current.write_bytes(base.read_bytes())
    db = sqlite3.connect(current)
    db.execute("UPDATE sqlite_sequence SET seq = (SELECT max(_id) FROM message) WHERE name = 'message'")
    phone_writes(db, 1, ms(2026, 10, 3, 12, 0), "bana gelen")
    phone_writes(db, 1, ms(2026, 10, 3, 12, 1), "benim attığım")
    phone_writes(db, 2, ms(2026, 10, 3, 12, 2), "baska sohbet")
    db.commit()
    assert last_text(db) != "benim attığım"

    with sqlite3.connect(base) as b:
        plan = repair.plan_repair(db, repair.baseline_max_id(b, db))
    assert set(plan.displaced) == {1, 2}
    repair.apply_repair(db, plan)

    order = [t for (t,) in db.execute("SELECT text_data FROM message WHERE chat_row_id = 1 ORDER BY sort_id")]
    assert order[-2:] == ["bana gelen", "benim attığım"]
    phone_writes(db, 1, ms(2026, 10, 3, 13, 0), "sonraki")
    assert last_text(db) == "sonraki"
    assert db.execute("SELECT count(*) - count(DISTINCT sort_id) FROM message").fetchone()[0] == 0


def test_baseline_mismatch_is_refused(db_path, tmp_path):
    base = sqlite3.connect(merged(db_path, tmp_path))
    other = sqlite3.connect(db_path)
    with pytest.raises(repair.RepairError):
        repair.baseline_max_id(base, other)


def test_untouched_chat_is_left_alone(db_path, tmp_path):
    base = merged(db_path, tmp_path)
    current = tmp_path / "current.db"
    current.write_bytes(base.read_bytes())
    db = sqlite3.connect(current)
    phone_writes(db, 2, ms(2026, 10, 3, 12, 2), "baska sohbet")
    db.commit()
    with sqlite3.connect(base) as b:
        plan = repair.plan_repair(db, repair.baseline_max_id(b, db))
    assert plan.displaced == {} and not plan.needs_sequence_bump
