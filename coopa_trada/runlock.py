"""One running copy per database and per Schwab account.

``Engine.run`` takes these locks itself, so the CLI and the web runner cannot
skip them:

* ``<database>.lock`` next to the SQLite file, always;
* ``<lock dir>/account-<sha256>.lock`` for runs bound to a Schwab account, so
  two databases cannot trade the same account. The lock dir defaults to
  ``~/.coopa_trada/locks`` (override with ``COOPA_TRADA_LOCK_DIR``).

The operating system releases the lock when its process exits, even after a
crash. The file also records who held it; a clean exit erases that record. A
lock that is free but still has a record therefore means the previous run did
not shut down cleanly. A stale record from a LIVE run blocks new runs until a
person clears it (``python -m coopa_trada unlock`` or the dashboard), after
checking the account on the Reconcile page. A stale record from a DRY-RUN is
cleared automatically: a simulation cannot leave real orders behind.

File layout: byte 0 is the locked byte; the owner record (JSON) follows it,
so it stays readable while the byte is locked (Windows locks are mandatory).
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "RunLocked",
    "StaleLock",
    "ProcessLock",
    "lock_dir",
    "database_lock_path",
    "account_lock_path",
    "inspect_lock",
    "clear_stale_lock",
    "known_locks",
]

_PAD = b" "


class RunLocked(RuntimeError):
    """Another live process holds the lock."""


class StaleLock(RuntimeError):
    """A crashed or killed run left its record behind; a person must clear it."""


def lock_dir() -> Path:
    return Path(os.environ.get("COOPA_TRADA_LOCK_DIR", Path.home() / ".coopa_trada" / "locks")).expanduser()


def database_lock_path(db_path: str | Path) -> Path:
    db_path = Path(db_path).resolve()
    return db_path.with_name(db_path.name + ".lock")


def account_lock_path(account_hash: str) -> Path:
    digest = hashlib.sha256(account_hash.encode("utf-8")).hexdigest()[:32]
    return lock_dir() / f"account-{digest}.lock"


if os.name == "nt":  # pragma: no cover - exercised only on Windows
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    # flock, not lockf: lockf locks belong to the process, so a second open in
    # the same process would not conflict and closing any descriptor (say, a
    # dashboard status check) would silently release a running engine's lock.
    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _open(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    if os.fstat(fd).st_size == 0:
        os.write(fd, _PAD)
    return fd


def _read_record(fd: int) -> dict | None:
    os.lseek(fd, 1, os.SEEK_SET)
    data = b""
    while chunk := os.read(fd, 65536):
        data += chunk
    if not data.strip():
        return None
    try:
        record = json.loads(data)
    except ValueError:
        return {"unreadable": data[:200].decode("utf-8", "replace")}
    return record if isinstance(record, dict) else {"unreadable": str(record)[:200]}


def _write_record(fd: int, record: dict | None) -> None:
    os.ftruncate(fd, 1)
    if record is not None:
        os.lseek(fd, 1, os.SEEK_SET)
        os.write(fd, json.dumps(record).encode("utf-8"))
    os.fsync(fd)


def describe(record: dict | None) -> str:
    if not record:
        return "unknown process"
    if "unreadable" in record:
        return "a process whose record is unreadable"
    return (f"process {record.get('pid')} on {record.get('host')} ({record.get('purpose')}, "
            f"{record.get('mode')}), started {record.get('started_at')}")


class ProcessLock:
    """Exclusive, crash-detecting lock. Use as a context manager."""

    def __init__(self, path: str | Path, *, purpose: str, mode: str):
        self.path = Path(path)
        self.purpose = purpose
        self.mode = mode
        self._fd: int | None = None
        self.cleared_stale: dict | None = None

    def acquire(self) -> "ProcessLock":
        fd = _open(self.path)
        try:
            if not _try_lock(fd):
                raise RunLocked(
                    f"Another copy of Coopa Trada is already running against this {self.purpose} "
                    f"({describe(_read_record(fd))}). Only one copy may run at a time. "
                    f"Lock file: {self.path}")
            previous = _read_record(fd)
            if previous is not None and previous.get("mode") == "DRY-RUN":
                # A simulation cannot leave real orders behind: note it and go on.
                self.cleared_stale = previous
            elif previous is not None:
                _unlock(fd)
                raise StaleLock(
                    f"The last run against this {self.purpose} did not shut down cleanly "
                    f"({describe(previous)}). It may have left orders or positions open. Check the "
                    f"Reconcile page or Schwab first, then clear the lock with "
                    f"'python -m coopa_trada unlock' or the dashboard's Clear stale lock button. "
                    f"Lock file: {self.path}")
            _write_record(fd, {
                "pid": os.getpid(), "host": socket.gethostname(), "purpose": self.purpose,
                "mode": self.mode, "started_at": datetime.now(timezone.utc).isoformat(),
            })
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _write_record(fd, None)  # erase the record: this was a clean exit
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def inspect_lock(path: str | Path) -> dict:
    """``{"state": "free" | "held" | "stale", ...}`` without changing anything."""
    path = Path(path)
    if not path.exists():
        return {"state": "free", "path": str(path), "holder": None}
    fd = os.open(path, os.O_RDWR)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, _PAD)
        if not _try_lock(fd):
            record = _read_record(fd)
            return {"state": "held", "path": str(path), "holder": record, "description": describe(record)}
        try:
            record = _read_record(fd)
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
    if record is None:
        return {"state": "free", "path": str(path), "holder": None}
    return {"state": "stale", "path": str(path), "holder": record, "description": describe(record)}


def known_locks(db_path: str | Path) -> list[dict]:
    """The database lock plus any account locks on this computer."""
    paths = [database_lock_path(db_path)] + sorted(lock_dir().glob("account-*.lock"))
    return [{**inspect_lock(p), "purpose": "database" if i == 0 else "Schwab account"}
            for i, p in enumerate(paths)]


def clear_stale_lock(path: str | Path, *, force_other_host: bool = False) -> dict:
    """Erase a stale record. Refuses if a live process holds the lock.

    A record from another computer (a shared folder) is refused unless
    ``force_other_host``: file locks are unreliable across network shares, so
    that copy might still be running.
    """
    path = Path(path)
    if not path.exists():
        return {"cleared": False, "message": "No lock file; nothing to clear."}
    fd = os.open(path, os.O_RDWR)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, _PAD)
        if not _try_lock(fd):
            raise RunLocked(f"Not cleared: a running copy holds this lock ({describe(_read_record(fd))}).")
        try:
            record = _read_record(fd)
            if record is None:
                return {"cleared": False, "message": "The lock is not stale; nothing to clear."}
            host = record.get("host")
            if host and host != socket.gethostname() and not force_other_host:
                raise RunLocked(
                    f"Not cleared: the lock was taken on another computer ({host}). Make sure Coopa "
                    "Trada is not running there, then clear it with --force-other-host.")
            _write_record(fd, None)
            return {"cleared": True, "message": f"Cleared the stale lock left by {describe(record)}."}
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
