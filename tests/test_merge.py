import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from warescue import chatexport, merge

TZ = ZoneInfo("Europe/Istanbul")
SCHEMA = """
CREATE TABLE jid (_id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, server TEXT NOT NULL,
  agent INTEGER, type INTEGER, raw_string TEXT, device INTEGER);
CREATE TABLE jid_map (lid_row_id INTEGER PRIMARY KEY NOT NULL, jid_row_id INTEGER NOT NULL, sort_id INTEGER);
CREATE TABLE chat (_id INTEGER PRIMARY KEY AUTOINCREMENT, jid_row_id INTEGER UNIQUE,
  display_message_row_id INTEGER, last_message_row_id INTEGER, last_read_message_row_id INTEGER,
  sort_timestamp INTEGER, last_message_sort_id INTEGER, display_message_sort_id INTEGER,
  last_read_message_sort_id INTEGER);
CREATE TABLE message (_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_row_id INTEGER NOT NULL,
  from_me INTEGER NOT NULL, key_id TEXT NOT NULL, sender_jid_row_id INTEGER, status INTEGER,
  broadcast INTEGER, recipient_count INTEGER, participant_hash TEXT, origination_flags INTEGER,
  origin INTEGER, timestamp INTEGER, received_timestamp INTEGER, receipt_server_timestamp INTEGER,
  message_type INTEGER, text_data TEXT, starred INTEGER, lookup_tables INTEGER,
  sort_id INTEGER NOT NULL DEFAULT 0, message_add_on_flags INTEGER, view_mode INTEGER,
  translated_text TEXT, view_replies_thread_id INTEGER, server_sts INTEGER);
"""


def ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=TZ).timestamp() * 1000)


def add(db, _id, chat, from_me, ts, text, mtype=0):
    db.execute("INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type,"
               " text_data, sort_id) VALUES (?,?,?,?,?,?,?,?)",
               (_id, chat, from_me, f"K{_id}", ts, mtype, text, _id))


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "msgstore.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO jid (_id, user, server, type) VALUES (5, '905550001122', 's.whatsapp.net', 0)")
    db.execute("INSERT INTO jid (_id, user, server, type) VALUES (6, '123456789', 'lid', 0)")
    db.execute("INSERT INTO jid_map VALUES (6, 5, 1)")
    add(db, 100, 1, 1, ms(2024, 10, 7, 22, 50), "eski 1")
    add(db, 101, 1, 0, ms(2024, 10, 7, 22, 52, 55), "eski son")
    add(db, 150, 2, 0, ms(2025, 1, 1, 0, 0), "baska sohbet")
    add(db, 200, 1, 1, ms(2026, 10, 2, 16, 24), None, mtype=7)
    db.execute("INSERT INTO chat VALUES (1, 5, 101, 200, 200, ?, 200, 101, 200)", (ms(2024, 10, 7, 22, 52),))
    db.commit()
    db.close()
    return path


EXPORT = """7.10.2024 22:50 - Ben: eski 1
7.10.2024 22:52 - Kişi 1: eski son
7.10.2024 22:52 - Kişi 1: yeni ama aynı dakika
8.10.2024 09:00 - Ben: günaydın
8.10.2024 09:00 - Ben: ikinci
1.5.2025 12:00 - Kişi 1: <Medya dahil edilmedi>
1.5.2025 12:01 - Kişi 1: Bu mesaj silindi
2.10.2026 16:09 - Ben: son
"""


def messages():
    return chatexport.parse_lines(EXPORT.splitlines(keepends=True))


def plan(db_path, **kw):
    with sqlite3.connect(db_path) as db:
        return merge.plan_merge(db, messages(), "5550001122", "Ben", **kw)


def chat_texts(db, chat=1):
    return [t for (t,) in db.execute(
        "SELECT coalesce(text_data, '<system>') FROM message WHERE chat_row_id = ? ORDER BY sort_id",
        (chat,))]


def test_plan_fills_only_the_gap(db_path):
    p = plan(db_path)
    assert p.chat_id == 1
    assert [r.text for r in p.rows] == [
        "yeni ama aynı dakika", "günaydın", "ikinci",
        "📎 Medya (yedekte yok)", "🚫 Bu mesaj silindi", "son"]
    assert p.skipped["duplicate_at_edge"] == 1
    assert p.skipped["already_in_db"] == 1
    assert len(p.filled) == 1
    ts = [r.timestamp for r in p.rows]
    assert ts == sorted(set(ts)) and ts[0] > p.filled[0].start_ms and ts[-1] < p.filled[0].end_ms


def test_skip_media(db_path):
    assert "📎 Medya (yedekte yok)" not in [r.text for r in plan(db_path, skip_media=True).rows]


def apply(db_path, tmp_path, export=None):
    msgs = chatexport.parse_lines((export or EXPORT).splitlines(keepends=True))
    out = tmp_path / "merged.db"
    merge.apply_merge(db_path, out, lambda db: merge.plan_merge(db, msgs, "5550001122", "Ben"))
    return sqlite3.connect(out)


def assert_pointers_consistent(db, chat=1):
    last_id, last_sort, disp_id, disp_sort = db.execute(
        "SELECT last_message_row_id, last_message_sort_id, display_message_row_id,"
        " display_message_sort_id FROM chat WHERE _id = ?", (chat,)).fetchone()
    top = db.execute("SELECT _id, sort_id FROM message WHERE chat_row_id = ? ORDER BY sort_id DESC LIMIT 1",
                     (chat,)).fetchone()
    assert (last_id, last_sort) == top
    assert db.execute("SELECT sort_id FROM message WHERE _id = ?", (disp_id,)).fetchone()[0] == disp_sort
    (dupes,) = db.execute("SELECT count(*) - count(DISTINCT sort_id) FROM message").fetchone()
    assert dupes == 0


def test_apply_keeps_order_and_source(db_path, tmp_path):
    before = db_path.read_bytes()
    db = apply(db_path, tmp_path)
    assert db_path.read_bytes() == before
    assert chat_texts(db) == ["eski 1", "eski son", "yeni ama aynı dakika", "günaydın", "ikinci",
                              "📎 Medya (yedekte yok)", "🚫 Bu mesaj silindi", "son", "<system>"]
    assert chat_texts(db, chat=2) == ["baska sohbet"]
    assert_pointers_consistent(db)
    disp = db.execute("SELECT text_data FROM message WHERE _id ="
                      " (SELECT display_message_row_id FROM chat WHERE _id = 1)").fetchone()[0]
    assert disp == "son"
    keys = [k for (k,) in db.execute("SELECT key_id FROM message WHERE _id > 200")]
    assert len(set(keys)) == 6 and all(len(k) == 32 for k in keys)


def test_open_tail_is_filled_when_nothing_came_after_restore(db_path, tmp_path):
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM message WHERE _id = 200")
    db = apply(db_path, tmp_path)
    assert chat_texts(db)[-1] == "son"
    assert_pointers_consistent(db)


def test_multiple_gaps_including_head(db_path, tmp_path):
    export = "1.1.2024 10:00 - Kişi 1: çok eski\n" + EXPORT
    db = apply(db_path, tmp_path, export)
    texts = chat_texts(db)
    assert texts[0] == "çok eski" and texts[1] == "eski 1"
    assert texts[-2:] == ["son", "<system>"]
    assert_pointers_consistent(db)


def test_refuses_existing_output(db_path, tmp_path):
    out = tmp_path / "merged.db"
    out.write_text("x")
    with pytest.raises(merge.MergeError):
        merge.apply_merge(db_path, out, lambda db: None)


def test_rejects_wrong_me(db_path):
    with sqlite3.connect(db_path) as db, pytest.raises(merge.MergeError):
        merge.plan_merge(db, messages(), "5550001122", "Kişi 3")


def test_fit_into_gap_squeezes_edge_minutes():
    gap = merge.Gap(start_ms=1_000_000, end_ms=1_010_000)
    rows = [merge.NewRow(0, ts, "x", "text") for ts in (990_000, 1_005_000, 1_020_000, 1_040_000)]
    merge._fit_into_gap(rows, gap)
    ts = [r.timestamp for r in rows]
    assert ts == sorted(set(ts)) and ts[0] > gap.start_ms and ts[-1] < gap.end_ms


def test_three_messages_in_minute_where_gap_ends(tmp_path):
    path = tmp_path / "m.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO jid (_id, user, server, type) VALUES (5, '905550001122', 's.whatsapp.net', 0)")
    add(db, 1, 1, 1, ms(2025, 1, 1, 10, 0), "a")
    add(db, 2, 1, 0, ms(2025, 1, 5, 14, 5, 10), "b")
    db.execute("INSERT INTO chat VALUES (1, 5, 2, 2, 2, 0, 2, 2, 2)")
    db.commit()
    export = ("1.1.2025 10:00 - Ben: a\n3.1.2025 9:00 - Kişi 1: ara\n"
              "5.1.2025 14:05 - Ben: x\n5.1.2025 14:05 - Ben: y\n5.1.2025 14:05 - Kişi 1: z\n"
              "5.1.2025 14:05 - Kişi 1: b\n")
    msgs = chatexport.parse_lines(export.splitlines(keepends=True))
    p = merge.plan_merge(db, msgs, "5550001122", "Ben", min_gap_days=2)
    assert [r.text for r in p.rows] == ["ara", "x", "y", "z"]
    assert all(r.timestamp < ms(2025, 1, 5, 14, 5, 10) for r in p.rows)


def test_media_boundary_is_not_duplicated(tmp_path):
    path = tmp_path / "m.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO jid (_id, user, server, type) VALUES (5, '905550001122', 's.whatsapp.net', 0)")
    add(db, 1, 1, 1, ms(2025, 1, 1, 10, 0), "a")
    add(db, 2, 1, 0, ms(2025, 1, 5, 14, 5, 10), None, mtype=1)
    db.execute("INSERT INTO chat VALUES (1, 5, 2, 2, 2, 0, 2, 2, 2)")
    db.commit()
    export = ("1.1.2025 10:00 - Ben: a\n3.1.2025 9:00 - Kişi 1: ara\n"
              "5.1.2025 14:05 - Kişi 1: <Medya dahil edilmedi>\n")
    msgs = chatexport.parse_lines(export.splitlines(keepends=True))
    p = merge.plan_merge(db, msgs, "5550001122", "Ben", min_gap_days=2)
    assert [r.text for r in p.rows] == ["ara"]
    assert p.skipped["duplicate_at_edge"] == 2


def test_short_silences_are_left_alone_by_default(tmp_path):
    path = tmp_path / "m.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO jid (_id, user, server, type) VALUES (5, '905550001122', 's.whatsapp.net', 0)")
    add(db, 1, 1, 1, ms(2025, 1, 1, 10, 0), "a")
    add(db, 2, 1, 0, ms(2025, 1, 10, 10, 0), "b")
    db.execute("INSERT INTO chat VALUES (1, 5, 2, 2, 2, 0, 2, 2, 2)")
    db.commit()
    export = "1.1.2025 10:00 - Ben: a\n5.1.2025 9:00 - Ben: benden sildim\n10.1.2025 10:00 - Kişi 1: b\n"
    msgs = chatexport.parse_lines(export.splitlines(keepends=True))
    p = merge.plan_merge(db, msgs, "5550001122", "Ben")
    assert "benden sildim" not in [r.text for r in p.rows]


def test_since_excludes_older_history(db_path):
    export = "1.1.2024 10:00 - Kişi 1: çok eski\n" + EXPORT
    msgs = chatexport.parse_lines(export.splitlines(keepends=True))
    with sqlite3.connect(db_path) as db:
        p = merge.plan_merge(db, msgs, "5550001122", "Ben", since_ms=ms(2024, 10, 1, 0, 0))
    assert "çok eski" not in [r.text for r in p.rows]
    assert p.skipped["before_since"] == 1
    assert "günaydın" in [r.text for r in p.rows]


def test_fresh_install_with_one_new_message(tmp_path):
    """No old backup at all: the user reinstalled, sent one message so the
    chat exists, and backed up. The whole history goes into the open head."""
    path = tmp_path / "m.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO jid (_id, user, server, type) VALUES (5, '905550001122', 's.whatsapp.net', 0)")
    add(db, 1, 1, 1, ms(2026, 10, 3, 9, 0), "selam, yeniden kurdum")
    db.execute("INSERT INTO chat VALUES (1, 5, 1, 1, 1, 0, 1, 1, 1)")
    db.commit()
    db.close()
    export = ("1.3.2021 10:00 - Ben: ilk mesaj\n1.3.2021 10:05 - Kişi 1: cevap\n"
              "15.8.2025 18:00 - Kişi 1: aradan yıllar geçti\n3.10.2026 9:00 - Ben: selam, yeniden kurdum\n")
    msgs = chatexport.parse_lines(export.splitlines(keepends=True))
    out = tmp_path / "merged.db"
    merge.apply_merge(path, out, lambda d: merge.plan_merge(d, msgs, "5550001122", "Ben"))
    merged = sqlite3.connect(out)
    assert chat_texts(merged) == ["ilk mesaj", "cevap", "aradan yıllar geçti", "selam, yeniden kurdum"]
    assert_pointers_consistent(merged)
