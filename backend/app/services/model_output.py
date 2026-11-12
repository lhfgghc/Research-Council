from __future__ import annotations

from typing import Any

from .workbench import VALIDATION_FIELDS


CONTRACTS = {
    "agenda": (("agenda",), ("directions",)),
    "candidate": (("title", "one_sentence", "mechanism"), ("literature_queries",)),
    "literature": ((), ("candidates",)),
    "selection": ((), ("selected",)),
    "discussion": (("speech",), ()),
    "debate": (("speech",), ()),
    "decision": (("thesis", "core_mechanism", "decision_rationale"), ("unresolved",)),
    "method": (("problem_formulation", "core_insight", "method_overview"), ("components", "procedure")),
    "experiment": (("resource_budget",), ("claims", "main_experiments")),
    "gate": ((), ("issues",)),
    "coherence": ((), ("issues",)),
    "revision": (("revision_plan",), ("claim_mappings", "decisions")),
}

CHOICES = {
    "discussion": ("stance", {"contribute", "respond", "challenge", "pass"}),
    "debate": ("stance", {"challenge", "defend", "revise", "concede", "synthesize", "pass"}),
    "gate": ("verdict", {"pass", "method_revise", "thesis_conflict"}),
    "coherence": ("verdict", {"pass", "revise"}),
    "revision": ("revision_type", {"LOCAL_REVISE", "NARROW_CLAIM", "MECHANISM_REDESIGN", "PIVOT"}),
}


def validate_model_output(contract: str, result: dict[str, Any]) -> dict[str, Any]:
    text_fields, list_fields = CONTRACTS[contract]
    invalid = [key for key in text_fields if not isinstance(result.get(key), str) or not result[key].strip()]
    invalid.extend(key for key in list_fields if not isinstance(result.get(key), list))
    for key in ("objections", "responses", "attacks", "proposed_actions", "new_routes", "issues", "selected"):
        if key in result and (
            not isinstance(result[key], list) or any(not isinstance(item, dict) for item in result[key])
        ):
            invalid.append(key)
    for key in ("evidence_ids", "literature_queries", "referenced_ideas"):
        if key in result and (
            not isinstance(result[key], list) or any(not isinstance(item, str) for item in result[key])
        ):
            invalid.append(key)
    if contract in CHOICES:
        key, choices = CHOICES[contract]
        value = result.get(key)
        normalized = (value.strip().upper() if contract == "revision" else value.strip().lower()) if isinstance(value, str) else ""
        if normalized not in choices:
            invalid.append(key)
        else:
            result[key] = normalized
    for key in {"method": ("components", "procedure"), "experiment": ("claims", "main_experiments")}.get(contract, ()):
        if not result.get(key):
            invalid.append(key)
    if contract == "gate" and result.get("verdict") in ("method_revise", "thesis_conflict") and not result.get("issues"):
        invalid.append("issues")
    if contract == "experiment":
        card = result.get("first_validation")
        if not isinstance(card, dict):
            invalid.append("first_validation")
        else:
            invalid.extend(
                f"first_validation.{key}" for key in VALIDATION_FIELDS
                if not isinstance(card.get(key), str) or not card[key].strip()
            )
            if card.get("resources_status") not in ("available", "unknown", "missing"):
                invalid.append("first_validation.resources_status")
    if invalid:
        raise ValueError(f"{contract} 阶段字段缺失或格式不符：{', '.join(dict.fromkeys(invalid))}")
    return result
