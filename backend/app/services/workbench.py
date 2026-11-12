from __future__ import annotations

from typing import Any


def accept_routes(state: dict[str, Any], raw: Any, turn: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    candidates = state["candidates"]
    route_count = sum(bool(item.get("parent_ids")) for item in candidates)
    known_ids = {item["id"] for item in candidates}
    mechanisms = {" ".join(str(item.get("mechanism") or "").lower().split()) for item in candidates}
    routes = []
    for route in raw[:2]:
        if route_count >= 4 or not isinstance(route, dict):
            break
        parents = route.get("parent_ids")
        if not isinstance(parents, list) or not 1 <= len(parents) <= 2:
            continue
        if not all(isinstance(parent, str) and parent in known_ids for parent in parents):
            continue
        kind = route.get("kind")
        if kind not in ("fork", "merge") or len(set(parents)) != (2 if kind == "merge" else 1):
            continue
        fields = {
            key: str(route.get(key) or "").strip()[:1600]
            for key in ("title", "one_sentence", "mechanism", "hypothesis_delta", "discriminating_test")
            if isinstance(route.get(key), str)
        }
        if len(fields) != 5 or not all(fields.values()):
            continue
        mechanism_key = " ".join(fields["mechanism"].lower().split())
        if mechanism_key in mechanisms:
            continue
        route_count += 1
        mechanisms.add(mechanism_key)
        routes.append({
            **fields, "id": f"route_{route_count}", "parent_ids": parents, "kind": kind,
            "source_turn": turn, "status": "candidate",
        })
    candidates.extend(routes)
    return routes


VALIDATION_FIELDS = {
    "hypothesis": "核心假设",
    "inputs": "数据、代码与依赖",
    "comparison": "最小对照",
    "resource_limit": "资源上限",
    "metric": "观察指标",
    "continue_if": "继续条件",
    "narrow_if": "收窄条件",
    "stop_if": "停止条件",
}


def validation_card(experiment: dict[str, Any], verdict: str) -> dict[str, Any]:
    verdict = verdict.strip().lower()
    raw = experiment.get("first_validation")
    raw = raw if isinstance(raw, dict) else {}
    fields = {
        key: raw[key].strip()[:1600]
        for key in VALIDATION_FIELDS
        if isinstance(raw.get(key), str) and raw[key].strip()
    }
    missing = [label for key, label in VALIDATION_FIELDS.items() if key not in fields]
    resources = raw.get("resources_status")
    status = "blocked" if verdict in ("method_revise", "thesis_conflict") or resources == "missing" else "pending"
    if verdict == "pass" and resources == "available" and not missing:
        status = "ready"
    return {**fields, "status": status, "missing": missing}


def render_idea_brief(idea: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    decision = idea.get("decision") or idea.get("consensus") or {}
    method = idea.get("method") or {}
    card = idea.get("validation_card") or {}
    status = {"ready": "可启动", "pending": "待确认", "blocked": "受阻"}.get(card.get("status"), "待确认")
    lines = [
        f"# {idea.get('title', 'Idea')}", "", "## 中心主张", "",
        str(decision.get("thesis") or idea.get("one_sentence") or "待定"), "",
        "## 核心机制", "",
        str(method.get("method_overview") or decision.get("core_mechanism") or idea.get("mechanism") or "待定"),
    ]
    procedure = method.get("procedure")
    if isinstance(procedure, list):
        lines.append("")
        for step in procedure:
            if isinstance(step, str) and step.strip():
                lines.append(f"- {step.strip()}")
    lines.extend(["", "## 首轮验证", "", f"**执行状态**　{status}", ""])
    for key, label in VALIDATION_FIELDS.items():
        lines.append(f"- **{label}**　{card.get(key) or '待确认'}")
    unresolved = decision.get("unresolved") or []
    if unresolved:
        lines.extend(["", "## 未决问题", ""])
        for item in unresolved:
            if isinstance(item, dict):
                problem = item.get("issue") or item.get("problem") or ""
                follow_up = item.get("how_to_handle") or item.get("fix") or ""
                lines.append(f"- {problem} {follow_up}".rstrip())
            else:
                lines.append(f"- {item}")
    rationale = decision.get("decision_rationale")
    if rationale:
        lines.extend(["", "## 取舍依据", "", str(rationale)])
    alternatives = decision.get("rejected_alternatives")
    if isinstance(alternatives, list):
        for item in alternatives:
            if isinstance(item, dict) and item.get("alternative"):
                lines.append(f"- {item['alternative']} — {item.get('reason') or '理由待补充'}")
    if evidence:
        lines.extend(["", "## 相关证据", ""])
        for item in evidence:
            level = {
                "abstract_verified": "有摘要",
                "metadata_only": "仅元数据",
            }.get(item.get("verification"), "待核验")
            lines.append(f"- {item.get('title')}（{level}） {item.get('url') or ''}".rstrip())
    return "\n".join(lines).rstrip() + "\n"
