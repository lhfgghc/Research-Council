from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import AppSettings
    from ..schemas import RunCreate


def model_bindings(settings: AppSettings, role: str) -> list[str]:
    valid = {profile.id for profile in settings.model_profiles}
    return list(dict.fromkeys(
        profile for profile in settings.role_models.get(role, []) if profile in valid
    ))


def budget_preview(request: RunCreate, settings: AppSettings) -> dict[str, int | str]:
    ideas = request.idea_count
    method = model_bindings(settings, "method_architect")
    experiment = model_bindings(settings, "experiment_designer")
    roster = settings.discussion_models
    team_models = 3 if roster is None else len(roster)
    models = len(set(method + experiment)) if roster is None else len(roster)
    core = 4 + request.candidate_count + (4 if request.deliverable == "idea_brief" else 8) * ideas
    team = team_models * request.team_rounds
    debate = models * ideas if models >= 2 else 0
    panels = sum(count for count in (len(method), len(experiment)) if count > 1) * ideas if roster is None else 0
    full = core + team + debate + panels
    error = ""
    if ideas > request.candidate_count:
        error = "完整 Idea 数不能超过独立候选数。"
    elif roster == []:
        error = "请至少选择一个参与讨论的模型。"
    elif request.max_team_turns and team > request.max_team_turns:
        error = f"最低发言目标为 {team} 次，超过团队讨论上限 {request.max_team_turns} 次。"
    elif request.max_debate_turns and models >= 2 and models > request.max_debate_turns:
        error = f"辩论参与模型数超过每个 Idea 的发言上限 {request.max_debate_turns}。"
    elif request.max_calls < core:
        error = f"核心流程至少需要 {core} 次调用。"
    elif roster is not None and request.max_calls < full:
        error = f"当前配置至少需要 {full} 次调用才能覆盖全部模型的最低发言次数。"
    return {
        "core": core, "team": team, "team_models": team_models, "debate": debate, "debate_models": models,
        "panels": panels, "full": full,
        "recommended": full + (2 if request.deliverable == "idea_brief" else 3) * ideas + 3,
        "error": error,
    }
