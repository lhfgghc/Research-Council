from __future__ import annotations

import asyncio
from typing import Any

from app.config import ModelProfile, SettingsStore
from app.db import Database, utc_now
from app.services import artifacts as artifacts_module
from app.services.artifacts import ArtifactStore
from app.services.engine import RunEngine
from app.services.events import EventBus


def _seed(db: Database, idea_count: int = 2) -> None:
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
        (
            "r1",
            "p1",
            "GENERATE",
            "RUNNING",
            "P2",
            "团队讨论",
            60,
            0,
            2,
            idea_count,
            1,
            "test",
            "zh-CN",
            "{}",
            "{}",
            now,
            now,
        ),
    )


def _engine(tmp_path, monkeypatch, idea_count: int = 2) -> RunEngine:
    monkeypatch.setattr(artifacts_module, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(artifacts_module, "DATA_DIR", tmp_path)
    db = Database(tmp_path / "floor.db")
    _seed(db, idea_count)
    engine = RunEngine(
        run_id="r1",
        database=db,
        settings_store=None,  # type: ignore[arg-type]
        artifacts=ArtifactStore(db),
        events=EventBus(db),
        gateway=None,  # type: ignore[arg-type]
    )
    engine.run = db.query_one("SELECT * FROM runs WHERE id = ?", ("r1",))
    engine.project = db.query_one("SELECT * FROM projects WHERE id = ?", ("p1",))
    engine.state = {
        "phase": "P2",
        "candidates": [
            {"id": "candidate_1", "title": "Idea A", "one_sentence": "A", "gap": "g", "mechanism": "m"},
            {"id": "candidate_2", "title": "Idea B", "one_sentence": "B", "gap": "g", "mechanism": "m"},
        ],
        "review_notes": [],
        "objections": [],
        "selected": [],
        "recent_messages": [],
        "decision_digest": [],
        "context_summaries": {},
        "user_inputs": [],
        "evidence": [],
        "research_turns": [],
        "idea_states": {},
        "decision_log": [],
    }
    return engine


def test_five_models_all_speak_in_both_floors_at_the_minimum_budget(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch, idea_count=1)
    roster = [f"speaker_{index}" for index in range(5)]
    engine.settings_store = SettingsStore(tmp_path / "settings.json")
    engine.settings_store.save({
        "discussion_models": roster,
        "model_profiles": [ModelProfile(id=key, model=key).model_dump() for key in roster],
        "role_models": {"pi": [roster[0]], "method_architect": roster[:2], "experiment_designer": [roster[2]]},
    })
    engine.config["discussion_models"] = roster
    engine.run["team_rounds"] = 2
    engine.db.execute("UPDATE runs SET budget_max=29,budget_used=4 WHERE id='r1'")

    async def call(**kwargs: Any) -> dict[str, Any]:
        engine.db.execute("UPDATE runs SET budget_used=budget_used+1 WHERE id='r1'")
        if kwargs["contract"] == "selection":
            return {"selected": [{"candidate_id": "candidate_1"}]}
        if kwargs["contract"] == "decision":
            return {"thesis": "decision", "core_mechanism": "mechanism"}
        return {"stance": "pass", "speech": "no new judgment"}

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    engine._call_json = call  # type: ignore[method-assign]
    engine._prepare_context = noop  # type: ignore[method-assign]
    engine._run_literature_queries = noop  # type: ignore[method-assign]
    asyncio.run(engine._phase_p2())

    keys = [f"model|{member}" for member in roster]
    assert engine.state["discussion"]["counts"] == dict.fromkeys(keys, 2)
    assert engine.state["debate_floors"]["candidate_1"]["counts"] == dict.fromkeys(keys, 1)
    assert engine._remaining_calls() == 8
    assert engine._profile_ids_for("method_architect") == [roster[0]]
    assert engine.state["dispatch"]["history"][0]["roster"][4]["key"] == keys[4]


def test_p2_discusses_then_debates_and_decides_every_selected_idea(
    tmp_path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch, idea_count=2)
    calls: list[str] = []
    debate_turns = 0

    async def fake_call_json(**kwargs: Any) -> dict[str, Any]:
        nonlocal debate_turns
        purpose = str(kwargs["purpose"])
        calls.append(purpose)
        if purpose.startswith("P2.1 团队讨论"):
            return {
                "stance": "contribute",
                "information_gain": "medium",
                "referenced_ideas": ["candidate_1"],
                "objections": [],
                "responses": [],
                "proposed_actions": [],
                "speech": "review",
            }
        if purpose.startswith("P2.2 路线取舍"):
            return {
                "selected": [{"candidate_id": "candidate_1"}, {"candidate_id": "candidate_2"}],
                "backup_candidate_id": "",
                "assessment": [],
                "rejected": [],
                "speech": "selected",
            }
        if purpose.startswith("P2.3 辩论"):
            debate_turns += 1
            if debate_turns % 2 == 1:
                return {
                    "stance": "challenge",
                    "information_gain": "medium",
                    "attacks": [{"target": "mechanism", "why_it_fails": "w", "required_fix": "f"}],
                    "objections": [{"issue": "mechanism fails", "severity": "blocking"}],
                    "speech": "challenge",
                }
            return {
                "stance": "concede",
                "information_gain": "medium",
                "responses": [
                    {"objection_id": "obj_1", "decision": "accepted", "why": "fixed"}
                ] if debate_turns <= 2 else [
                    {"objection_id": "obj_2", "decision": "accepted", "why": "fixed"}
                ],
                "revised_plan": "revised plan",
                "speech": "concede",
            }
        if purpose.startswith("P2.4 研究决策"):
            prompt = kwargs["messages"][-1]["content"]
            assert "当前讨论形成的方案" in prompt and "revised plan" in prompt
            return {
                "thesis": "decision thesis",
                "core_mechanism": "decision mechanism",
                "contributions": ["contribution"],
                "hypotheses": [],
                "key_experiments": ["experiment"],
                "unresolved": [],
                "decision_rationale": "why",
                "speech": "decided",
            }
        raise AssertionError(f"unexpected purpose: {purpose}")

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def no_literature(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    engine._call_json = fake_call_json  # type: ignore[method-assign]
    engine._prepare_context = noop  # type: ignore[method-assign]
    engine._run_literature_queries = no_literature  # type: ignore[method-assign]
    engine._profile_ids_for = (  # type: ignore[method-assign]
        lambda role: ["m1", "m2"] if role == "method_architect" else []
    )
    engine._profile_label = (  # type: ignore[method-assign]
        lambda profile_id: {"m1": "Model A", "m2": "Model B"}.get(profile_id, profile_id)
    )

    asyncio.run(engine._phase_p2())

    assert len([call for call in calls if call.startswith("P2.1 团队讨论")]) == 3
    assert engine.state["selection_done"] is True
    assert len(engine.state["selected"]) == 2
    assert engine.state["discussion"]["closed"] is True
    assert engine.state["discussion"]["close_reason"]
    assert len(engine.state["debate_floors"]) == 2
    assert len(engine.state["idea_states"]) == 2
    for idea in engine.state["selected"]:
        assert idea["consensus"]["thesis"] == "decision thesis"
        assert idea["decision"]["core_mechanism"] == "decision mechanism"
    assert engine.state["phase"] == "P3"
    assert len(engine.state["decision_log"]) == 2
    board = engine.state["dispatch"]
    assert board["active"] is None
    assert [item["stage"] for item in board["history"][:3]] == ["P2.1"] * 3
    assert all(len(item["roster"]) == 3 for item in board["history"][:3])
    debate = [item for item in board["history"] if item["stage"] == "P2.3" and item["turn"]]
    assert {item["idea_id"] for item in debate} == {"candidate_1", "candidate_2"}
    assert all(len(item["roster"]) == 2 and item["message_id"] for item in debate)
    for floor in engine.state["debate_floors"].values():
        assert sum(action["type"] == "DEVELOP" for action in floor["actions"]) == 1


def test_selection_marks_parked_and_rejected_candidates(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch, idea_count=1)
    engine.state["candidates"].append(
        {"id": "candidate_3", "title": "Idea C", "one_sentence": "C", "gap": "g", "mechanism": "m"}
    )

    async def fake_call_json(**kwargs: Any) -> dict[str, Any]:
        purpose = str(kwargs["purpose"])
        if purpose.startswith("P2.1 团队讨论"):
            return {
                "stance": "contribute",
                "information_gain": "medium",
                "referenced_ideas": ["candidate_1", "candidate_2", "candidate_3", "route_1"],
                "new_routes": [{
                    "kind": "fork", "parent_ids": ["candidate_1"], "title": "Idea A fork",
                    "one_sentence": "New claim", "mechanism": "Distinct mechanism",
                    "hypothesis_delta": "A new assumption", "discriminating_test": "Compare A to the fork",
                }],
                "objections": [],
                "responses": [],
                "speech": "review",
            }
        if purpose.startswith("P2.2 路线取舍"):
            return {
                "selected": [{"candidate_id": "route_1"}],
                "backup_candidate_id": "candidate_2",
                "rejected": [{"candidate_id": "candidate_3", "reason": "机制不成立"}],
                "speech": "selected",
            }
        if purpose.startswith("P2.3 辩论"):
            return {"stance": "pass", "information_gain": "none", "speech": "pass"}
        if purpose.startswith("P2.4 研究决策"):
            return {"thesis": "t", "core_mechanism": "m", "unresolved": [], "speech": "decided"}
        raise AssertionError(f"unexpected purpose: {purpose}")

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def no_literature(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    engine._call_json = fake_call_json  # type: ignore[method-assign]
    engine._prepare_context = noop  # type: ignore[method-assign]
    engine._run_literature_queries = no_literature  # type: ignore[method-assign]
    engine._profile_ids_for = (  # type: ignore[method-assign]
        lambda role: ["m1", "m2"] if role == "method_architect" else []
    )
    engine._profile_label = lambda profile_id: profile_id  # type: ignore[method-assign]

    asyncio.run(engine._phase_p2())

    statuses = {item["id"]: item["status"] for item in engine.state["candidates"]}
    assert statuses == {
        "candidate_1": "parked",
        "candidate_2": "backup",
        "candidate_3": "rejected",
        "route_1": "selected",
    }
    assert engine.state["backup_candidate"]["id"] == "candidate_2"
    assert engine.state["selected"][0]["parent_ids"] == ["candidate_1"]
