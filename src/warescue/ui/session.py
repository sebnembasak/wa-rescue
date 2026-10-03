"""Server-side state of one wizard session.

The backup key lives only here, in memory, as a mutable buffer that is
zeroed when the session finishes. It is never serialised: `snapshot()`
reports only whether a key is set.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from .. import crypt15, service


def display_path(path: Path) -> str:
    """Shorten paths under the home folder to "~/..." (not on Windows, where
    Explorer does not understand "~")."""
    if os.name != "nt":
        try:
            return f"~/{path.relative_to(Path.home()).as_posix()}"
        except ValueError:
            pass
    return str(path)


class Session:
    def __init__(self, workspace: Path, token: str | None = None) -> None:
        self.workspace = workspace
        self.token = token or secrets.token_urlsafe(32)
        self._key: bytearray | None = None
        self._state: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._last_activity = time.monotonic()

    @classmethod
    def start(cls, workspace_root: Path | None = None) -> Session:
        return cls(service.create_workspace(workspace_root))

    # activity

    def touch(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_activity

    # key

    def set_key(self, text: str) -> None:
        """Accept the key as WhatsApp shows it. Raises Crypt15Error if malformed."""
        key = bytearray(crypt15.parse_root_key(text))
        with self._lock:
            self._wipe_key()
            self._key = key

    def key(self) -> bytes:
        with self._lock:
            if self._key is None:
                raise service.ServiceError("no backup key entered")
            return bytes(self._key)

    @property
    def has_key(self) -> bool:
        with self._lock:
            return self._key is not None

    def clear_key(self) -> None:
        with self._lock:
            self._wipe_key()

    def _wipe_key(self) -> None:
        if self._key is not None:
            self._key[:] = bytes(len(self._key))
            self._key = None

    # wizard state

    def update_state(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    def reset_state(self, **values: Any) -> None:
        with self._lock:
            self._state = dict(values)

    def remove_state(self, *names: str) -> None:
        with self._lock:
            for name in names:
                self._state.pop(name, None)

    def state(self, name: str) -> Any:
        with self._lock:
            return self._state.get(name)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"workspace": display_path(self.workspace), "has_key": self._key is not None,
                    "state": dict(self._state)}

    # end

    def close(self) -> None:
        """Forget the key. Remove the workspace if nothing was ever put in it."""
        self.clear_key()
        if self.workspace.is_dir() and not any(
                p.name != service.WORKSPACE_MARKER for p in self.workspace.iterdir()):
            service.cleanup_workspace(self.workspace)
