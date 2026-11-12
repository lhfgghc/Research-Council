from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import RunCreate
from app.services.orchestrator import (
    FloorContext,
    Participant,
    normalize_gain,
    plan_next_action,
)

ROSTER = [
    Participant(key="a", role="domain_researcher_1", label="A"),
    Participant(key="b", role="method_architect", label="B"),
    Participant(key="c", role="experiment_designer", label="C"),
]
MIN = {"a": 1, "b": 1, "c": 1}


def ctx(**overrides) -> FloorContext:
    base = dict(
        phase="discussion",
        participants=ROSTER,
        counts={"a": 1, "b": 1, "c": 1},
        required=MIN,
        issues=[],
        requests=[],
        ideas=[],
        idea_mentions={},
        last_key="a",
        low_gain_streak=0,
        turn=3,
        budget_left=20,
        budget_reserve=5,
    )
    base.update(overrides)
    return FloorContext(**base)


def test_coverage_picks_the_least_spoken_role() -> None:
    action = plan_next_action(ctx(counts={"a": 0, "b": 1, "c": 1}))
    assert action is not None and action.type == "CONTINUE"
    assert action.target is not None and action.target.key == "a"


def test_debate_gives_a_peer_one_development_turn_before_routine_issues() -> None:
    issue = {"id": "obj_1", "idea_id": "candidate_1", "severity": "major", "issue": "try another baseline"}
    floor = ctx(phase="debate", active_idea_id="idea_1", candidate_id="candidate_1", issues=[issue], owner_key="b")
    action = plan_next_action(floor)
    assert action is not None and action.type == "DEVELOP"
    assert action.target is not None and action.target.key != "b"

    floor.development_done = True
    action = plan_next_action(floor)
    assert action is not None and action.type == "RESPOND" and action.issue_id == "obj_1"

    floor.development_done = False
    floor.budget_left = floor.budget_reserve
    assert plan_next_action(floor) is None


def test_minor_objection_has_no_automatic_followup() -> None:
    floor = ctx(issues=[{"id": "obj_1", "severity": "minor", "type": "evidence", "issue": "add background"}])
    assert plan_next_action(floor) is None


def test_coverage_avoids_the_last_speaker_unless_they_are_behind() -> None:
    action = plan_next_action(ctx(counts={"a": 1, "b": 0, "c": 1}, last_key="a"))
    assert action is not None and action.target is not None and action.target.key == "b"
    action = plan_next_action(ctx(counts={"a": 0, "b": 1, "c": 1}, last_key="a"))
    assert action is not None and action.target is not None and action.target.key == "a"


def test_explicit_request_wins_after_coverage() -> None:
    action = plan_next_action(
        ctx(requests=[{"to": "c", "issue": "请回应机制问题", "attempts": 0}])
    )
    assert action is not None and action.type == "RESPOND"
    assert action.target is not None and action.target.key == "c"


def test_open_issue_routes_to_the_responsible_role() -> None:
    action = plan_next_action(
        ctx(
            issues=[
                {
                    "id": "obj_1",
                    "status": "open",
                    "type": "因果链",
                    "severity": "blocking",
                    "issue": "机制不成立",
                    "fix_direction": "补充因果链",
                    "idea_id": "",
                }
            ]
        )
    )
    assert action is not None and action.type == "RESPOND"
    assert action.target is not None and action.target.role == "method_architect"


def test_assigned_issue_goes_to_the_assignee() -> None:
    action = plan_next_action(
        ctx(
            issues=[
                {
                    "id": "obj_1",
                    "status": "open",
                    "type": "实验",
                    "severity": "major",
                    "issue": "实验不可行",
                    "assigned_to": "c",
                }
            ]
        )
    )
    assert action is not None and action.target is not None and action.target.key == "c"


def test_evidence_issue_triggers_retrieval_without_a_speaker() -> None:
    action = plan_next_action(
        ctx(
            issues=[
                {
                    "id": "obj_1",
                    "status": "open",
                    "type": "证据",
                    "severity": "blocking",
                    "issue": "缺乏先例核验",
                    "evidence_query": "test-time adaptation prior work",
                }
            ]
        )
    )
    assert action is not None and action.type == "RETRIEVE"
    assert action.queries == ("test-time adaptation prior work",)


def test_exhausted_issue_is_not_reopened() -> None:
    action = plan_next_action(
        ctx(
            issues=[
                {
                    "id": "obj_1",
                    "status": "contested",
                    "type": "因果链",
                    "severity": "blocking",
                    "issue": "已争论两次",
                    "attempt_count": 2,
                }
            ]
        )
    )
    assert action is None


def test_low_gain_patience_and_budget_stop_the_floor() -> None:
    assert plan_next_action(ctx(low_gain_streak=3)) is None
    assert plan_next_action(ctx(budget_left=5, budget_reserve=5)) is None


def test_undiscussed_candidate_gets_an_explore_action() -> None:
    action = plan_next_action(
        ctx(
            ideas=[
                {"id": "candidate_1", "title": "Idea A", "status": "candidate"},
                {"id": "candidate_2", "title": "Idea B", "status": "candidate"},
            ],
            idea_mentions={"candidate_1": 1},
        )
    )
    assert action is not None and action.type == "EXPLORE"
    assert action.idea_id == "candidate_2"


def test_normalize_gain_prefers_explicit_value_and_handles_pass() -> None:
    assert normalize_gain("high") == "high"
    assert normalize_gain(None, revised=True) == "medium"
    assert normalize_gain(None, is_pass=True) == "none"
    assert normalize_gain(None) == "low"


def test_team_rounds_has_no_upper_bound() -> None:
    assert RunCreate(team_rounds=8).team_rounds == 8
    assert RunCreate().team_rounds == 2
    assert RunCreate().max_team_turns == 20
    assert RunCreate().max_debate_turns == 15
    assert RunCreate(max_team_turns=0).max_team_turns == 0
    with pytest.raises(ValidationError):
        RunCreate(team_rounds=0)


def test_balance_interleaves_when_one_role_dominates() -> None:
    # 实测场景：实验设计者被问题路由反复选中（10 次 vs 4 次），差距 ≥3 且
    # 最高优先级动作只是普通 major 问题（<0.82）时，先补发言最少的角色。
    issues = [
        {
            "id": "obj_1",
            "type": "判别实验",
            "severity": "major",
            "status": "open",
            "issue": "等开销对照缺失",
            "fix_direction": "补中性填充对照",
            "attempt_count": 0,
        }
    ]
    action = plan_next_action(
        ctx(counts={"a": 4, "b": 6, "c": 10}, issues=issues, last_key="c")
    )
    assert action is not None and action.type == "CONTINUE"
    assert action.target is not None and action.target.key == "a"
    assert "不均衡" in action.reason


def test_critical_issue_still_beats_balance() -> None:
    issues = [
        {
            "id": "obj_2",
            "type": "中心主张",
            "severity": "critical",
            "status": "open",
            "issue": "中心主张在等预算下不成立",
            "fix_direction": "收窄主张",
            "attempt_count": 0,
        }
    ]
    action = plan_next_action(
        ctx(counts={"a": 4, "b": 6, "c": 10}, issues=issues, last_key="c")
    )
    assert action is not None and action.type == "RESPOND"
    assert action.issue_id == "obj_2"


def test_direct_request_beats_balance() -> None:
    action = plan_next_action(
        ctx(
            counts={"a": 4, "b": 6, "c": 10},
            requests=[{"to": "b", "issue": "请回应", "attempts": 0}],
            last_key="c",
        )
    )
    assert action is not None and action.type == "RESPOND"
    assert action.target is not None and action.target.key == "b"
