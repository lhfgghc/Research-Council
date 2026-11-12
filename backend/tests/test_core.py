from __future__ import annotations

import asyncio
import json

import pytest

from app.db import Database, utc_now
from app.services import artifacts as artifacts_module
from app.services.artifacts import ArtifactStore
from app.services.engine import document_completion_issues, extract_json, strip_markdown_fence
from app.services.model_gateway import BudgetExhausted, ModelGateway


def test_extract_json_from_fenced_and_noisy_output() -> None:
    assert extract_json('{"ok": true}') == {"ok": True}
    assert extract_json('```json\n{"ok": true}\n```') == {"ok": True}
    assert extract_json('说明文字 {"selected": [1, 2]} 后续')["selected"] == [1, 2]
    with pytest.raises(ValueError):
        extract_json("not json")
    with pytest.raises(ValueError):
        extract_json('{"method_overview": broken, "components": [{"name": "component"}]}')
    with pytest.raises(ValueError):
        extract_json('[{"name": "component"}]')


def test_markdown_fence_stripping() -> None:
    assert strip_markdown_fence("```markdown\n# Title\nBody\n```") == "# Title\nBody"
    assert strip_markdown_fence("```\n# Title\n```") == "# Title"


def test_incomplete_document_is_detected() -> None:
    partial = "# Idea\n## 1. Introduction\nText\n## 2. Related Work\nText\n## 3. Method\nText"
    missing = document_completion_issues(partial)
    assert "## 4. Experiment" in missing
    complete = "\n".join(
        [
            "## Abstract",
            "## 1. Introduction",
            "## 2. Related Work",
            "## 3. Method",
            "## 4. Experiment",
            "## 5. Expected Results",
            "## 6. Discussion and Limitations",
            "## 7. Reproducibility Plan",
        ]
    )
    assert document_completion_issues(complete) == []


def test_budget_is_atomically_hard_capped(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
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
        ("r1", "p1", "GENERATE", "RUNNING", "P0", "项目定义", 1, 3, 1, 1, "test", "zh-CN", "{}", "{}", now, now),
    )
    gateway = ModelGateway(db)
    asyncio.run(gateway._reserve("r1"))
    run = db.query_one("SELECT budget_used FROM runs WHERE id = 'r1'")
    assert run["budget_used"] == 1
    with pytest.raises(BudgetExhausted):
        asyncio.run(gateway._reserve("r1"))
    assert db.query_one("SELECT budget_used FROM runs WHERE id = 'r1'")["budget_used"] == 1


def test_versions_never_overwrite(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(artifacts_module, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(artifacts_module, "DATA_DIR", tmp_path)
    db = Database(tmp_path / "versions.db")
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
        ("r1", "p1", "GENERATE", "COMPLETED", "DONE", "完成", 10, 3, 1, 1, "test", "zh-CN", "{}", "{}", now, now),
    )
    store = ArtifactStore(db)
    idea = store.create_idea(project_id="p1", run_id="r1", title="Test Idea", summary="Summary", index=0)
    first = store.save_version(
        idea=idea, project_id="p1", document="# V1\nOriginal", manifest={"status": "DRAFT"}
    )
    second = store.save_version(
        idea=idea, project_id="p1", document="# V2\nRevised", manifest={"status": "NEEDS_LITERATURE"}
    )
    assert first["version"] == 1
    assert second["version"] == 2
    assert store.read_relative_text(first["path"]).startswith("# V1")
    versions = db.query_all("SELECT version FROM idea_versions WHERE idea_id = ? ORDER BY version", (idea["id"],))
    assert [item["version"] for item in versions] == [1, 2]

    db.execute(
        "INSERT INTO runs (id, project_id, mode, status, phase, phase_label, budget_max, candidate_count, "
        "idea_count, team_rounds, model, language, config_json, state_json, created_at, updated_at) "
        "SELECT 'r2', project_id, mode, status, phase, phase_label, budget_max, candidate_count, idea_count, "
        "team_rounds, model, language, config_json, state_json, created_at, updated_at FROM runs WHERE id = 'r1'"
    )
    other = store.create_idea(project_id="p1", run_id="r2", title="Test Idea", summary="", index=0)
    assert store.create_idea(project_id="p1", run_id="r2", title="Renamed", summary="", index=0) == other
    original = store.save_version(
        idea=other, project_id="p1", document="# Different", manifest={}, operation_key="r2",
    )
    retry = store.save_version(
        idea=other, project_id="p1", document="# Retried", manifest={}, operation_key="r2",
    )
    assert original == retry
    assert original["path"] != first["path"]
    assert store.read_relative_text(original["path"]) == "# Different\n"
    assert store.read_relative_text(first["path"]).startswith("# V1")


def test_salvage_json_handles_trailing_commas_and_raw_newlines() -> None:
    from app.services.engine import extract_json

    raw = '{"title": "示例", "items": ["a", "b",], "note": "第一行\n第二行"}'
    parsed = extract_json(raw)
    assert parsed["title"] == "示例"
    assert parsed["items"] == ["a", "b"]
    assert "\n" in parsed["note"]

    fenced = "```json\n{\"a\": 1, \"list\": [1, 2,]}\n```"
    assert extract_json(fenced)["a"] == 1
