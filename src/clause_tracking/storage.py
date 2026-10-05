"""条款协商与生效跟踪的 SQLite 模式与事务辅助。"""

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

-- 对话与参与方 ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS dialogues (
    dialogue_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    participant_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dialogue_participants (
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    joined_at TEXT NOT NULL,
    PRIMARY KEY (dialogue_id, participant_id)
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('secretariat', 'delegate', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- 代表授权（同一受权代表对同一参与方同时只能持有一份未撤销授权） --------

CREATE TABLE IF NOT EXISTS authorizations (
    authorization_id TEXT PRIMARY KEY,
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    delegate_id TEXT NOT NULL REFERENCES users(user_id),
    scope_json TEXT NOT NULL DEFAULT '[]',
    granted_at TEXT NOT NULL,
    granted_by TEXT NOT NULL REFERENCES users(user_id),
    revoked_at TEXT,
    revoked_by TEXT REFERENCES users(user_id),
    revoke_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_authorization
ON authorizations(delegate_id, participant_id)
WHERE revoked_at IS NULL;

-- 条款与版本链 -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS clauses (
    clause_id TEXT PRIMARY KEY,
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    clause_code TEXT NOT NULL,
    title TEXT NOT NULL,
    independent INTEGER NOT NULL DEFAULT 1 CHECK (independent IN (0, 1)),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (dialogue_id, clause_code)
);

CREATE TABLE IF NOT EXISTS clause_revisions (
    revision_id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    parent_revision_id TEXT REFERENCES clause_revisions(revision_id),
    kind TEXT NOT NULL CHECK (kind IN ('baseline', 'amendment')),
    language TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    superseded_by TEXT REFERENCES clause_revisions(revision_id),
    superseded_at TEXT,
    merged_at TEXT,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (clause_id, revision_no),
    UNIQUE (clause_id, content_sha256)
);

-- 翻译对应 ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS revision_translations (
    translation_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES clause_revisions(revision_id),
    language TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    correspondence_json TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    registered_at TEXT NOT NULL,
    UNIQUE (revision_id, language)
);

-- 修订冲突：互相冲突的修订不能同时并入基线 -------------------------------

CREATE TABLE IF NOT EXISTS revision_conflicts (
    revision_a_id TEXT NOT NULL REFERENCES clause_revisions(revision_id),
    revision_b_id TEXT NOT NULL REFERENCES clause_revisions(revision_id),
    reason TEXT NOT NULL,
    marked_by TEXT NOT NULL REFERENCES users(user_id),
    marked_at TEXT NOT NULL,
    PRIMARY KEY (revision_a_id, revision_b_id),
    CHECK (revision_a_id < revision_b_id)
);

-- 支持与保留意见 ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS revision_supports (
    revision_id TEXT NOT NULL REFERENCES clause_revisions(revision_id),
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    authorization_id TEXT NOT NULL REFERENCES authorizations(authorization_id),
    supported_by TEXT NOT NULL REFERENCES users(user_id),
    supported_at TEXT NOT NULL,
    PRIMARY KEY (revision_id, participant_id)
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES clause_revisions(revision_id),
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    note TEXT NOT NULL,
    authorization_id TEXT NOT NULL REFERENCES authorizations(authorization_id),
    recorded_by TEXT NOT NULL REFERENCES users(user_id),
    recorded_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdrawn_by TEXT REFERENCES users(user_id),
    withdraw_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_reservation
ON reservations(revision_id, participant_id)
WHERE withdrawn_at IS NULL;

-- 封存表决（同一条款同一修订只能封存一次；已封存表决事实不可变） --------

CREATE TABLE IF NOT EXISTS clause_seals (
    seal_id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    revision_id TEXT NOT NULL REFERENCES clause_revisions(revision_id),
    sealed_by TEXT NOT NULL REFERENCES users(user_id),
    sealed_at TEXT NOT NULL,
    ballot_json TEXT NOT NULL,
    participation INTEGER NOT NULL CHECK (participation >= 0),
    support_count INTEGER NOT NULL CHECK (support_count >= 0),
    reservation_count INTEGER NOT NULL DEFAULT 0 CHECK (reservation_count >= 0),
    threshold TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    UNIQUE (clause_id, revision_id)
);

CREATE TABLE IF NOT EXISTS ballot_votes (
    seal_id TEXT NOT NULL REFERENCES clause_seals(seal_id),
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    position TEXT NOT NULL CHECK (position IN ('support', 'reservation')),
    reservation_id TEXT REFERENCES reservations(reservation_id),
    PRIMARY KEY (seal_id, participant_id)
);

-- 前置条件与后续行动 -----------------------------------------------------

CREATE TABLE IF NOT EXISTS preconditions (
    precondition_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES clause_revisions(revision_id),
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    code TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'satisfied', 'waived')),
    responsible_participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    due_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    evidence_sha256 TEXT,
    evidence_summary TEXT,
    completed_by TEXT REFERENCES users(user_id),
    completed_at TEXT,
    UNIQUE (revision_id, participant_id, code)
);

CREATE TABLE IF NOT EXISTS follow_up_actions (
    action_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES clause_revisions(revision_id),
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    code TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'done', 'revoked')),
    responsible_participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    due_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    evidence_sha256 TEXT,
    evidence_summary TEXT,
    completed_by TEXT REFERENCES users(user_id),
    completed_at TEXT,
    revoked_by TEXT REFERENCES users(user_id),
    revoked_at TEXT,
    revoke_reason TEXT,
    UNIQUE (revision_id, participant_id, code)
);

-- 生效状态（条款—修订—参与方；独立条款可分别生效） ----------------------

CREATE TABLE IF NOT EXISTS effectiveness (
    revision_id TEXT NOT NULL REFERENCES clause_revisions(revision_id),
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    state TEXT NOT NULL CHECK (state IN ('accepted', 'in_force', 'terminated')),
    seal_id TEXT REFERENCES clause_seals(seal_id),
    accepted_at TEXT NOT NULL,
    effective_at TEXT,
    terminated_at TEXT,
    terminate_reason TEXT,
    updated_by TEXT NOT NULL REFERENCES users(user_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (revision_id, participant_id)
);

-- 只追加事件日志：条件完成、撤销等状态变化的可重放依据 ------------------

CREATE TABLE IF NOT EXISTS event_journal (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    basis_sha256 TEXT NOT NULL CHECK (length(basis_sha256) = 64)
);

CREATE INDEX IF NOT EXISTS event_journal_entity_idx
ON event_journal(entity_type, entity_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "dialogues", "participants", "dialogue_participants", "users",
    "authorizations", "clauses", "clause_revisions", "revision_translations",
    "revision_conflicts", "revision_supports", "reservations", "clause_seals",
    "ballot_votes", "preconditions", "follow_up_actions", "effectiveness",
    "event_journal",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
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
    """初始化全部表，重复执行不改变已有数据。"""

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
