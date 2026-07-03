"""Disk state for the bridge: credentials, context tokens, sync buffer, sessions.

Layout under WXCC_HOME (default ~/.wxcc):
    accounts/<account_id>.json                iLink credentials
    accounts/<account_id>.context-tokens.json per-peer context tokens
    accounts/<account_id>.sync.json           getupdates sync buffer
    sessions.json                             chat_id -> Claude session id
    state.json                                owner pairing state
    cache/                                    downloaded inbound media
    config.json                               bridge configuration
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional


def get_home() -> Path:
    root = Path(os.getenv("WXCC_HOME", "") or (Path.home() / ".wxcc"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def account_dir() -> Path:
    path = get_home() / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_account(*, account_id: str, token: str, base_url: str, user_id: str = "") -> None:
    payload = {
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = account_dir() / f"{account_id}.json"
    atomic_json_write(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_account(account_id: str) -> Optional[Dict[str, Any]]:
    return _read_json(account_dir() / f"{account_id}.json")


def list_accounts() -> list[str]:
    return sorted(
        p.stem
        for p in account_dir().glob("*.json")
        if not p.name.endswith((".context-tokens.json", ".sync.json"))
    )


class ContextTokenStore:
    """Disk-backed context_token cache keyed by peer user id."""

    def __init__(self, account_id: str):
        self._account_id = account_id
        self._path = account_dir() / f"{account_id}.context-tokens.json"
        self._cache: Dict[str, str] = {}
        data = _read_json(self._path)
        if isinstance(data, dict):
            self._cache = {k: v for k, v in data.items() if isinstance(v, str) and v}

    def get(self, user_id: str) -> Optional[str]:
        return self._cache.get(user_id)

    def set(self, user_id: str, token: str) -> None:
        self._cache[user_id] = token
        atomic_json_write(self._path, self._cache)

    def pop(self, user_id: str) -> None:
        if self._cache.pop(user_id, None) is not None:
            atomic_json_write(self._path, self._cache)


def load_sync_buf(account_id: str) -> str:
    data = _read_json(account_dir() / f"{account_id}.sync.json")
    return (data or {}).get("get_updates_buf", "") if isinstance(data, dict) else ""


def save_sync_buf(account_id: str, sync_buf: str) -> None:
    atomic_json_write(account_dir() / f"{account_id}.sync.json", {"get_updates_buf": sync_buf})


class SessionStore:
    """chat_id -> Claude Code session id, persisted so /resume survives restarts.

    A per-chat *history* of past conversations is also kept (in a separate file
    so the simple sessions.json format is untouched) so the user can `!resume`
    an earlier conversation. Each history entry tracks the latest session id for
    a logical conversation plus a short label (its first prompt) and timestamp.
    """

    HISTORY_CAP = 30

    def __init__(self):
        self._path = get_home() / "sessions.json"
        data = _read_json(self._path)
        self._map: Dict[str, str] = data if isinstance(data, dict) else {}
        self._hist_path = get_home() / "session_history.json"
        hist = _read_json(self._hist_path)
        self._hist: Dict[str, list] = hist if isinstance(hist, dict) else {}

    def get(self, chat_id: str) -> Optional[str]:
        return self._map.get(chat_id)

    def set(self, chat_id: str, session_id: str, label: Optional[str] = None) -> None:
        prev = self._map.get(chat_id)
        self._map[chat_id] = session_id
        atomic_json_write(self._path, self._map)
        self._record(chat_id, prev, session_id, label)

    def _record(self, chat_id: str, prev: Optional[str], new: str, label: Optional[str]) -> None:
        now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        entries = self._hist.setdefault(chat_id, [])
        # Continuation of an existing conversation: keep its label, bump the id.
        for entry in entries:
            if entry.get("id") == prev and prev is not None:
                entry["id"] = new
                entry["ts"] = now
                atomic_json_write(self._hist_path, self._hist)
                return
        # A brand-new conversation.
        entries.append({"id": new, "label": (label or "").strip()[:40], "ts": now})
        if len(entries) > self.HISTORY_CAP:
            del entries[: -self.HISTORY_CAP]
        atomic_json_write(self._hist_path, self._hist)

    def history(self, chat_id: str) -> list:
        """Past conversations for a chat, most recent first."""
        return list(reversed(self._hist.get(chat_id, [])))

    def switch(self, chat_id: str, session_id: str) -> None:
        """Point the chat at an existing session id without recording new
        history (the entry already exists — used by !resume)."""
        self._map[chat_id] = session_id
        atomic_json_write(self._path, self._map)

    def ensure(self, chat_id: str, session_id: str, label: str = "") -> None:
        """Make sure a session id is represented in history. Used to backfill a
        conversation that predates the history feature (or was only in the
        current-pointer map) so it shows up in !resume."""
        if not session_id:
            return
        entries = self._hist.setdefault(chat_id, [])
        if any(e.get("id") == session_id for e in entries):
            return
        now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        entries.append({"id": session_id, "label": label.strip()[:40], "ts": now})
        if len(entries) > self.HISTORY_CAP:
            del entries[: -self.HISTORY_CAP]
        atomic_json_write(self._hist_path, self._hist)

    def reset(self, chat_id: str) -> None:
        # Drop the *current* pointer only; keep history so it can be resumed.
        if self._map.pop(chat_id, None) is not None:
            atomic_json_write(self._path, self._map)


class BridgeState:
    """Small mutable state (owner pairing)."""

    def __init__(self):
        self._path = get_home() / "state.json"
        data = _read_json(self._path)
        self._state: Dict[str, Any] = data if isinstance(data, dict) else {}

    @property
    def owner_id(self) -> Optional[str]:
        return self._state.get("owner_id") or None

    def set_owner(self, user_id: str) -> None:
        self._state["owner_id"] = user_id
        atomic_json_write(self._path, self._state)


DEFAULT_CONFIG: Dict[str, Any] = {
    # Who may talk to the bridge over WeChat DM:
    #   "first"     — first sender ever becomes the owner (persisted), others ignored
    #   "allowlist" — only ids in allow_from
    #   "open"      — everyone (dangerous: full Claude Code access)
    "dm_policy": "first",
    "allow_from": [],
    # Claude Code execution
    "cwd": str(Path.home() / "wxcc-workspace"),
    "permission_mode": "bypassPermissions",
    "model": None,
    "max_turns": None,
    "run_timeout_seconds": 1800,
    "setting_sources": [],
    "system_prompt_extra": "",
    # Weixin delivery tuning
    "send_chunk_delay_seconds": 1.5,
    "text_batch_delay_seconds": 3.0,
}


def load_config() -> Dict[str, Any]:
    path = get_home() / "config.json"
    data = _read_json(path)
    if not isinstance(data, dict):
        atomic_json_write(path, DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged
