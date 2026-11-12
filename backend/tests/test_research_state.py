from __future__ import annotations

from app.services.engine import RunEngine
from app.services.orchestrator import Action, FloorContext, Participant, plan_next_action
from app.services.research_state import (
    add_issue,
    apply_issue_response,
    record_decision,
    record_evidence,
    record_turn,
)


def _state() -> dict:
    return {"objections": [], "idea_states": {}, "evidence": [], "research_turns": []}


def test_blocking_issue_needs_evidence_or_revision_before_it_is_resolved() -> None:
    state = _state()
    issue = add_issue(
        state,
        {"issue": "机制与实验不一致", "type": "因果链", "severity": "blocking"},
        speaker_key="a",
        speaker_label="A",
        turn=1,
        stage="test",
        idea_id="idea_1",
        default_assignee="b",
    )
    assert issue is not None
    assert issue["status"] == "open"
    assert issue["id"] == "obj_1"
    assert "obj_1" in state["idea_states"]["idea_1"]["open_issue_ids"]

    contested = apply_issue_response(
        state, "obj_1", "accepted", "口头认可", speaker_key="b", revised=False
    )
    assert contested is not None
    assert contested["status"] == "contested"
    assert contested["attempt_count"] == 1

    resolved = apply_issue_response(
        state, "obj_1", "accepted", "已修订", speaker_key="b", revised=True
    )
    assert resolved is not None
    assert resolved["status"] == "resolved"
    assert "obj_1" not in state["idea_states"]["idea_1"]["open_issue_ids"]


def test_two_rejections_make_an_issue_unresolved() -> None:
    state = _state()
    add_issue(
        state,
        {"issue": "中心主张不成立", "type": "中心主张", "severity": "critical"},
        speaker_key="a",
        speaker_label="A",
        turn=1,
        stage="test",
        idea_id="idea_1",
        default_assignee="b",
    )
    first = apply_issue_response(state, "obj_1", "rejected", "不改", speaker_key="b")
    assert first is not None and first["status"] == "contested"
    assert first["assigned_to"] == "a"
    second = apply_issue_response(state, "obj_1", "rejected", "坚持", speaker_key="a")
    assert second is not None and second["status"] == "unresolved"


def test_issue_cannot_be_closed_with_invented_or_unrelated_evidence() -> None:
    state = _state()
    state["evidence"] = [
        {"id": "ev_other", "idea_ids": ["candidate_2"]},
        {"id": "ev_own", "idea_ids": ["candidate_1"]},
    ]
    issue = add_issue(
        state, {"issue": "Blocking", "severity": "blocking"},
        speaker_key="a", speaker_label="A", turn=1, stage="P2.1 讨论", idea_id="candidate_1",
    )
    assert issue is not None
    apply_issue_response(state, issue["id"], "accepted", "evidence", speaker_key="b",
                         evidence_ids=["ev_missing", "ev_other"])
    assert issue["status"] == "contested"
    assert issue["evidence_ids"] == []
    apply_issue_response(state, issue["id"], "accepted", "evidence", speaker_key="b",
                         evidence_ids=["ev_own"])
    assert issue["status"] == "resolved"


def test_debate_scopes_candidate_issues_and_preserves_other_pending_requests() -> None:
    engine = object.__new__(RunEngine)
    engine.state = {"candidates": [{"id": "candidate_1"}, {"id": "candidate_2"}]}
    speaker = Participant(key="a", role="method_architect", label="A")
    others = [
        Participant(key="b", role="experiment_designer", label="B"),
        Participant(key="c", role="domain_researcher_1", label="C"),
    ]
    labels = {item.label: item.key for item in [speaker, *others]}
    engine._add_result_issues(
        {"objections": [{"target": "candidate_2", "issue": "candidate 2 only", "respond_to": "B"}]},
        speaker, 1, "P2.1 讨论", "candidate_1", labels,
    )
    issue = engine.state["objections"][0]
    assert issue["idea_id"] == "candidate_2"
    assert issue["assigned_to"] == "b"
    engine._apply_result_responses(
        {"responses": [{"objection_id": issue["id"], "decision": "accepted"}]},
        others[0], 2, {"candidate_id": "candidate_1"},
    )
    assert issue["status"] == "open"

    floor = {}
    engine._advance_floor_requests(
        floor, Action("CONTINUE"),
        {"proposed_actions": [
            {"type": "COMPARE", "to": "B", "issue": "compare"},
            {"type": "REVISE", "to": "C", "issue": "revise"},
        ]},
        labels, speaker.key,
    )
    ctx = FloorContext("discussion", [speaker, *others], {}, {}, [], requests=floor["requests"], budget_left=10)
    selected = plan_next_action(ctx)
    assert selected is not None and selected.target == others[0]
    engine._advance_floor_requests(floor, selected, {}, labels, others[0].key)
    assert [(item["to"], item["action_type"]) for item in floor["requests"]] == [("c", "REVISE")]


def test_evidence_records_have_ids_levels_and_deduplicate() -> None:
    state = _state()
    first = record_evidence(
        state,
        [{"title": "Paper A", "abstract": "abs", "url": "https://example.org/a"}],
        query="query",
        idea_id="idea_1",
    )
    assert first[0]["id"] == "ev_1"
    assert first[0]["verification"] == "abstract_verified"
    duplicate = record_evidence(
        state,
        [{"title": "Paper A", "abstract": "abs", "url": "https://example.org/a"}],
        query="query 2",
        idea_id="idea_2",
    )
    assert duplicate[0]["id"] == "ev_1"
    assert "idea_2" in duplicate[0]["idea_ids"]
    assert len(state["evidence"]) == 1


def test_decision_records_versions_and_turns() -> None:
    state = _state()
    idea = {"candidate_id": "candidate_1", "title": "Idea"}
    decision = {"thesis": "t", "core_mechanism": "m", "unresolved": []}
    item = record_decision(state, idea, decision)
    assert item["version"] == 1
    assert state["idea_states"]["candidate_1"]["decision"]["thesis"] == "t"
    record_decision(state, idea, decision)
    assert state["idea_states"]["candidate_1"]["version"] == 2
    assert len(state["decision_log"]) == 2

    record_turn(
        state,
        phase="P2.1",
        speaker_key="a",
        speaker_label="A",
        model_id="m",
        action="RESPOND",
        info_gain="medium",
        speech="hello",
    )
    assert state["research_turns"][0]["turn_id"] == 1
    assert state["research_turns"][0]["information_gain"] == "medium"


class _DiversityHost:
    def __init__(self, candidates: list[dict]) -> None:
        self.state = {"candidates": candidates}
        self.messages: list[tuple] = []

    def _record_message(self, *args, **kwargs) -> None:
        self.messages.append(args)

    def _save_state(self) -> None:
        return None


def test_candidate_diversity_only_marks_similar_mechanisms() -> None:
    host = _DiversityHost(
        [
            {
                "id": "candidate_1",
                "title": "Decision-preserving context compression for long agent traces",
                "one_sentence": "Compress agent traces while preserving decision evidence.",
                "mechanism": "Decision-aware compression with evidence retention.",
                "gap": "Long traces lose decisions.",
            },
            {
                "id": "candidate_2",
                "title": "Decision-preserving compression of long agent traces",
                "one_sentence": "Compress long traces and preserve decision evidence.",
                "mechanism": "Evidence-retaining decision-aware compression.",
                "gap": "Decisions are lost in long traces.",
            },
            {
                "id": "candidate_3",
                "title": "Sparse retrieval for multilingual code search",
                "one_sentence": "Improve code search with sparse retrieval.",
                "mechanism": "Lexical sparse retrieval over code.",
                "gap": "Dense retrieval misses rare identifiers.",
            },
        ]
    )
    RunEngine._check_candidate_diversity(host)  # type: ignore[arg-type]
    groups = host.state["candidate_diversity"]["groups"]
    assert ["candidate_1", "candidate_2"] in groups
    assert all("candidate_3" not in group for group in groups)
    assert host.state["candidates"][0]["duplicate_group"] == "G1"
