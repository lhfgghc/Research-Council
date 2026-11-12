from __future__ import annotations

import asyncio
import json

from app.config import ModelCallSettings
from app.db import Database, utc_now
from app.services.context import (
    ContextManager,
    estimate_messages,
    estimate_tokens,
    fit_messages_to_budget,
    hard_trim_messages,
    mask_messages,
)


def _seed_run(db: Database, budget_max: int = 20) -> None:
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
        (
            "r1", "p1", "GENERATE", "RUNNING", "P1", "独立构思", budget_max, 3, 1, 1,
            "test-model", "zh-CN", "{}", "{}", now, now,
        ),
    )


def _seed_messages(db: Database, count: int = 12, scope: str = "RUN") -> None:
    now = utc_now()
    for index in range(count):
        content = (
            f"第 {index + 1} 条讨论：我们检查了候选方案在长上下文下的表现，"
            "并记录了一个需要保留的决策与相应理由。" + ("补充说明。" * 60)
        )
        db.execute(
            "INSERT INTO messages (run_id, role, role_label, message_type, content, structured_json, scope, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "domain_researcher_1", "领域研究者 A", "speech", content, "{}", scope, now),
        )


def _settings(**overrides) -> ModelCallSettings:
    values = {
        "id": "test", "model": "test-model",
        "context_window_tokens": 8000,
        "max_input_tokens": 4000,
        "context_compression_enabled": True,
        "context_compression_trigger": 0.3,
        "context_compression_hard": 0.95,
    }
    values.update(overrides)
    return ModelCallSettings(**values)


def test_token_estimate_is_conservative_for_chinese() -> None:
    chinese = "这是一个用于测试上下文压缩的中文句子。" * 20
    english = "This is an English sentence used for context compression tests. " * 20
    assert estimate_tokens(chinese) > len(chinese) / 2
    assert estimate_tokens(english) > 0
    assert estimate_tokens("") == 0


def test_mask_messages_drops_duplicates_and_noise() -> None:
    messages = [
        {"id": 1, "role": "A", "type": "speech", "content": "同意。"},
        {"id": 2, "role": "A", "type": "speech", "content": "这是一条足够长的实质性讨论。" * 6},
        {"id": 3, "role": "B", "type": "speech", "content": "另一条足够长的实质性讨论。" * 6},
        {"id": 4, "role": "PI", "type": "artifact", "content": "短文档"},
        {"id": 5, "role": "B", "type": "speech", "content": "这是一条足够长的实质性讨论。" * 6},
    ]
    masked = mask_messages(messages)
    assert [item["id"] for item in masked] == [2, 3, 4]


def test_context_manager_summarizes_and_records_provenance(tmp_path) -> None:
    db = Database(tmp_path / "context.db")
    _seed_run(db)
    _seed_messages(db, 12)
    state = {
        "phase": "P1",
        "agenda": "验证上下文压缩",
        "problem_frame": "长讨论需要自动压缩",
        "recent_messages": [],
        "context_summaries": {},
        "decision_digest": ["已选择保留决策台账"],
    }
    manager = ContextManager(db, "r1", state, _settings())

    async def summarize(existing: str, messages: list[dict]) -> dict:
        assert messages
        return {
            "summary": "团队已讨论候选方案的长上下文表现，并决定保留决策台账。",
            "decisions": ["保留决策台账", "压缩只作用于旧发言"],
            "rejected": ["删除全部历史"],
            "open_questions": ["压缩后是否仍能追溯"],
            "evidence": [],
            "next_actions": ["继续深化候选"],
        }

    snapshot = asyncio.run(
        manager.prepare(purpose="test", base_tokens=0, summarize=summarize)
    )
    assert snapshot.compressed is True
    assert snapshot.strategy == "llm_summary"
    assert snapshot.summary_id is not None
    summary = db.query_one("SELECT * FROM run_summaries WHERE id = ?", (snapshot.summary_id,))
    assert summary is not None
    assert "已决定" in summary["summary_text"]
    assert summary["covered_end"] > 0
    assert "保留决策台账" in state["context_summaries"]["RUN"]["text"]
    assert len(state["context_summaries"]["RUN"]["covered_ids"]) > 0
    remaining = manager.uncovered_messages()
    assert len(remaining) < 12
    assert snapshot.estimated_tokens < 4000


def test_context_manager_falls_back_when_summary_fails(tmp_path) -> None:
    db = Database(tmp_path / "fallback.db")
    _seed_run(db)
    _seed_messages(db, 12)
    state = {
        "phase": "P1",
        "agenda": "验证失败降级",
        "recent_messages": [],
        "context_summaries": {},
        "decision_digest": [],
    }
    manager = ContextManager(db, "r1", state, _settings())

    async def broken(existing: str, messages: list[dict]) -> dict:
        raise RuntimeError("summary model unavailable")

    snapshot = asyncio.run(
        manager.prepare(purpose="test", base_tokens=0, summarize=broken)
    )
    assert snapshot.compressed is True
    assert snapshot.strategy == "extractive_fallback"
    assert "抽取式" in state["context_summaries"]["RUN"]["text"]
    assert "summary model unavailable" in state["last_compression_error"]


def test_hard_trim_keeps_system_and_last_user_message() -> None:
    messages = [
        {"role": "system", "content": "system rules"},
        {"role": "user", "content": "old " * 2000},
        {"role": "assistant", "content": "old answer " * 2000},
        {"role": "user", "content": "new task " * 2000},
    ]
    trimmed = hard_trim_messages(messages, 1200)
    assert trimmed[0]["role"] == "system"
    assert trimmed[-1]["role"] == "user"
    assert "new task" in trimmed[-1]["content"]
    assert len(trimmed) <= 3


def test_fit_messages_truncates_middle_and_keeps_both_ends() -> None:
    head = "任务开头：请生成一份完整的科研方案。"
    tail = '输出格式：{"summary": "...", "experiment": "..."}'
    messages = [
        {"role": "system", "content": "system rules " * 100},
        {"role": "user", "content": head + ("中间载荷。" * 3000) + tail},
    ]
    fitted = fit_messages_to_budget(messages, 1800)
    assert fitted[0]["role"] == "system"
    content = fitted[-1]["content"]
    assert head in content
    assert tail in content
    assert "中间内容因单次输入预算被截断" in content
    assert len(content) < len(messages[-1]["content"])


def test_fit_messages_caps_a_large_task_payload() -> None:
    messages = [
        {"role": "system", "content": "system rules " * 200},
        {"role": "user", "content": "任务开头。" + ("大段方法与实验载荷。" * 6000) + "输出格式结束。"},
    ]
    fitted = fit_messages_to_budget(messages, 12000)
    assert estimate_messages(fitted) <= 12000
    assert "任务开头" in fitted[-1]["content"]
    assert "输出格式结束" in fitted[-1]["content"]
