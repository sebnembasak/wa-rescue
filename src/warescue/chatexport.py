"""Parser for WhatsApp "Export chat" text files.

Each message starts with a timestamp prefix; any line that does not start
with one is a continuation of the previous message (multi-line messages).
Exports carry no seconds and no message ids, only minute-precision local
time, sender display name and text.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Bidi marks and BOMs that WhatsApp sprinkles into names, media lines and
# file names. They are invisible but break exact string matching.
_INVISIBLE = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff"))
_SPACES = {0x202F: " ", 0x00A0: " "}

PREFIXES = [
    # Android, dotted dates:   2.10.2026 13:29 - Name: text
    re.compile(r"^(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{2,4}),? (?P<H>\d{1,2}):(?P<M>\d{2}) - "),
    # iOS:                     [2.10.2026 13:29:05] Name: text
    re.compile(r"^\[(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{2,4}),? (?P<H>\d{1,2}):(?P<M>\d{2}):(?P<S>\d{2})\] "),
    # Android, slashed + optional 12h clock: 10/2/26, 1:29 PM - Name: text (day-first assumed)
    re.compile(r"^(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{2,4}),? (?P<H>\d{1,2}):(?P<M>\d{2})(?: ?(?P<ampm>[AaPp][Mm]))? - "),
]

# Whole-message markers only: "<Medya dahil edilmedi>", "<Video note omitted>",
# "<sticker omitted>" ... Exports can mix languages after a locale change.
OMITTED = re.compile(r"^<(?:(?P<en>[^<>]+?) omitted|(?P<tr>[^<>]*?) ?dahil edilmedi)>$")
LOCATION_PREFIXES = ("konum: ", "location: ")
POLL_PREFIXES = ("ANKET:", "POLL:")
DELETED = {"Bu mesaj silindi", "Bu mesajı sildiniz", "This message was deleted", "You deleted this message"}
EDITED_SUFFIXES = ("<Bu mesaj düzenlendi>", "<This message was edited>")
ATTACHED = re.compile(r"^(?P<file>\S.*?\.\w{2,5}) \((?:dosya ekli|file attached)\)$")


@dataclass
class ExportMessage:
    line_no: int
    time: datetime  # timezone-aware, minute precision
    sender: str | None  # None for system lines
    text: str
    kind: str = "text"  # text | media_omitted | media | contact | location | poll | deleted | system
    media_file: str | None = None
    media_label: str | None = None   # e.g. "Video note", "Medya"
    edited: bool = False


def clean(line: str) -> str:
    return line.translate(_INVISIBLE).translate(_SPACES).rstrip("\r\n")


def _match_prefix(line: str):
    for pattern in PREFIXES:
        if m := pattern.match(line):
            return m
    return None


def _to_datetime(m: re.Match, tz: ZoneInfo) -> datetime:
    g = m.groupdict()
    year = int(g["y"])
    year += 2000 if year < 100 else 0
    hour = int(g["H"])
    if ampm := g.get("ampm"):
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    return datetime(year, int(g["m"]), int(g["d"]), hour, int(g["M"]), int(g.get("S") or 0), tzinfo=tz)


def _classify(msg: ExportMessage) -> None:
    for suffix in EDITED_SUFFIXES:
        if msg.text.endswith(suffix):
            msg.text = msg.text[: -len(suffix)].rstrip()
            msg.edited = True
    first, _, rest = msg.text.partition("\n")
    if msg.sender is None:
        msg.kind = "system"
    elif m := OMITTED.match(msg.text):
        msg.kind, msg.media_label = "media_omitted", (m["en"] or m["tr"] or "Medya").strip()
    elif msg.text in DELETED:
        msg.kind = "deleted"
    elif m := ATTACHED.match(first):
        msg.media_file, msg.text = m["file"], rest  # rest = caption
        msg.kind = "contact" if msg.media_file.lower().endswith(".vcf") else "media"
    elif msg.text.lower().startswith(LOCATION_PREFIXES):
        msg.kind = "location"
    elif msg.text.startswith(POLL_PREFIXES):
        msg.kind = "poll"


def parse_lines(lines, tz: str = "Europe/Istanbul") -> list[ExportMessage]:
    zone = ZoneInfo(tz)
    messages: list[ExportMessage] = []
    for line_no, raw in enumerate(lines, start=1):
        line = clean(raw)
        m = _match_prefix(line)
        if m is None:
            if messages:                       # continuation of a multi-line message
                messages[-1].text += "\n" + line
            continue
        body = line[m.end():]
        sender, sep, text = body.partition(": ")
        if not sep:                            # "… - Messages are end-to-end encrypted" etc.
            sender, text = None, body
        messages.append(ExportMessage(line_no, _to_datetime(m, zone), sender, text))

    for msg in messages:
        _classify(msg)
    return messages


def parse_file(path: str | Path, tz: str = "Europe/Istanbul") -> list[ExportMessage]:
    with open(path, encoding="utf-8-sig") as fh:
        return parse_lines(fh, tz)


def summarize(messages: list[ExportMessage]) -> dict:
    real = [m for m in messages if m.kind != "system"]
    return {
        "total": len(messages),
        "first": real[0].time if real else None,
        "last": real[-1].time if real else None,
        "senders": Counter(m.sender for m in real),
        "kinds": Counter(m.kind for m in messages),
        "edited": sum(m.edited for m in messages),
    }
