from __future__ import annotations

import asyncio
import json

from app.config import ModelCallSettings
from app.db import Database, utc_now
from app.services.context import ContextManager


def _settings() -> ModelCallSettings:
    return ModelCallSettings(
        id="test",
        model="test-model",
        context_window_tokens=8000,
        max_input_tokens=4000,
        context_compression_trigger=0.3,
        context_compression_hard=0.95,
    )


def _seed(tmp_path):
    db = Database(tmp_path / "repeat.db")
    now = utc_now()
    db.execute(
        "INSERT INTO projects (id, name, topic, venue, domain, constraints, language, skills_json, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("p1", "Project", "Topic", "ACL", "NLP", "", "zh-CN", "[]", now, now),
    )
    db.execute(
        "INSERT INTO runs (id, project_id, mode, status, phase, phase_label, budget_max, candidate_count, "
        "idea_count, team_rounds, model, language, config_json, state_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "p1", "GENERATE", "RUNNING", "P2", "团队讨论", 20, 3, 1, 1,
         "test-model", "zh-CN", "{}", "{}", now, now),
    )
    state = {
        "phase": "P2",
        "agenda": "验证重复摘要",
        "recent_messages": [],
        "context_summaries": {},
        "decision_digest": [],
    }
    return db, state


def _insert_messages(db: Database, start: int, count: int) -> None:
    now = utc_now()
    for index in range(start, start + count):
        db.execute(
            "INSERT INTO messages (run_id, role, role_label, message_type, content, structured_json, scope, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "domain_researcher_1", "领域研究者 A", "speech",
             f"第 {index} 条讨论。" + "这是一段足够长的实质性内容，用于触发压缩。" * 12,
             "{}", "RUN", now),
        )


def test_summaries_do_not_reprocess_covered_messages(tmp_path) -> None:
    db, state = _seed(tmp_path)
    _insert_messages(db, 1, 12)
    manager = ContextManager(db, "r1", state, _settings())

    async def summarize(existing: str, messages: list[dict]) -> dict:
        return {"summary": f"覆盖 {len(messages)} 条消息的摘要。", "decisions": [], "rejected": [], "open_questions": [], "evidence": [], "next_actions": []}

    first = asyncio.run(manager.prepare(purpose="first", base_tokens=0, summarize=summarize))
    assert first.compressed
    first_end = db.query_one("SELECT covered_end FROM run_summaries WHERE id = ?", (first.summary_id,))["covered_end"]
    assert first_end >= 1

    _insert_messages(db, 13, 4)
    second = asyncio.run(manager.prepare(purpose="second", base_tokens=0, summarize=summarize))
    assert second.compressed
    second_row = db.query_one("SELECT * FROM run_summaries WHERE id = ?", (second.summary_id,))
    assert second_row["covered_start"] > first_end
    all_covered = json.loads(second_row["covered_ids_json"])
    assert len(all_covered) == len(set(all_covered))
    assert set(range(1, first_end + 1)).issubset(set(all_covered))
    assert second_row["covered_start"] not in set(range(1, first_end + 1))
