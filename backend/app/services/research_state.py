"""Structured research state helpers.

The discussion transcript stays in the messages table; this module owns the
facts that scheduling depends on: issues, evidence, turns and idea decisions.
Everything lives in ``run.state_json`` so a paused run can resume without
replaying the conversation.
"""

from __future__ import annotations

import json
from typing import Any

from ..db import utc_now
from .orchestrator import MAX_UNPRODUCTIVE_REPLIES

SEVERITY_ORDER = {"critical": 0, "blocking": 1, "major": 2, "minor": 3}


def ensure_research_state(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("objections", [])
    state.setdefault("evidence", [])
    state.setdefault("research_turns", [])
    state.setdefault("idea_states", {})
    state.setdefault("decision_log", [])
    state.setdefault("low_gain_streak", 0)
    return state


def idea_key(idea: dict[str, Any]) -> str:
    return str(idea.get("idea_id") or idea.get("id") or idea.get("candidate_id") or "")


def ensure_idea_state(state: dict[str, Any], idea: dict[str, Any]) -> dict[str, Any]:
    key = idea_key(idea)
    store = state.setdefault("idea_states", {})
    item = store.setdefault(
        key,
        {
            "idea_id": key,
            "title": idea.get("title", ""),
            "version": 0,
            "status": "selected",
            "open_issue_ids": [],
            "decision": {},
            "revision_history": [],
            "rollback_count": 0,
            "debate_done": False,
        },
    )
    item["title"] = idea.get("title") or item.get("title", "")
    return item


def issue_by_id(state: dict[str, Any], issue_id: str) -> dict[str, Any] | None:
    return next(
        (item for item in state.get("objections", []) if item.get("id") == issue_id),
        None,
    )


def open_issues(state: dict[str, Any], idea_id: str = "") -> list[dict[str, Any]]:
    items = []
    for item in state.get("objections", []):
        if (item.get("status") or "open") not in ("open", "contested"):
            continue
        if idea_id and item.get("idea_id") and item["idea_id"] != idea_id:
            continue
        items.append(item)
    return items


def add_issue(
    state: dict[str, Any],
    payload: dict[str, Any],
    *,
    speaker_key: str,
    speaker_label: str,
    turn: int,
    stage: str,
    idea_id: str = "",
    default_assignee: str = "",
) -> dict[str, Any] | None:
    text = str(payload.get("issue") or "").strip()
    if not text:
        return None
    items = state.setdefault("objections", [])
    issue = {
        "id": f"obj_{len(items) + 1}",
        "turn": turn,
        "stage": stage,
        "idea_id": idea_id,
        "raised_by": speaker_key,
        "role": speaker_label,
        "target": str(payload.get("target", ""))[:400],
        "type": str(payload.get("type", ""))[:80],
        "issue": text[:1200],
        "fix_direction": str(payload.get("fix_direction", ""))[:800],
        "severity": str(payload.get("severity", "major")),
        "status": "open",
        "assigned_to": str(default_assignee or payload.get("respond_to") or ""),
        "respond_to": str(default_assignee or payload.get("respond_to") or ""),
        "answered_by": "",
        "verdict": "",
        "resolution": "",
        "evidence_ids": [],
        "attempt_count": 0,
        "next_action": "等待回应",
        "evidence_query": str(payload.get("evidence_query") or "")[:200],
        "retrieval_done": False,
    }
    items.append(issue)
    if idea_id:
        state.setdefault("idea_states", {}).setdefault(idea_id, {}).setdefault(
            "open_issue_ids", []
        ).append(issue["id"])
    return issue


def apply_issue_response(
    state: dict[str, Any],
    issue_id: str,
    decision: str,
    why: str,
    *,
    speaker_key: str,
    evidence_ids: list[str] | None = None,
    revised: bool = False,
    turn: int = 0,
) -> dict[str, Any] | None:
    issue = issue_by_id(state, issue_id)
    if not issue or issue.get("status") == "resolved":
        return None
    decision = str(decision or "").strip().lower()
    issue["answered_by"] = speaker_key
    issue["answered_turn"] = turn
    issue["verdict"] = decision
    if why:
        issue["resolution"] = str(why)[:800]
    scope = {issue.get("idea_id"), issue.get("target")} - {None, ""}
    registered = {
        item["id"] for item in state.get("evidence", [])
        if item.get("id") and (
            not scope or not item.get("idea_ids") or scope.intersection(item["idea_ids"])
        )
    }
    issue["evidence_ids"] = list(dict.fromkeys(
        key for key in [*(issue.get("evidence_ids") or []), *(evidence_ids or [])]
        if key in registered
    ))

    if decision in ("accepted", "partial"):
        severe = str(issue.get("severity")) in ("critical", "blocking")
        withdrawn = speaker_key == str(issue.get("raised_by") or "")
        if severe and not revised and not issue.get("evidence_ids") and not withdrawn:
            issue["status"] = "contested"
            issue["attempt_count"] = int(issue.get("attempt_count") or 0) + 1
            issue["next_action"] = "补充证据或完整修订后再裁决"
            issue["resolution"] = (
                issue.get("resolution", "") + "（缺少证据或修订，暂不视为解决）"
            )[:800]
        else:
            issue["status"] = "resolved"
            issue["next_action"] = ""
    elif decision == "rejected":
        issue["attempt_count"] = int(issue.get("attempt_count") or 0) + 1
        if issue["attempt_count"] >= MAX_UNPRODUCTIVE_REPLIES:
            issue["status"] = "unresolved"
            issue["next_action"] = "提交研究决策，记录为未决"
        else:
            issue["status"] = "contested"
            issue["assigned_to"] = str(issue.get("raised_by") or "")
            issue["respond_to"] = issue["assigned_to"]
            issue["next_action"] = "提出者需决定撤回或坚持"
    elif decision in ("unresolved", "give_up"):
        issue["status"] = "unresolved"
        issue["next_action"] = "提交研究决策，记录为未决"
    else:
        issue["status"] = "contested"
        issue["next_action"] = "回应不完整，需要重新裁决"
    if idea_id := str(issue.get("idea_id") or ""):
        ids = state.setdefault("idea_states", {}).setdefault(idea_id, {}).setdefault(
            "open_issue_ids", []
        )
        if issue["status"] in ("resolved", "unresolved") and issue["id"] in ids:
            ids.remove(issue["id"])
    return issue


def record_turn(
    state: dict[str, Any],
    *,
    phase: str,
    speaker_key: str,
    speaker_label: str,
    model_id: str,
    action: str,
    info_gain: str,
    speech: str,
    issue_ids: list[str] | None = None,
    idea_id: str = "",
    reason: str = "",
) -> dict[str, Any]:
    turns = state.setdefault("research_turns", [])
    item = {
        "turn_id": len(turns) + 1,
        "phase": phase,
        "speaker_role": speaker_key,
        "speaker_label": speaker_label,
        "model_id": model_id,
        "action": action,
        "information_gain": info_gain,
        "summary": str(speech or "")[:600],
        "issue_ids": issue_ids or [],
        "idea_id": idea_id,
        "reason": reason,
        "at": utc_now(),
    }
    turns.append(item)
    state["research_turns"] = turns[-200:]
    return item


def _evidence_level(work: dict[str, Any]) -> str:
    if str(work.get("abstract") or "").strip():
        return "abstract_verified"
    if work.get("url") or work.get("doi") or work.get("arxiv_id"):
        return "metadata_only"
    return "unverified"


def record_evidence(
    state: dict[str, Any],
    works: list[dict[str, Any]],
    *,
    query: str,
    idea_id: str = "",
) -> list[dict[str, Any]]:
    """Register retrieval results with stable ids and a verification level."""
    store = state.setdefault("evidence", [])
    index = {
        (str(item.get("doi") or item.get("url") or item.get("title") or "")).lower(): item
        for item in store
    }
    added: list[dict[str, Any]] = []
    next_id = max(
        (int(item["id"][3:]) for item in store if str(item.get("id", ""))[3:].isdigit()),
        default=0,
    ) + 1
    for work in works:
        identity = str(work.get("doi") or work.get("url") or work.get("title") or "").lower()
        if not identity:
            continue
        if identity in index:
            record = index[identity]
            if idea_id and idea_id not in record.setdefault("idea_ids", []):
                record["idea_ids"].append(idea_id)
            if _evidence_level(work) == "abstract_verified" and record.get("verification") != "abstract_verified":
                record["verification"] = "abstract_verified"
                record["abstract"] = str(work["abstract"])[:800]
            added.append(record)
            continue
        record = {
            "id": f"ev_{next_id}",
            "title": str(work.get("title") or "")[:300],
            "source": str(work.get("source") or ""),
            "year": work.get("year"),
            "venue": str(work.get("venue") or "")[:200],
            "url": str(work.get("url") or work.get("doi") or ""),
            "doi": str(work.get("doi") or ""),
            "query": str(query or "")[:200],
            "verification": _evidence_level(work),
            "abstract": str(work.get("abstract") or "")[:800],
            "idea_ids": [idea_id] if idea_id else [],
            "retrieved_at": utc_now(),
        }
        store.append(record)
        next_id += 1
        index[identity] = record
        added.append(record)
    state["evidence"] = store[-400:]
    return added


def record_decision(
    state: dict[str, Any],
    idea: dict[str, Any],
    decision: dict[str, Any],
    *,
    unresolved: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    item = ensure_idea_state(state, idea)
    item["version"] = int(item.get("version") or 0) + 1
    item["decision"] = decision
    item["status"] = "decided"
    item["decided_at"] = utc_now()
    item["unresolved"] = unresolved or []
    item.setdefault("revision_history", []).append(
        {
            "version": item["version"],
            "thesis": str(decision.get("thesis") or "")[:400],
            "at": item["decided_at"],
        }
    )
    state.setdefault("decision_log", []).append(
        {
            "idea_id": idea_key(idea),
            "version": item["version"],
            "thesis": str(decision.get("thesis") or "")[:400],
            "at": item["decided_at"],
        }
    )
    state["decision_log"] = state["decision_log"][-100:]
    return item


def unresolved_for(state: dict[str, Any], idea_id: str) -> list[dict[str, Any]]:
    return [
        {
            "issue_id": item.get("id"),
            "issue": item.get("issue", ""),
            "severity": item.get("severity", ""),
            "attempts": item.get("attempt_count", 0),
            "next_action": item.get("next_action", ""),
        }
        for item in state.get("objections", [])
        if item.get("status") == "unresolved"
        and (not idea_id or not item.get("idea_id") or item["idea_id"] == idea_id)
    ]


def dump(state: dict[str, Any]) -> str:
    return json.dumps(
        {
            "issues": state.get("objections", []),
            "evidence": state.get("evidence", []),
            "turns": state.get("research_turns", []),
        },
        ensure_ascii=False,
    )[:20000]
