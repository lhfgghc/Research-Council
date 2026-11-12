from __future__ import annotations

import asyncio
import json

from app.config import AppSettings, SettingsStore
from app.db import Database, utc_now
from app.services import artifacts as artifacts_module
from app.services.artifacts import ArtifactStore
from app.services.engine import RunEngine
from app.services.events import EventBus
from app.services.prompts import writer_initial_part_messages
from app.services.skills import DEFAULT_DISCUSSION_SKILLS, DEFAULT_WRITING_SKILLS, skill_kind_for


def test_phase_skill_profiles_switch_at_writing_stages() -> None:
    assert skill_kind_for("P0") == "discussion"
    assert skill_kind_for("P2") == "discussion"
    assert skill_kind_for("P3", "method") == "discussion"
    assert skill_kind_for("P3", "experiment") == "discussion"
    assert skill_kind_for("P3", "gate") == "writing"
    assert skill_kind_for("P3", "draft_a") == "writing"
    assert skill_kind_for("P3", "revise_c") == "writing"
    assert skill_kind_for("P4") == "writing"
    assert skill_kind_for("R3A") == "writing"


def test_default_skill_lists_cover_discussion_and_writing() -> None:
    settings = AppSettings()
    assert settings.default_skills == DEFAULT_DISCUSSION_SKILLS
    assert settings.writing_skills == DEFAULT_WRITING_SKILLS
    assert "ccf-idea-optimizer" in settings.default_skills
    assert "ccf-idea-reviewer" in settings.default_skills
    assert "ccf-experiment-designer" in settings.default_skills
    assert "ccf-paper-writer" in settings.writing_skills
    assert "ccf-paper-reviewer" in settings.writing_skills


def test_writer_front_part_is_a_paper_not_a_plan() -> None:
    messages = writer_initial_part_messages(
        {"name": "Project", "topic": "Topic", "venue": "ACL", "language": "zh-CN"},
        {"language": "zh-CN", "idea_count": 1, "candidate_count": 1, "budget_max": 10},
        "",
        {"title": "Test", "consensus": "t"},
        {"method_overview": "m"},
        {"claims": []},
        "A",
    )
    task = messages[-1]["content"]
    assert "## Abstract" in task
    assert "## 1. Introduction" in task
    assert "## 2. Related Work" in task
    assert "## Metadata" not in task
    assert "## One-Sentence Thesis" not in task


def test_engine_loads_discussion_and_writing_bundles(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(artifacts_module, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(artifacts_module, "DATA_DIR", tmp_path)
    skill_root = tmp_path / "skills"
    for name, text in (("demo-discussion", "DISCUSSION SKILL BODY"), ("demo-writing", "WRITING SKILL BODY")):
        folder = skill_root / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: demo\n---\n{text}\n", encoding="utf-8")
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "api_key": "test",
                "skills_dirs": [str(skill_root)],
                "default_skills": ["demo-discussion"],
                "writing_skills": ["demo-writing"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db = Database(tmp_path / "skills.db")
    now = utc_now()
    db.execute(
        "INSERT INTO projects (id, name, topic, venue, domain, constraints, language, skills_json, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("p1", "Project", "Topic", "ACL", "NLP", "", "zh-CN", "[]", now, now),
    )
    db.execute(
        "INSERT INTO runs (id, project_id, mode, status, phase, phase_label, budget_max, budget_used, "
        "candidate_count, idea_count, team_rounds, model, language, config_json, state_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "p1", "GENERATE", "RUNNING", "P2", "团队讨论", 30, 0, 2, 1, 1, "test", "zh-CN", "{}", '{"phase":"P2","selected":[{"stage":"draft_a"}]}', now, now),
    )
    engine = RunEngine(
        run_id="r1",
        database=db,
        settings_store=SettingsStore(settings_path),
        artifacts=ArtifactStore(db),
        events=EventBus(db),
        gateway=None,  # type: ignore[arg-type]
    )
    asyncio.run(engine._prepare())

    assert "DISCUSSION SKILL BODY" in engine.skill_bundles["discussion"]
    assert "WRITING SKILL BODY" in engine.skill_bundles["writing"]
    (skill_root / "demo-writing" / "SKILL.md").write_text("NEW BODY", encoding="utf-8")
    asyncio.run(engine._prepare())
    assert "WRITING SKILL BODY" in engine.skill_bundles["writing"]
    assert "NEW BODY" not in engine.skill_bundles["writing"]
    assert "DISCUSSION SKILL BODY" in engine.skills_bundle
    engine.state["phase"] = "P3"
    engine.state["selected"] = [{"stage": "draft_b"}]
    engine._activate_skills()
    assert "WRITING SKILL BODY" in engine.skills_bundle
