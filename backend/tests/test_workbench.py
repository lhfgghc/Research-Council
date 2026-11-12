from __future__ import annotations

import asyncio

import httpx

from app.config import AppSettings, ModelProfile
from app.schemas import RunCreate
from app.services.model_gateway import response_events
from app.services.prompts import agenda_context
from app.services.run_budget import budget_preview
from app.services.workbench import accept_routes


def test_budget_includes_panel_synthesis_for_each_idea() -> None:
    settings = AppSettings(
        model_profiles=[ModelProfile(id=name, model=name) for name in ("a", "b", "c")],
        role_models={"method_architect": ["a", "b", "c"], "experiment_designer": ["a", "b", "c"]},
    )
    assert budget_preview(RunCreate(), settings)["full"] == 67
    brief = budget_preview(RunCreate(deliverable="idea_brief"), settings)
    assert brief["core"] == 22
    assert brief["full"] == 55
    assert brief["panels"] == 18
    assert budget_preview(RunCreate(team_rounds=7), settings)["error"]


def test_explicit_roster_budget_covers_all_models_without_panels() -> None:
    roster = list("abcde")
    settings = AppSettings(
        model_profiles=[ModelProfile(id=name, model=name) for name in roster],
        discussion_models=roster,
        role_models={"method_architect": ["a"], "experiment_designer": ["b"]},
    )
    preview = budget_preview(RunCreate(deliverable="idea_brief"), settings)
    assert (preview["team_models"], preview["team"], preview["debate"], preview["panels"]) == (5, 10, 15, 0)
    assert (preview["full"], preview["recommended"], preview["error"]) == (47, 56, "")
    assert "59" in budget_preview(RunCreate(deliverable="manuscript"), settings)["error"]


def test_discussion_can_register_distinct_routes_without_replacing_parents() -> None:
    state = {"candidates": [{"id": "candidate_1", "mechanism": "original"}]}
    proposal = {
        "kind": "fork", "parent_ids": ["candidate_1"], "title": "Branch",
        "one_sentence": "New claim", "mechanism": "A new mechanism",
        "hypothesis_delta": "Assume heterogeneity", "discriminating_test": "Compare stratified results",
    }
    routes = accept_routes(state, [proposal, proposal], 4)
    assert [item["id"] for item in state["candidates"]] == ["candidate_1", "route_1"]
    assert routes[0]["parent_ids"] == ["candidate_1"]
    assert routes[0]["source_turn"] == 4


def test_idea_context_isolates_unrelated_turns_and_objections() -> None:
    context = agenda_context({
        "phase": "P3", "current_idea_id": "a",
        "recent_messages": [
            {"scope": "P3:a", "content": "correct-turn"},
            {"scope": "P3:b", "content": "other-turn"},
        ],
        "objections": [
            {"idea_id": "a", "issue": "correct-issue"},
            {"idea_id": "b", "issue": "other-issue"},
        ],
        "literature_by_idea": {"a": ["correct-paper"]},
        "recent_literature": ["other-paper"],
    })
    assert all(text in context for text in ("correct-turn", "correct-issue", "correct-paper"))
    assert all(text not in context for text in ("other-turn", "other-issue", "other-paper"))


def test_gateway_accepts_sse_and_json_response_envelopes() -> None:
    async def collect(response: httpx.Response) -> list[dict]:
        return [event async for event in response_events(response)]

    event = {"choices": [{"message": {"content": "answer"}}]}
    assert asyncio.run(collect(httpx.Response(200, json=event))) == [event]
    sse = httpx.Response(200, content='data: {"usage":{"prompt_tokens":8}}\n\ndata: [DONE]\n')
    assert asyncio.run(collect(sse)) == [{"usage": {"prompt_tokens": 8}}]
