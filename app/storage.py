"""Durable, disk-backed state machine (SQLite in WAL mode).

Design rules from the spec:
  * Persistence and double-charge avoidance are core behavior, not a patch.
  * Never hold a SQL transaction open across a network call. All DB operations
    here are short; network calls happen in the worker layer, between claims.
  * Use atomic claims and unique constraints so a restart or a duplicate event
    cannot start a second paid job.

States (jobs.state):
    queued -> preparing -> submitting -> polling -> ready -> sending -> done
  video adds:  preview_ready -> awaiting_accept -> video_queued (-> submitting ...)
  terminal/review:  failed | uncertain | stalled | cancelled

`submitting` is written BEFORE any unsafe POST. Once a provider id is returned
it is stored and only that id is polled. A timeout after POST with no known
result goes to `uncertain`, never an automatic re-submit.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# ---- job kinds -------------------------------------------------------------
KIND_IMAGE = "image"
KIND_VIDEO_PREVIEW = "video_preview"
KIND_VIDEO_FINAL = "video_final"
KIND_CAPTION = "caption"
KIND_INSTAGRAM_FETCH = "instagram_fetch"

# ---- states ----------------------------------------------------------------
S_QUEUED = "queued"
S_PREPARING = "preparing"
S_SUBMITTING = "submitting"
S_POLLING = "polling"
S_READY = "ready"
S_SENDING = "sending"
S_DONE = "done"
S_PREVIEW_READY = "preview_ready"
S_AWAITING_ACCEPT = "awaiting_accept"
S_VIDEO_QUEUED = "video_queued"
S_FAILED = "failed"
S_UNCERTAIN = "uncertain"
S_STALLED = "stalled"
S_CANCELLED = "cancelled"

TERMINAL_STATES = {S_DONE, S_FAILED, S_CANCELLED}
REVIEW_STATES = {S_UNCERTAIN, S_STALLED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    owner           INTEGER,
    chat_id         INTEGER,
    input_message_id INTEGER,
    input_file_id   TEXT,
    source_hash     TEXT,
    source_path     TEXT,
    root_id         TEXT,
    parent_id       TEXT,
    state           TEXT NOT NULL,
    extra           TEXT,            -- JSON blob for per-kind fields
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    locked_by       TEXT,
    locked_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(kind, state, next_attempt_at);

CREATE TABLE IF NOT EXISTS snapshots (
    job_id            TEXT PRIMARY KEY REFERENCES jobs(job_id),
    reference_path    TEXT,
    reference_hash    TEXT,
    image_prompt      TEXT,
    image_prompt_version TEXT,
    video_prompt      TEXT,
    video_prompt_version TEXT,
    caption_prompt    TEXT,
    caption_prompt_version TEXT,
    target_width      INTEGER,
    target_height     INTEGER,
    resize_policy     TEXT,
    source_topic      TEXT,
    destination_topic TEXT,
    config_version    TEXT,
    params            TEXT             -- JSON: custom prompt/size etc.
);

CREATE TABLE IF NOT EXISTS provider (
    job_id          TEXT PRIMARY KEY REFERENCES jobs(job_id),
    provider        TEXT,
    model           TEXT,
    prediction_id   TEXT,
    run_id          TEXT,
    output_url      TEXT,
    output_path     TEXT,
    output_hash     TEXT,
    preview_revision INTEGER DEFAULT 0,
    preview_receipt TEXT,
    approved_by     INTEGER,
    approved_revision INTEGER,
    final_receipt   TEXT,
    final_file_id   TEXT,
    sanitized_error TEXT
);

CREATE TABLE IF NOT EXISTS seen_updates (
    update_id   INTEGER PRIMARY KEY,
    seen_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_callbacks (
    callback_id TEXT PRIMARY KEY,
    seen_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_prompts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    thread_id   INTEGER,
    job_id      TEXT,
    purpose     TEXT NOT NULL,        -- regen_prompt | set_reference | set_key | set_prompt
    created_at  REAL NOT NULL,
    consumed    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_prompts(user_id, consumed);

CREATE TABLE IF NOT EXISTS notices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER,
    thread_id     INTEGER,
    text          TEXT NOT NULL,
    delivery_state TEXT NOT NULL DEFAULT 'pending',
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS caption_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    output_job_id   TEXT NOT NULL,
    source_receipt  TEXT,
    destination_topic TEXT,
    state           TEXT NOT NULL DEFAULT 'queued',
    caption         TEXT,
    created_at      REAL NOT NULL,
    UNIQUE(output_job_id, state)
);

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  REAL NOT NULL
);
"""


@dataclass
class Job:
    job_id: str
    kind: str
    state: str
    owner: int | None
    chat_id: int | None
    input_message_id: int | None
    input_file_id: str | None
    source_hash: str | None
    source_path: str | None
    root_id: str | None
    parent_id: str | None
    extra: dict[str, Any]
    attempts: int
    next_attempt_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            job_id=row["job_id"],
            kind=row["kind"],
            state=row["state"],
            owner=row["owner"],
            chat_id=row["chat_id"],
            input_message_id=row["input_message_id"],
            input_file_id=row["input_file_id"],
            source_hash=row["source_hash"],
            source_path=row["source_path"],
            root_id=row["root_id"],
            parent_id=row["parent_id"],
            extra=json.loads(row["extra"]) if row["extra"] else {},
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
        )


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=30000;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def backup(self, dest: str | Path) -> None:
        """Consistent online backup via the SQLite backup API."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
            finally:
                target.close()

    # -- idempotency ---------------------------------------------------------
    def mark_update_seen(self, update_id: int) -> bool:
        """Return True if this update_id is new (and record it)."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO seen_updates(update_id, seen_at) VALUES(?, ?)",
                    (update_id, time.time()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_callback_seen(self, callback_id: str) -> bool:
        """Return True if this callback id is new. Duplicate clicks return False."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO seen_callbacks(callback_id, seen_at) VALUES(?, ?)",
                    (callback_id, time.time()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # -- jobs ----------------------------------------------------------------
    def create_job(
        self,
        job_id: str,
        kind: str,
        *,
        owner: int | None = None,
        chat_id: int | None = None,
        input_message_id: int | None = None,
        input_file_id: str | None = None,
        source_hash: str | None = None,
        source_path: str | None = None,
        root_id: str | None = None,
        parent_id: str | None = None,
        state: str = S_QUEUED,
        extra: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs(job_id, kind, owner, chat_id, input_message_id,
                       input_file_id, source_hash, source_path, root_id, parent_id,
                       state, extra, created_at, updated_at, next_attempt_at, attempts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)""",
                (
                    job_id, kind, owner, chat_id, input_message_id, input_file_id,
                    source_hash, source_path, root_id, parent_id, state,
                    json.dumps(extra or {}), now, now,
                ),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return Job.from_row(row) if row else None

    def set_state(
        self,
        job_id: str,
        state: str,
        *,
        expect: str | Iterable[str] | None = None,
        source_path: str | None = None,
        source_hash: str | None = None,
        next_attempt_at: float | None = None,
        bump_attempt: bool = False,
    ) -> bool:
        """Atomic state transition. If `expect` is given, only transition when the
        current state matches (guards against races / stale events)."""
        now = time.time()
        sets = ["state=?", "updated_at=?"]
        params: list[Any] = [state, now]
        if source_path is not None:
            sets.append("source_path=?")
            params.append(source_path)
        if source_hash is not None:
            sets.append("source_hash=?")
            params.append(source_hash)
        if next_attempt_at is not None:
            sets.append("next_attempt_at=?")
            params.append(next_attempt_at)
        if bump_attempt:
            sets.append("attempts=attempts+1")
        where = "job_id=?"
        params.append(job_id)
        if expect is not None:
            expected = [expect] if isinstance(expect, str) else list(expect)
            where += " AND state IN (%s)" % ",".join("?" * len(expected))
            params.extend(expected)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE {where}", params
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_extra(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT extra FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            extra = json.loads(row["extra"]) if row and row["extra"] else {}
            extra.update(fields)
            self._conn.execute(
                "UPDATE jobs SET extra=?, updated_at=? WHERE job_id=?",
                (json.dumps(extra), time.time(), job_id),
            )
            self._conn.commit()

    def claim_next(self, kinds: Iterable[str], states: Iterable[str], worker: str) -> Job | None:
        """Atomically claim one due job in any of `states` for any of `kinds`.
        Returns the claimed Job or None. The claim is a single UPDATE so two
        workers can never grab the same job."""
        kinds = list(kinds)
        states = list(states)
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                f"""SELECT job_id FROM jobs
                     WHERE kind IN ({','.join('?' * len(kinds))})
                       AND state IN ({','.join('?' * len(states))})
                       AND next_attempt_at <= ?
                       AND (locked_by IS NULL OR locked_at < ?)
                     ORDER BY created_at ASC LIMIT 1""",
                (*kinds, *states, now, now - 900),
            ).fetchone()
            if not row:
                return None
            job_id = row["job_id"]
            cur = self._conn.execute(
                """UPDATE jobs SET locked_by=?, locked_at=? WHERE job_id=?
                   AND (locked_by IS NULL OR locked_at < ?)""",
                (worker, now, job_id, now - 900),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            got = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return Job.from_row(got) if got else None

    def release(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET locked_by=NULL, locked_at=NULL WHERE job_id=?",
                (job_id,),
            )
            self._conn.commit()

    def children(self, parent_id: str) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE parent_id=? ORDER BY created_at", (parent_id,)
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    def recover_locks_on_boot(self) -> None:
        """On startup, release stale worker locks and move dangling `sending`/
        `submitting` jobs into review states so nothing is silently retried."""
        with self._lock:
            self._conn.execute("UPDATE jobs SET locked_by=NULL, locked_at=NULL")
            self._conn.commit()

    # -- snapshots & provider ------------------------------------------------
    def save_snapshot(self, job_id: str, **fields: Any) -> None:
        cols = ["job_id"] + list(fields.keys())
        placeholders = ",".join("?" * len(cols))
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO snapshots({','.join(cols)}) VALUES({placeholders})",
                (job_id, *fields.values()),
            )
            self._conn.commit()

    def get_snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snapshots WHERE job_id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_provider(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            existing = self._conn.execute(
                "SELECT job_id FROM provider WHERE job_id=?", (job_id,)
            ).fetchone()
            if existing:
                sets = ",".join(f"{k}=?" for k in fields)
                self._conn.execute(
                    f"UPDATE provider SET {sets} WHERE job_id=?",
                    (*fields.values(), job_id),
                )
            else:
                cols = ["job_id"] + list(fields.keys())
                self._conn.execute(
                    f"INSERT INTO provider({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))})",
                    (job_id, *fields.values()),
                )
            self._conn.commit()

    def get_provider(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM provider WHERE job_id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    # -- pending prompts -----------------------------------------------------
    def add_pending_prompt(
        self, user_id: int, chat_id: int, thread_id: int | None, purpose: str,
        job_id: str | None = None,
    ) -> int:
        with self._lock:
            # Supersede any earlier open request of the same purpose for this user.
            self._conn.execute(
                "UPDATE pending_prompts SET consumed=1 WHERE user_id=? AND purpose=? AND consumed=0",
                (user_id, purpose),
            )
            cur = self._conn.execute(
                """INSERT INTO pending_prompts(user_id, chat_id, thread_id, job_id, purpose, created_at, consumed)
                   VALUES(?,?,?,?,?,?,0)""",
                (user_id, chat_id, thread_id, job_id, purpose, time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def take_pending_prompt(self, user_id: int, chat_id: int, thread_id: int | None) -> dict[str, Any] | None:
        """Atomically consume the most recent open prompt request for this user in
        this chat/thread. Returns its row or None."""
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM pending_prompts
                    WHERE user_id=? AND chat_id=? AND consumed=0
                      AND (thread_id IS ? OR thread_id=?)
                    ORDER BY created_at DESC LIMIT 1""",
                (user_id, chat_id, thread_id, thread_id),
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE pending_prompts SET consumed=1 WHERE id=?", (row["id"],)
            )
            self._conn.commit()
            return dict(row)

    # -- caption jobs --------------------------------------------------------
    def create_caption_job(
        self, output_job_id: str, source_receipt: str | None, destination_topic: str | None
    ) -> int | None:
        with self._lock:
            try:
                cur = self._conn.execute(
                    """INSERT INTO caption_jobs(output_job_id, source_receipt, destination_topic, state, created_at)
                       VALUES(?,?,?, 'queued', ?)""",
                    (output_job_id, source_receipt, destination_topic, time.time()),
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None  # a queued caption job already exists for this output

    def set_caption_state(self, caption_id: int, state: str, caption: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE caption_jobs SET state=?, caption=COALESCE(?, caption) WHERE id=?",
                (state, caption, caption_id),
            )
            self._conn.commit()

    # -- notices -------------------------------------------------------------
    def add_notice(self, chat_id: int | None, thread_id: int | None, text: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO notices(chat_id, thread_id, text, delivery_state, created_at) VALUES(?,?,?,?,?)",
                (chat_id, thread_id, text, "pending", time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def pending_notices(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notices WHERE delivery_state='pending' ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_notice_delivered(self, notice_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE notices SET delivery_state='delivered' WHERE id=?", (notice_id,)
            )
            self._conn.commit()

    # -- key/value state -----------------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, time.time()),
            )
            self._conn.commit()

    # -- reporting -----------------------------------------------------------
    def counts_by_state(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) c FROM jobs GROUP BY state"
            ).fetchall()
        return {r["state"]: r["c"] for r in rows}

    def get_last_update_id(self) -> int:
        return int(self.kv_get("last_update_id", "0") or 0)

    def set_last_update_id(self, update_id: int) -> None:
        self.kv_set("last_update_id", str(update_id))
