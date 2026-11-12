"""Action-level scheduling for the discussion floor.

The scheduler never decides what is true about the research; it only decides
which action is worth executing next and who should execute it. Rules handle
the clear cases (participation floor, direct requests, blocking issues); the
remaining choices fall back to a small priority score so the behaviour stays
predictable and cheap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LOW_GAIN_PATIENCE = 3
MAX_UNPRODUCTIVE_REPLIES = 2

SEVERITY_WEIGHT = {
    "critical": 1.0,
    "blocking": 1.0,
    "major": 0.65,
    "minor": 0.3,
}

EVIDENCE_ISSUE_TYPES = {"证据", "evidence", "novelty", "创新性", "文献"}

ISSUE_ROLE = {
    "方法论": "method_architect",
    "methodology": "method_architect",
    "机制": "method_architect",
    "因果链": "method_architect",
    "理论": "method_architect",
    "理论有效性": "method_architect",
    "实验": "experiment_designer",
    "experiment": "experiment_designer",
    "判别实验": "experiment_designer",
    "可行性": "experiment_designer",
    "feasibility": "experiment_designer",
    "资源": "experiment_designer",
    "resource": "experiment_designer",
    "一致性": "method_architect",
    "consistency": "method_architect",
}


@dataclass(frozen=True)
class Participant:
    key: str
    role: str
    label: str
    profile: str | None = None
    capabilities: tuple[str, ...] = ()


@dataclass
class Action:
    type: str
    target: Participant | None = None
    instruction: str = ""
    reason: str = ""
    priority: float = 0.0
    issue_id: str = ""
    idea_id: str = ""
    queries: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class FloorContext:
    phase: str
    participants: list[Participant]
    counts: dict[str, int]
    required: dict[str, int]
    issues: list[dict[str, Any]]
    requests: list[dict[str, Any]] = field(default_factory=list)
    ideas: list[dict[str, Any]] = field(default_factory=list)
    idea_mentions: dict[str, int] = field(default_factory=dict)
    last_key: str = ""
    low_gain_streak: int = 0
    turn: int = 0
    budget_left: int = 0
    budget_reserve: int = 0
    max_turns: int = 0
    active_idea_id: str = ""
    challenged: bool = False
    candidate_id: str = ""
    development_done: bool = False
    owner_key: str = ""


def _status(issue: dict[str, Any]) -> str:
    return str(issue.get("status") or "open")


def is_open_issue(issue: dict[str, Any]) -> bool:
    return _status(issue) in ("open", "contested")


def is_exhausted_issue(issue: dict[str, Any]) -> bool:
    return is_open_issue(issue) and int(issue.get("attempt_count") or 0) >= MAX_UNPRODUCTIVE_REPLIES


def needs_evidence(issue: dict[str, Any]) -> bool:
    if issue.get("retrieval_done"):
        return False
    kind = str(issue.get("type") or "").strip().lower()
    if kind in EVIDENCE_ISSUE_TYPES:
        return True
    return bool(issue.get("evidence_query"))


def issue_severity(issue: dict[str, Any]) -> float:
    return SEVERITY_WEIGHT.get(str(issue.get("severity") or "major"), 0.65)


def _by_key(participants: list[Participant], key: str | None) -> Participant | None:
    return next((item for item in participants if item.key == key), None)


def _role_participant(
    participants: list[Participant], role: str, counts: dict[str, int], last_key: str
) -> Participant | None:
    pool = [item for item in participants if item.role == role]
    if not pool:
        pool = participants
    if not pool:
        return None
    pool.sort(key=lambda item: (item.key == last_key, counts.get(item.key, 0)))
    return pool[0]


def _responder_for(
    issue: dict[str, Any],
    participants: list[Participant],
    counts: dict[str, int],
    last_key: str,
) -> Participant | None:
    assigned = str(issue.get("assigned_to") or issue.get("respond_to") or "")
    explicit = _by_key(participants, assigned)
    if explicit:
        return explicit
    role = ISSUE_ROLE.get(str(issue.get("type") or "").strip()) or ISSUE_ROLE.get(
        str(issue.get("type") or "").strip().lower()
    )
    if role:
        return _role_participant(participants, role, counts, last_key)
    return _role_participant(participants, "method_architect", counts, last_key)


def _issue_action(
    issue: dict[str, Any],
    ctx: FloorContext,
) -> Action | None:
    if not is_open_issue(issue) or is_exhausted_issue(issue) or issue.get("severity") == "minor":
        return None
    if ctx.phase == "debate" and ctx.active_idea_id:
        idea_id = str(issue.get("idea_id") or "")
        if idea_id and idea_id not in {ctx.active_idea_id, ctx.candidate_id}:
            return None
    if needs_evidence(issue):
        query = str(issue.get("evidence_query") or issue.get("issue") or "")[:120]
        return Action(
            type="RETRIEVE",
            instruction=f"定向核验：{issue.get('issue', '')[:200]}",
            reason=f"Issue {issue.get('id', '?')} 缺少可核验证据",
            priority=0.72 + 0.18 * issue_severity(issue),
            issue_id=str(issue.get("id") or ""),
            idea_id=str(issue.get("idea_id") or ""),
            queries=(query,) if query else (),
            meta={"severity": issue.get("severity"), "source_key": issue.get("raised_by", "")},
        )
    target = _responder_for(issue, ctx.participants, ctx.counts, ctx.last_key)
    if not target:
        return None
    severity = issue_severity(issue)
    attempts = int(issue.get("attempt_count") or 0)
    resolvability = 0.9 if issue.get("fix_direction") else 0.55
    urgency = 1.0 if severity >= 0.99 else 0.6
    remaining = 1.0 - attempts / MAX_UNPRODUCTIVE_REPLIES
    priority = (
        0.4 * severity
        + 0.25 * resolvability
        + 0.2 * urgency
        + 0.15 * remaining
        - (0.05 if target.key == ctx.last_key else 0.0)
    )
    return Action(
        type="RESPOND",
        target=target,
        instruction=(
            f"回应 {issue.get('id', '')}：{issue.get('issue', '')[:300]}"
            f"（修复方向：{issue.get('fix_direction') or '待定'}）"
        ),
        reason=f"未决问题 {issue.get('id', '?')}（{issue.get('severity', 'major')}）尚未闭环",
        priority=priority,
        issue_id=str(issue.get("id") or ""),
        idea_id=str(issue.get("idea_id") or ""),
        meta={"severity": issue.get("severity"), "attempts": attempts, "source_key": issue.get("raised_by", "")},
    )


def plan_next_action(ctx: FloorContext) -> Action | None:
    """Return the next action, or None when the floor should收束.

    Protect participation and one opportunity to develop each idea before
    scoring the requests, objections and remaining candidate exploration.
    """
    if not ctx.participants:
        return None
    if ctx.budget_left <= ctx.budget_reserve:
        return None
    if ctx.max_turns and ctx.turn >= ctx.max_turns:
        return None

    behind = [
        item
        for item in ctx.participants
        if ctx.counts.get(item.key, 0) < ctx.required.get(item.key, 0)
    ]
    if behind:
        behind.sort(
            key=lambda item: (
                ctx.counts.get(item.key, 0),
                item.key == ctx.last_key,
            )
        )
        target = behind[0]
        return Action(
            type="CONTINUE",
            target=target,
            instruction="给出独立判断。可以补全方法、支持有价值的路线、指出实质问题，或说明没有新增判断。",
            reason="最低参与次数尚未满足",
            priority=0.99,
            meta={"coverage": True},
        )

    if ctx.phase == "debate" and not ctx.development_done and len(ctx.participants) > 1:
        peers = [item for item in ctx.participants if item.key != ctx.owner_key]
        target = _role_participant(peers, "method_architect", ctx.counts, ctx.last_key)
        if target:
            return Action(
                type="DEVELOP",
                target=target,
                instruction=(
                    "把当前路线完善成一套方法：用一个典型案例说明问题为何值得做，"
                    "再讲清输入、主要步骤、输出，以及这些步骤怎样解决问题。"
                    "可以复用已有组件或提出替代机制。保留有价值的问题规模，优先修方法，"
                    "有具体依据才缩小适用范围。实质改动写入 revised_plan 和 changes。"
                ),
                reason="留出一轮完善方法与研究叙事",
                priority=0.95,
                idea_id=ctx.active_idea_id,
                meta={"source_key": ctx.owner_key},
            )

    request_actions: list[Action] = []
    for request_index, request in enumerate(ctx.requests or []):
        key = str(request.get("to") or "")
        target = _by_key(ctx.participants, key)
        if not target:
            continue
        if int(request.get("attempts") or 0) >= 1:
            continue
        issue_text = str(request.get("issue") or "").strip()
        action_type = str(request.get("action_type") or "RESPOND")
        request_actions.append(
            Action(
                type=action_type,
                target=target,
                instruction=f"回应点名：{issue_text[:300]}" if issue_text else "回应上一轮的点名。",
                reason="上一轮明确要求该成员回应",
                priority=0.9 - (0.05 if target.key == ctx.last_key else 0.0),
                issue_id=str(request.get("issue_id") or ""),
                meta={"request": True, "request_index": request_index, "source_key": request.get("source_key", "")},
            )
        )

    issue_actions = [action for action in (_issue_action(item, ctx) for item in ctx.issues) if action]
    blocking = [action for action in issue_actions if action.meta.get("severity") in {"blocking", "critical"}]
    if ctx.low_gain_streak >= LOW_GAIN_PATIENCE:
        return max(blocking, key=lambda action: action.priority, default=None)

    explore_actions: list[Action] = []
    if ctx.phase == "discussion":
        candidates = [
            item
            for item in ctx.ideas
            if str(item.get("status") or "candidate") not in ("rejected", "archived")
        ]
        candidates.sort(key=lambda item: ctx.idea_mentions.get(str(item.get("id")), 0))
        for item in candidates[:1]:
            mentions = ctx.idea_mentions.get(str(item.get("id")), 0)
            if mentions >= 1:
                continue
            target = _role_participant(
                ctx.participants, "domain_researcher_1", ctx.counts, ctx.last_key
            )
            if not target:
                continue
            explore_actions.append(
                Action(
                    type="EXPLORE",
                    target=target,
                    instruction=f"深入讨论候选 {item.get('title', '')}：说明问题为何值得做，补全一条有希望的方法，或指出决定性的障碍。",
                    reason=f"候选 {item.get('id', '?')} 尚未得到充分讨论",
                    priority=0.42 - 0.05 * mentions,
                    idea_id=str(item.get("id") or ""),
                )
            )

    if explore_actions and not (request_actions or blocking):
        return explore_actions[0]

    pool = request_actions + issue_actions + explore_actions
    pool.sort(key=lambda action: (-action.priority, action.target.key if action.target else ""))

    # 参与均衡的提前干预：单一角色被问题路由连续选中、发言差距拉大时，
    # 在非点名、非阻断级的普通动作之前先补足发言最少的参与者（plan §2.3 / 实测校准）。
    counts = ctx.counts
    if len(ctx.participants) >= 2:
        low = min(counts.get(item.key, 0) for item in ctx.participants)
        high = max(counts.get(item.key, 0) for item in ctx.participants)
        top_priority = pool[0].priority if pool else 0.0
        if high - low >= 3 and top_priority < 0.82:
            candidates = [item for item in ctx.participants if item.key != ctx.last_key]
            if not candidates:
                candidates = ctx.participants
            target = sorted(candidates, key=lambda item: counts.get(item.key, 0))[0]
            if counts.get(target.key, 0) == low:
                return Action(
                    type="CONTINUE",
                    target=target,
                    instruction="补充一个尚未被覆盖的视角，或者明确说明没有新增判断。",
                    reason="发言次数分布不均衡（差距 ≥ 3）",
                    priority=0.82,
                )

    if pool:
        return pool[0]

    if ctx.phase == "debate" and not ctx.challenged and len(ctx.participants) > 1:
        target = _role_participant(
            ctx.participants, "method_architect", ctx.counts, ctx.last_key
        )
        if target:
            return Action(
                type="CHALLENGE",
                target=target,
                instruction="在写作前对当前方案发起一次最强反驳：找出它最可能失效的条件，或明确给出它成立的范围。",
                reason="核心机制还没有经过实质性挑战",
                priority=0.5,
            )

    if ctx.participants:
        low = min(ctx.counts.get(item.key, 0) for item in ctx.participants)
        high = max(ctx.counts.get(item.key, 0) for item in ctx.participants)
        if high - low >= 2:
            target = sorted(
                ctx.participants, key=lambda item: ctx.counts.get(item.key, 0)
            )[0]
            return Action(
                type="CONTINUE",
                target=target,
                instruction="补充一个尚未被覆盖的视角，或者明确说明没有新增判断。",
                reason="发言次数分布不均衡",
                priority=0.15,
            )
    return None


def normalize_gain(
    value: Any,
    *,
    revised: bool = False,
    new_issue: bool = False,
    resolved: int = 0,
    is_pass: bool = False,
) -> str:
    text = str(value or "").strip().lower()
    if text in ("high", "medium", "low", "none"):
        return text
    if is_pass:
        return "none"
    if revised or new_issue or resolved:
        return "medium"
    return "low"


def describe_action(action: Action) -> dict[str, Any]:
    return {
        "type": action.type,
        "target_role": action.target.role if action.target else "",
        "target_model": action.target.profile if action.target else "",
        "target_key": action.target.key if action.target else "",
        "instruction": action.instruction,
        "reason": action.reason,
        "priority": round(action.priority, 3),
        "issue_id": action.issue_id,
        "idea_id": action.idea_id,
        "queries": list(action.queries),
        "source_key": action.meta.get("source_key", ""),
        "requested": bool(action.meta.get("request")),
    }
