"""Minimal, schema-less protobuf wire-format codec.

We only need to *look at* the small header that prefixes a crypt15 backup,
so instead of pulling in protobuf + generated classes we walk the wire format
by hand: every field is a varint key (field_number << 3 | wire_type) followed
by a payload whose shape depends on the wire type.
"""
from __future__ import annotations

from dataclasses import dataclass

VARINT, I64, LEN, I32 = 0, 1, 2, 5
_WIRE_NAMES = {VARINT: "varint", I64: "i64", LEN: "bytes", I32: "i32"}


class ProtoError(ValueError):
    """Raised when bytes are not valid protobuf wire format."""


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtoError("varint longer than 10 bytes")


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("negative varints are not supported")
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def encode_field(number: int, value: int | bytes) -> bytes:
    """Encode a single varint or length-delimited field."""
    if isinstance(value, int):
        return encode_varint(number << 3 | VARINT) + encode_varint(value)
    return encode_varint(number << 3 | LEN) + encode_varint(len(value)) + value


@dataclass
class Field:
    number: int
    wire_type: int
    value: int | bytes
    # Length-delimited payloads *may* be nested messages.
    # Protobuf does not say which, so we optimistically try to decode them.
    children: list["Field"] | None = None


def decode(buf: bytes) -> list[Field]:
    fields: list[Field] = []
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        number, wire_type = key >> 3, key & 0x07
        if number == 0:
            raise ProtoError("field number 0 is invalid")

        if wire_type == VARINT:
            value, pos = read_varint(buf, pos)
        elif wire_type in (I64, I32):
            size = 8 if wire_type == I64 else 4
            if pos + size > len(buf):
                raise ProtoError("truncated fixed-width field")
            value, pos = buf[pos:pos + size], pos + size
        elif wire_type == LEN:
            length, pos = read_varint(buf, pos)
            if pos + length > len(buf):
                raise ProtoError("length-delimited field overruns buffer")
            value, pos = buf[pos:pos + length], pos + length
        else:
            raise ProtoError(f"unsupported wire type {wire_type}")

        field = Field(number, wire_type, value)
        if wire_type == LEN:
            field.children = try_decode(value)
        fields.append(field)
    return fields


def try_decode(buf: bytes) -> list[Field] | None:
    if not buf:
        return None
    try:
        return decode(buf)
    except ProtoError:
        return None


def find(fields: list[Field] | None, *path: int) -> Field | None:
    """Follow a path of field numbers, e.g. find(fields, 3, 1)."""
    current, found = fields, None
    for number in path:
        if current is None:
            return None
        found = next((f for f in current if f.number == number), None)
        if found is None:
            return None
        current = found.children
    return found


def format_fields(fields: list[Field], indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    for f in fields:
        kind = _WIRE_NAMES.get(f.wire_type, "?")
        if isinstance(f.value, int):
            lines.append(f"{pad}#{f.number} {kind} = {f.value}")
            continue
        raw = f.value
        preview = raw[:32].hex() + ("..." if len(raw) > 32 else "")
        line = f"{pad}#{f.number} {kind}[{len(raw)}] {preview}"
        try:
            text = raw.decode("utf-8")
            if text and text.isprintable():
                line += f'  "{text}"'
        except UnicodeDecodeError:
            pass
        lines.append(line)
        if f.children:
            lines.extend(format_fields(f.children, indent + 1))
    return lines
