"""Small request/response types shared by the server and the API handlers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from typing import Any, BinaryIO

JSON_LIMIT = 64 * 1024
CHUNK = 1024 * 1024


class ApiError(Exception):
    """A refusal the page can explain: `code` selects the translated text,
    `message` is a plain English fallback."""

    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Response:
    status: HTTPStatus
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()
    file: Path | None = None

    @classmethod
    def json(cls, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> Response:
        return cls(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    @classmethod
    def error(cls, status: HTTPStatus, code: str, message: str) -> Response:
        return cls.json({"error": code, "message": message}, status)

    @classmethod
    def download(cls, path: Path, filename: str) -> Response:
        """Stream a file as an attachment; `filename` must be plain ASCII."""
        return cls(HTTPStatus.OK, b"", "application/octet-stream",
                   (("Content-Disposition", f'attachment; filename="{filename}"'),), path)

    @property
    def length(self) -> int:
        return self.file.stat().st_size if self.file else len(self.body)

    def write_to(self, out: BinaryIO) -> None:
        if self.file is None:
            out.write(self.body)
            return
        with open(self.file, "rb") as source:
            while chunk := source.read(CHUNK):
                out.write(chunk)


class Request:
    def __init__(self, method: str, path: str, headers: Message, body: BinaryIO,
                 match: re.Match[str] | None = None) -> None:
        self.method = method
        self.path = path
        self.headers = headers
        self.match = match
        self._body = body

    def content_length(self, limit: int) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "bad_request", "Content-Length is required")
        try:
            length = int(raw)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "invalid Content-Length") from None
        if length < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "invalid Content-Length")
        if length > limit:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large", "request body is too large")
        return length

    def json(self) -> dict[str, Any]:
        length = self.content_length(JSON_LIMIT)
        try:
            payload = json.loads(self._body.read(length) or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "body is not valid JSON") from None
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "body must be a JSON object")
        return payload

    def save_body(self, target: Path, limit: int) -> int:
        """Stream the body to `target` without holding it in memory."""
        remaining = total = self.content_length(limit)
        try:
            with open(target, "wb") as out:
                while remaining:
                    chunk = self._body.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ApiError(HTTPStatus.BAD_REQUEST, "upload_incomplete",
                                       "the upload stopped before the whole file arrived")
                    out.write(chunk)
                    remaining -= len(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return total
