from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DB_PATH

_LOCK = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_mode = "unknown"
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            topic TEXT NOT NULL,
            venue TEXT NOT NULL,
            domain TEXT NOT NULL DEFAULT '',
            constraints TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT 'zh-CN',
            skills_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            mode TEXT NOT NULL DEFAULT 'GENERATE',
            status TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT 'P0',
            phase_label TEXT NOT NULL DEFAULT '项目定义',
            budget_max INTEGER NOT NULL,
            budget_used INTEGER NOT NULL DEFAULT 0,
            candidate_count INTEGER NOT NULL DEFAULT 6,
            idea_count INTEGER NOT NULL DEFAULT 3,
            team_rounds INTEGER NOT NULL DEFAULT 2,
            model TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'zh-CN',
            config_json TEXT NOT NULL DEFAULT '{}',
            state_json TEXT NOT NULL DEFAULT '{}',
            base_idea_id TEXT,
            feedback_id TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ended_at TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
            role TEXT NOT NULL,
            role_label TEXT NOT NULL,
            message_type TEXT NOT NULL DEFAULT 'speech',
            content TEXT NOT NULL,
            structured_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ideas (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            run_id TEXT NOT NULL REFERENCES runs(id),
            title TEXT NOT NULL,
            slug TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'DRAFT',
            current_version INTEGER NOT NULL DEFAULT 0,
            parent_idea_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS idea_versions (
            id TEXT PRIMARY KEY,
            idea_id TEXT NOT NULL REFERENCES ideas(id),
            version INTEGER NOT NULL,
            path TEXT NOT NULL,
            revision_path TEXT,
            manifest_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(idea_id, version)
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            idea_id TEXT NOT NULL REFERENCES ideas(id),
            base_version INTEGER NOT NULL,
            source_path TEXT NOT NULL,
            report_text TEXT NOT NULL,
            instructions TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_inputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS model_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
            role TEXT NOT NULL,
            purpose TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
            scope TEXT NOT NULL,
            covered_start INTEGER NOT NULL DEFAULT 0,
            covered_end INTEGER NOT NULL DEFAULT 0,
            covered_ids_json TEXT NOT NULL DEFAULT '[]',
            summary_text TEXT NOT NULL,
            schema_json TEXT NOT NULL DEFAULT '{}',
            token_count INTEGER NOT NULL DEFAULT 0,
            model TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS context_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
            phase TEXT NOT NULL,
            purpose TEXT NOT NULL,
            estimated_tokens INTEGER NOT NULL DEFAULT 0,
            budget_tokens INTEGER NOT NULL DEFAULT 0,
            ratio REAL NOT NULL DEFAULT 0,
            compressed INTEGER NOT NULL DEFAULT 0,
            strategy TEXT NOT NULL DEFAULT 'none',
            summary_id INTEGER,
            model_call_id INTEGER,
            actual_prompt_tokens INTEGER NOT NULL DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id, id);
        CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
        CREATE INDEX IF NOT EXISTS idx_calls_run ON model_calls(run_id);
        CREATE INDEX IF NOT EXISTS idx_summaries_run ON run_summaries(run_id, scope, id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_run ON context_snapshots(run_id, id);
        CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_ideas_project ON ideas(project_id, created_at);
        """
        with _LOCK, self.connection() as connection:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                self.journal_mode = "wal"
            except sqlite3.OperationalError:
                self.journal_mode = "delete"
                connection.execute("PRAGMA journal_mode = DELETE")
            connection.executescript(schema)
            self._ensure_column(connection, "messages", "scope", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(
                connection, "run_summaries", "covered_ids_json", "TEXT NOT NULL DEFAULT '[]'"
            )
            self._ensure_column(connection, "projects", "archived_at", "TEXT")
            self._ensure_column(connection, "ideas", "creation_index", "INTEGER")
            self._ensure_column(connection, "idea_versions", "operation_key", "TEXT")
            connection.execute(
                "UPDATE runs SET status = 'COMPLETED', error = NULL WHERE status = 'COMPLETED_WITH_WARNINGS'"
            )
            connection.execute("UPDATE ideas SET status = 'COMPLETED' WHERE status = 'NEEDS_LITERATURE'")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_ideas_creation "
                "ON ideas(run_id, creation_index)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_idea_versions_operation "
                "ON idea_versions(idea_id, operation_key)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages(run_id, scope, id)"
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(sql, params).fetchone()
        return dict(row) if row else None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with _LOCK, self.connection() as connection:
            cursor = connection.execute(sql, params)
            return cursor.rowcount

    def insert(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with _LOCK, self.connection() as connection:
            cursor = connection.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def update_json_state(self, run_id: str, state: dict[str, Any]) -> None:
        self.execute(
            "UPDATE runs SET state_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(state, ensure_ascii=False), utc_now(), run_id),
        )

    def writable_status(self) -> tuple[bool, str]:
        """Probe write access; retry once to absorb startup/checkpoint races."""
        last_error = ""
        for attempt in range(2):
            try:
                self.execute("CREATE TABLE IF NOT EXISTS __write_probe (id INTEGER)")
                self.execute("DROP TABLE IF EXISTS __write_probe")
                return True, ""
            except Exception as exc:
                last_error = str(exc)[:300]
                if attempt == 0:
                    time.sleep(0.15)
        return False, last_error


db = Database()
