from __future__ import annotations

import sqlite3

from app.db import Database


def test_projects_get_archived_column_and_order(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    Database(db_path)
    cols = {row[1] for row in sqlite3.connect(db_path).execute("pragma table_info(projects)")}
    assert "archived_at" in cols

    db = Database(db_path)
    now = "2026-11-10T00:00:00+00:00"
    for pid, updated in (("p_old", "2026-11-01T00:00:00+00:00"), ("p_new", "2026-11-02T00:00:00+00:00")):
        db.execute(
            "INSERT INTO projects (id, name, topic, venue, domain, constraints, language, skills_json, created_at, updated_at) "
            "VALUES (?, ?, 't', 'ACL', 'd', 'c', 'zh-CN', '[]', ?, ?)",
            (pid, pid, now, updated),
        )
    db.execute("UPDATE projects SET archived_at = ? WHERE id = 'p_old'", (now,))

    rows = db.query_all(
        "SELECT id FROM projects ORDER BY (archived_at IS NOT NULL) ASC, updated_at DESC"
    )
    assert [row["id"] for row in rows] == ["p_new", "p_old"]
