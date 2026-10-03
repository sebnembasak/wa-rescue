from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import crypt15, merge, protowire, service

KEY_ENV = "WA_BACKUP_KEY"


def hexdump(data: bytes, start: int = 0, width: int = 16) -> list[str]:
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{start + i:08x}  {hexpart:<{width * 3}} {text}")
    return lines


def read_key() -> bytes:
    # Never take the key as a CLI argument: it would land in shell history.
    raw = os.environ.get(KEY_ENV) or getpass.getpass("64-digit backup key: ")
    return crypt15.parse_root_key(raw)


def _refuse_existing(out: Path, force: bool) -> bool:
    try:
        service.ensure_new_output(out, force)
    except service.OutputExistsError:
        print(f"{out} exists; use --force to overwrite", file=sys.stderr)
        return True
    return False


def cmd_inspect(args: argparse.Namespace) -> int:
    info = service.inspect_backup(args.file)
    print(f"file size      : {info.size:,} bytes")
    print("first bytes    :")
    print("\n".join("  " + l for l in hexdump(info.head)))
    if info.layout is None:
        print(f"error: {info.layout_error}", file=sys.stderr)
        return 2

    layout = info.layout
    print(f"header size    : {layout.header_size}")
    print(f"feature flag   : {'yes (0x01)' if layout.has_feature_flag else 'no'}")
    print(f"payload offset : {layout.payload_offset}")
    print(f"IV ({len(layout.iv)} bytes) : {layout.iv.hex()}")
    print(f"md5 trailer    : {'verified' if info.md5_verified else 'absent / not matching'}")
    print("header fields  :")
    print("\n".join("  " + l for l in protowire.format_fields(layout.fields)))
    return 0


def cmd_decrypt(args: argparse.Namespace) -> int:
    out = Path(args.output)
    if _refuse_existing(out, args.force):
        return 1

    result = service.decrypt_backup(args.file, out, read_key(), overwrite=args.force)
    print(f"decrypted ({result.layout_label}) -> {result.output}  [{result.size:,} bytes]")

    if result.summary is None:
        print("warning: output is not a SQLite file (maybe a zip-style backup)")
        return 0

    print(f"tables         : {len(result.summary.tables)}")
    for name, count in result.summary.row_counts.items():
        print(f"  {name:<12} : {count:,} rows")
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    info = service.parse_export(args.file, args.tz)
    print(f"messages       : {info.total:,}")
    print(f"range          : {info.first:%Y-%m-%d %H:%M} -> {info.last:%Y-%m-%d %H:%M}")
    print("senders        :")
    for name, count in info.senders.most_common():
        mark = "  <- you (from_me=1)" if args.me and name == args.me else ""
        print(f"  {count:>8,}  {name}{mark}")
    print("kinds          : " + ", ".join(f"{k}={v:,}" for k, v in info.kinds.most_common()))
    print(f"edited         : {info.edited:,}")
    if args.me and args.me not in info.senders:
        print(f"warning: --me {args.me!r} matches no sender; copy the name exactly from the list above")
        return 1
    return 0


def cmd_encrypt(args: argparse.Namespace) -> int:
    out = Path(args.output)
    if _refuse_existing(out, args.force):
        return 1
    if not service.is_sqlite_file(args.db):
        print(f"{args.db} is not a SQLite database", file=sys.stderr)
        return 1
    try:
        result = service.encrypt_backup(args.db, args.template, out, read_key(), overwrite=args.force)
    except service.RoundTripError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"encrypted -> {result.output}  [{result.size:,} bytes, round-trip verified]")
    return 0


def _print_plan(result: service.MergeResult) -> None:
    plan, tz = result.plan, result.tz
    zone = ZoneInfo(tz)

    def local(ms: int, open_label: str) -> str:
        if ms in (0, merge.FAR_FUTURE):
            return open_label
        return merge.ms_to_utc(ms).astimezone(zone).strftime("%Y-%m-%d %H:%M")

    print(f"chat           : #{plan.chat_id} (jid #{plan.jid_id})")
    print(f"gaps filled    : {len(plan.filled)}  ({tz})")
    for g in plan.filled:
        print(f"  {local(g.start_ms, '(start)'):>16} -> {local(g.end_ms, '(end)'):<16} {g.inserted:>9,} msgs")
    print(f"to insert      : {len(plan.rows):,}  (you: {result.from_me:,}, them: {result.from_them:,})")
    print("  by kind      : " + ", ".join(f"{k}={v:,}" for k, v in result.kinds.most_common()))
    if plan.skipped:
        print("skipped        : " + ", ".join(f"{k}={v:,}" for k, v in plan.skipped.most_common()))


def cmd_merge(args: argparse.Namespace) -> int:
    export = service.parse_export(args.export, args.tz)
    since = datetime.strptime(args.since, "%Y-%m-%d").date() if args.since else None
    request = service.MergeRequest(export.messages, args.phone, args.me, args.tz,
                                   args.skip_media, args.min_gap_days, since)
    try:
        if args.dry_run:
            _print_plan(service.plan_merge(args.db, request))
            print("dry run: nothing written")
            return 0
        if not args.out:
            print("--out is required unless --dry-run", file=sys.stderr)
            return 1
        result = service.apply_merge(args.db, args.out, request)
    except merge.MergeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_plan(result)
    print(f"written        : {args.out}  (sqlite quick_check ok)")
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else None
    if not args.dry_run and out is None:
        print("--out is required unless --dry-run", file=sys.stderr)
        return 1
    if out is not None and out.exists():
        print(f"{out} exists; refusing to overwrite", file=sys.stderr)
        return 1
    try:
        plan = service.plan_repair(args.db, args.baseline)
    except merge.MergeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"baseline max id: {plan.baseline_max_id:,}  (rows above it were written after the restore)")
    print(f"next _id now   : {plan.sequence_before + 1:,}   highest sort_id: {plan.max_sort:,}")
    print(f"chats to fix   : {len(plan.displaced)}")
    for chat, ids in plan.displaced.items():
        print(f"  chat #{chat:<8} {len(ids):>6} message(s) moved to the end")
    print(f"counter bump   : {'yes' if plan.needs_sequence_bump else 'not needed'}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    try:
        service.apply_repair(args.db, plan, out)
    except merge.MergeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"written        : {out}  (sqlite quick_check ok)")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .ui import server
    from .ui.session import Session

    def announce(running: server.WizardServer) -> None:
        print(f"warescue ui    : {running.url}")
        print(f"workspace      : {running.session.workspace}")
        print("open the link above if no browser window appears; press Ctrl+C to stop", flush=True)

    session = Session.start(Path(args.workspace) if args.workspace else None)
    server.serve(session, port=args.port, open_browser=not args.no_browser, announce=announce)
    print("warescue ui stopped; the key was wiped from memory")
    return 0


def _utf8_console() -> None:
    # Windows consoles default to a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    from . import __version__
    parser = argparse.ArgumentParser(prog="warescue")
    parser.add_argument("--version", action="version", version=f"warescue {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", help="dump the crypt15 container layout")
    p.add_argument("file")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("decrypt", help=f"decrypt to SQLite (key via ${KEY_ENV} or prompt)")
    p.add_argument("file")
    p.add_argument("output")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_decrypt)

    p = sub.add_parser("parse", help="preview an exported _chat.txt (read-only)")
    p.add_argument("file")
    p.add_argument("--me", help="your name exactly as it appears in the export")
    p.add_argument("--tz", default="Europe/Istanbul", help="timezone of the exporting phone")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("encrypt", help="re-encrypt a database using an existing backup as template")
    p.add_argument("db", help="SQLite database to encrypt")
    p.add_argument("template", help="the original .crypt15 (header and key check)")
    p.add_argument("output")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_encrypt)

    p = sub.add_parser("repair", help="move messages hidden mid-chat after a restore to the end")
    p.add_argument("db", help="decrypted backup taken after the restore (never modified)")
    p.add_argument("--baseline", required=True, help="the merged database that was restored")
    p.add_argument("--out")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("merge", help="fill the chat's gap with messages from an export")
    p.add_argument("db", help="decrypted msgstore.db (never modified)")
    p.add_argument("export", help="_chat.txt from the other person")
    p.add_argument("--phone", required=True, help="contact's number, at least the last 7 digits")
    p.add_argument("--me", required=True, help="your name exactly as in the export")
    p.add_argument("--out", help="where to write the merged copy")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-media", action="store_true", help="drop media placeholders")
    p.add_argument("--min-gap-days", type=float, default=30,
                   help="silences shorter than this are not treated as missing history")
    p.add_argument("--since", help="only insert export messages from this date on (YYYY-MM-DD)")
    p.add_argument("--tz", default="Europe/Istanbul")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("ui", help="open the step-by-step recovery wizard in your browser")
    p.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a random free one)")
    p.add_argument("--workspace", help="folder for working files (default: ~/warescue-workspace)")
    p.add_argument("--no-browser", action="store_true", help="only print the link")
    p.set_defaults(func=cmd_ui)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except crypt15.Crypt15Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
