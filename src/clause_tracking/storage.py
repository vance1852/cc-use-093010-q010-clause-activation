"""条款协商与生效跟踪服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('secretariat', 'delegate', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    participant_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    added_by TEXT NOT NULL REFERENCES users(user_id),
    added_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, participant_id)
);

CREATE TABLE IF NOT EXISTS authorizations (
    authorization_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    delegate_id TEXT NOT NULL REFERENCES users(user_id),
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    revoked_at TEXT,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    CHECK (valid_from < valid_until),
    FOREIGN KEY (instrument_id, participant_id) REFERENCES participants(instrument_id, participant_id)
);

CREATE TABLE IF NOT EXISTS authorization_capabilities (
    authorization_id TEXT NOT NULL REFERENCES authorizations(authorization_id),
    capability TEXT NOT NULL CHECK (capability IN ('revise', 'vote', 'sign', 'reserve', 'fulfill')),
    PRIMARY KEY (authorization_id, capability)
);

CREATE TABLE IF NOT EXISTS text_versions (
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    parent_version_no INTEGER,
    origin TEXT NOT NULL CHECK (origin IN ('baseline', 'merge', 'editorial')),
    origin_revision_id TEXT,
    note TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, version_no),
    FOREIGN KEY (instrument_id, parent_version_no) REFERENCES text_versions(instrument_id, version_no)
);

CREATE TABLE IF NOT EXISTS clause_versions (
    instrument_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    clause_id TEXT NOT NULL,
    clause_version INTEGER NOT NULL CHECK (clause_version > 0),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    clause_sha256 TEXT NOT NULL CHECK (length(clause_sha256) = 64),
    PRIMARY KEY (instrument_id, version_no, clause_id),
    FOREIGN KEY (instrument_id, version_no) REFERENCES text_versions(instrument_id, version_no)
);

CREATE TABLE IF NOT EXISTS revisions (
    revision_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    base_version_no INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'merged', 'withdrawn')),
    participant_id TEXT NOT NULL,
    proposed_by TEXT NOT NULL REFERENCES users(user_id),
    authorization_id TEXT NOT NULL REFERENCES authorizations(authorization_id),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    merged_into_version_no INTEGER,
    merged_at TEXT,
    FOREIGN KEY (instrument_id, base_version_no) REFERENCES text_versions(instrument_id, version_no)
);

CREATE TABLE IF NOT EXISTS revision_changes (
    revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
    clause_id TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK (change_type IN ('add', 'amend', 'remove')),
    title TEXT,
    body TEXT,
    PRIMARY KEY (revision_id, clause_id)
);

CREATE TABLE IF NOT EXISTS translations (
    translation_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    clause_id TEXT NOT NULL,
    clause_sha256 TEXT NOT NULL CHECK (length(clause_sha256) = 64),
    language TEXT NOT NULL,
    text TEXT NOT NULL,
    translation_sha256 TEXT NOT NULL CHECK (length(translation_sha256) = 64),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (instrument_id, clause_id, clause_sha256, language)
);

CREATE TABLE IF NOT EXISTS acceptance_events (
    acceptance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    authorization_id TEXT NOT NULL REFERENCES authorizations(authorization_id),
    signed_by TEXT NOT NULL REFERENCES users(user_id),
    signed_at TEXT NOT NULL,
    FOREIGN KEY (instrument_id, participant_id) REFERENCES participants(instrument_id, participant_id),
    FOREIGN KEY (instrument_id, version_no) REFERENCES text_versions(instrument_id, version_no)
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    clause_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    authorization_id TEXT NOT NULL REFERENCES authorizations(authorization_id),
    declared_by TEXT NOT NULL REFERENCES users(user_id),
    declared_at TEXT NOT NULL,
    withdrawn_by TEXT REFERENCES users(user_id),
    withdrawn_at TEXT,
    withdrawal_authorization_id TEXT REFERENCES authorizations(authorization_id),
    FOREIGN KEY (instrument_id, participant_id) REFERENCES participants(instrument_id, participant_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_reservation_per_clause
ON reservations(instrument_id, participant_id, clause_id)
WHERE withdrawn_at IS NULL;

CREATE TABLE IF NOT EXISTS conditions (
    condition_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    clause_id TEXT NOT NULL,
    description TEXT NOT NULL,
    applies_to TEXT NOT NULL,
    owner_participant_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS condition_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL REFERENCES conditions(condition_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('fulfill', 'revoke')),
    evidence_ref TEXT NOT NULL,
    note TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(user_id),
    authorization_id TEXT REFERENCES authorizations(authorization_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clause_force (
    instrument_id TEXT NOT NULL,
    clause_id TEXT NOT NULL,
    clause_sha256 TEXT NOT NULL CHECK (length(clause_sha256) = 64),
    declared_by TEXT NOT NULL REFERENCES users(user_id),
    declared_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, clause_id)
);

CREATE TABLE IF NOT EXISTS vote_rounds (
    vote_round_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'sealed')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    opened_by TEXT NOT NULL REFERENCES users(user_id),
    opened_at TEXT NOT NULL,
    sealed_by TEXT REFERENCES users(user_id),
    sealed_at TEXT,
    support_count INTEGER,
    object_count INTEGER,
    abstain_count INTEGER,
    UNIQUE (instrument_id, version_no),
    FOREIGN KEY (instrument_id, version_no) REFERENCES text_versions(instrument_id, version_no)
);

CREATE TABLE IF NOT EXISTS ballots (
    vote_round_id TEXT NOT NULL REFERENCES vote_rounds(vote_round_id),
    participant_id TEXT NOT NULL,
    choice TEXT NOT NULL CHECK (choice IN ('support', 'object', 'abstain')),
    authorization_id TEXT NOT NULL REFERENCES authorizations(authorization_id),
    cast_by TEXT NOT NULL REFERENCES users(user_id),
    cast_at TEXT NOT NULL,
    PRIMARY KEY (vote_round_id, participant_id)
);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    clause_id TEXT NOT NULL,
    description TEXT NOT NULL,
    owner_participant_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'completed', 'cancelled')),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    completed_by TEXT REFERENCES users(user_id),
    completed_at TEXT,
    completion_evidence_ref TEXT,
    completion_authorization_id TEXT REFERENCES authorizations(authorization_id),
    cancelled_by TEXT REFERENCES users(user_id),
    cancelled_at TEXT,
    cancel_reason TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "instruments", "participants",
    "authorizations", "authorization_capabilities",
    "text_versions", "clause_versions", "revisions", "revision_changes",
    "translations", "acceptance_events", "reservations",
    "conditions", "condition_events", "clause_force",
    "vote_rounds", "ballots", "actions", "audit_events",
})


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=check_same_thread)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
