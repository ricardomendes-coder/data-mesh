"""Minimal user store.

Usernames -> bcrypt password hashes, persisted to a JSON file. This is
intentionally simple for v1; when you're ready you can swap this module for a
real users table without touching the rest of the app.
"""

import json
import os
from pathlib import Path
from threading import Lock

import bcrypt

USERS_FILE = Path(os.getenv("USERS_FILE", "/app/data/users.json"))
_lock = Lock()


def _load() -> dict[str, str]:
    if not USERS_FILE.exists():
        return {}
    with USERS_FILE.open() as f:
        return json.load(f)


def _save(users: dict[str, str]) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with USERS_FILE.open("w") as f:
        json.dump(users, f, indent=2)


def any_users() -> bool:
    return len(_load()) > 0


def user_exists(username: str) -> bool:
    return username in _load()


def add_user(username: str, password: str) -> None:
    with _lock:
        users = _load()
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        users[username] = pw_hash
        _save(users)


def verify_user(username: str, password: str) -> bool:
    stored = _load().get(username)
    if not stored:
        # Hash a dummy value so a missing user takes about as long as a real
        # one (reduces username-enumeration via timing).
        bcrypt.hashpw(b"timing", bcrypt.gensalt())
        return False
    return bcrypt.checkpw(password.encode(), stored.encode())
