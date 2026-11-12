from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from ..db import Database, utc_now
from ..config import AppSettings, ModelCallSettings, SettingsStore
from .artifacts import ArtifactStore, new_id
from .context import (
    SUMMARY_REQUEST,
    SUMMARY_SYSTEM,
    ContextManager,
    current_scope,
    estimate_messages,
    estimate_tokens,
    fit_messages_to_budget,
    hard_trim_messages,
)
from .events import EventBus
from .literature import check_literature
from .model_gateway import BudgetExhausted, ModelCallError, ModelGateway
from .model_output import validate_model_output
from .orchestrator import (
    Action,
    FloorContext,
    Participant,
    describe_action,
    normalize_gain,
    plan_next_action,
)
from .prompts import (
    ROLE_LABELS,
    candidate_messages,
    coherence_messages,
    decision_messages,
    debate_floor_messages,
    experiment_messages,
    gate_messages,
    literature_review_messages,
    method_messages,
    panel_synthesis_messages,
    pi_plan_messages,
    portfolio_messages,
    review_messages,
    revision_plan_messages,
    selection_messages,
    spec_snapshot,
    writer_initial_part_messages,
    writer_revision_part_messages,
)
from .research_state import (
    add_issue,
    apply_issue_response,
    ensure_idea_state,
    ensure_research_state,
    idea_key,
    open_issues,
    record_decision,
    record_evidence,
    record_turn,
    unresolved_for,
)
from .run_budget import model_bindings
from .skills import SkillRegistry, skill_kind_for
from .workbench import accept_routes, render_idea_brief, validation_card


class RunPaused(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


class InvalidModelOutput(ModelCallError):
    pass


def _escape_raw_newlines_in_strings(text: str) -> str:
    """Escape literal newlines/tabs that appear inside JSON string values."""
    out: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            elif char == "\n":
                out.append("\\n")
                continue
            elif char == "\t":
                out.append("\\t")
                continue
            elif char == "\r":
                out.append("\\r")
                continue
            out.append(char)
            continue
        if char == '"':
            in_string = True
        out.append(char)
    return "".join(out)


def salvage_json(text: str) -> dict[str, Any] | None:
    """Deterministic cleanup for near-JSON outputs: fences, trailing commas, raw newlines."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    for candidate in (
        cleaned,
        _escape_raw_newlines_in_strings(cleaned),
        re.sub(r",\s*([}\]])", r"\1", _escape_raw_newlines_in_strings(cleaned)),
    ):
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    decoder = json.JSONDecoder()
    normalized = re.sub(r",\s*([}\]])", r"\1", _escape_raw_newlines_in_strings(cleaned))
    if normalized.startswith("{"):
        try:
            value, _ = decoder.raw_decode(normalized)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    # Accept a prose prefix, but never search for a valid object inside a broken root.
    start = re.search(r"[{\[]", cleaned)
    salvaged = salvage_json(cleaned[start.start():]) if start and start.group() == "{" else None
    if salvaged is not None:
        return salvaged
    raise ValueError("模型没有返回合法 JSON 对象")


def document_completion_issues(text: str) -> list[str]:
    required = [
        "## Abstract",
        "## 1. Introduction",
        "## 2. Related Work",
        "## 3. Method",
        "## 4. Experiment",
        "## 5. Expected Results",
        "## 6. Discussion",
        "## 7. Reproducibility Plan",
    ]
    return [heading for heading in required if heading not in text]


def strip_markdown_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```markdown"):
        cleaned = cleaned[len("```markdown") :].lstrip()
    elif cleaned.startswith("```md"):
        cleaned = cleaned[len("```md") :].lstrip()
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:].lstrip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].rstrip()
    return cleaned.strip()


class RunEngine:
    def __init__(
        self,
        run_id: str,
        database: Database,
        settings_store: SettingsStore,
        artifacts: ArtifactStore,
        events: EventBus,
        gateway: ModelGateway,
        live_feed=None,
    ) -> None:
        self.run_id = run_id
        self.db = database
        self.settings_store = settings_store
        self.artifacts = artifacts
        self.events = events
        self.gateway = gateway
        self.live_feed = live_feed
        self.pause_requested = False
        self.cancel_requested = False
        self.shutting_down = False
        self.run: dict[str, Any] = {}
        self.project: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self.config: dict[str, Any] = {}
        self.skills_bundle = ""
        self.skill_bundles: dict[str, str] = {}
        self.skill_snapshot: list[dict[str, str]] = []
        self._pending_snapshot_id: int | None = None
        self._pending_snapshot_compressed = False
        self._last_response_model = ""

    async def execute(self) -> None:
        try:
            await self._prepare()
            self.events.publish(self.run_id, "run.started", {"run_id": self.run_id})
            await self._drive()
        except RunPaused:
            self._mark_status("PAUSED", "用户暂停，当前调用完成后停止")
        except RunCancelled:
            self._mark_status("CANCELLED", "用户已终止本次运行")
        except BudgetExhausted as exc:
            self._mark_status("BUDGET_EXHAUSTED", str(exc))
            self.events.publish(self.run_id, "run.budget_exhausted", {"run_id": self.run_id, "error": str(exc)})
        except asyncio.CancelledError:
            if self.shutting_down:
                self._mark_status("PAUSED", "服务已停止，运行处于可恢复状态")
            else:
                self._mark_status("CANCELLED", "本次运行已终止")
            raise
        except Exception as exc:
            self._mark_status("FAILED", str(exc))
            self.events.publish(self.run_id, "run.failed", {"run_id": self.run_id, "error": str(exc)})
        finally:
            if self._active_dispatch():
                self._finish_dispatch(status="interrupted")

    async def _prepare(self) -> None:
        self.run = self._get_run()
        self.project = self.db.query_one("SELECT * FROM projects WHERE id = ?", (self.run["project_id"],)) or {}
        if not self.project:
            raise RuntimeError("项目不存在")
        self.state = json.loads(self.run["state_json"] or "{}")
        if self._active_dispatch():
            self._finish_dispatch(status="interrupted")
        self.config = json.loads(self.run["config_json"] or "{}")
        self.state.setdefault("phase", self.run["phase"] or "P0")
        self.state.setdefault("recent_messages", [])
        self.state.setdefault("context_summaries", {})
        self.state.setdefault("decision_digest", [])
        self.state.setdefault("candidates", [])
        self.state.setdefault("review_notes", [])
        self.state.setdefault("selected", [])
        self.state.setdefault("user_inputs", [])
        self.state.setdefault("idea_next", 0)
        self.state.setdefault("recent_messages", [])
        if self.run.get("mode") == "REVISE" and self.run.get("base_idea_id"):
            self.state["current_idea_id"] = self.run["base_idea_id"]
        self.state.setdefault("spec_snapshot", spec_snapshot(self.project.get("venue", "ACL")))
        settings = self.settings_store.load()
        registry = SkillRegistry(settings.skills_dirs)
        snapshot = self.state.get("skill_bundles_snapshot")
        if not snapshot:
            # 没有运行级覆盖时，讨论和写作分别继承各自的默认值。
            explicit = self.config.get("skills")
            if explicit is None:
                explicit = json.loads(self.project.get("skills_json") or "[]") or settings.default_skills
            writing = self.config.get("writing_skills")
            snapshot = {
                "discussion": list(explicit),
                "writing": list(settings.writing_skills if writing is None else writing),
            }
            self.state["skill_bundles_snapshot"] = snapshot
        loaded_all: list[dict[str, str]] = []
        seen: set[str] = set()
        contents = self.state.setdefault("skill_content_snapshot", {})
        for kind in ("discussion", "writing"):
            if kind not in contents:
                bundle, loaded = registry.load_bundle(list(snapshot.get(kind) or []))
                contents[kind] = {"bundle": bundle, "loaded": loaded}
            bundle = contents[kind]["bundle"]
            loaded = contents[kind]["loaded"]
            self.skill_bundles[kind] = bundle
            for item in loaded:
                if item["id"] not in seen:
                    seen.add(item["id"])
                    loaded_all.append(item)
        self.skill_snapshot = loaded_all
        self.state["skills_snapshot"] = loaded_all
        self._activate_skills()
        self._save_state()

    def _get_run(self) -> dict[str, Any]:
        run = self.db.query_one("SELECT * FROM runs WHERE id = ?", (self.run_id,))
        if not run:
            raise RuntimeError("运行不存在")
        return run

    def _fresh_state(self) -> dict[str, Any]:
        self.run = self._get_run()
        return json.loads(self.run["state_json"] or "{}")

    def _save_state(self) -> None:
        self.db.execute(
            "UPDATE runs SET state_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(self.state, ensure_ascii=False), utc_now(), self.run_id),
        )

    def _mark_status(self, status: str, error: str | None = None) -> None:
        current = self._get_run()
        if current["status"] in {"COMPLETED", "COMPLETED_WITH_WARNINGS", "CANCELLED"} and status == "FAILED":
            return
        ended = utc_now() if status in {
            "COMPLETED",
            "COMPLETED_WITH_WARNINGS",
            "BUDGET_EXHAUSTED",
            "FAILED",
            "CANCELLED",
        } else None
        self.db.execute(
            "UPDATE runs SET status = ?, error = ?, updated_at = ?, ended_at = COALESCE(?, ended_at) WHERE id = ?",
            (status, error, utc_now(), ended, self.run_id),
        )
        self.events.publish(
            self.run_id,
            "run.status",
            {"status": status, "error": error, "phase": current["phase"]},
        )

    def _skill_kind(self) -> str:
        phase = str(self.state.get("phase") or "P0")
        stage = ""
        if phase == "P3":
            selected = self.state.get("selected") or []
            index = int(self.state.get("idea_next") or 0)
            if index < len(selected):
                stage = str(selected[index].get("stage") or "method")
        return skill_kind_for(phase, stage)

    def _activate_skills(self, kind: str | None = None) -> None:
        key = kind or self._skill_kind()
        self.skills_bundle = self.skill_bundles.get(key, "")

    def _set_phase(self, phase: str, label: str) -> None:
        self.state["phase"] = phase
        self._activate_skills()
        self._save_state()
        now = utc_now()
        self.db.execute(
            "UPDATE runs SET phase = ?, phase_label = ?, updated_at = ? WHERE id = ?",
            (phase, label, now, self.run_id),
        )
        self.db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, self.project["id"]))
        self.events.publish(self.run_id, "phase.started", {"phase": phase, "label": label})

    def _active_dispatch(self) -> dict[str, Any] | None:
        return (self.state.get("dispatch") or {}).get("active")

    def _publish_dispatch(self) -> None:
        self._save_state()
        self.events.publish(self.run_id, "dispatch.changed", {})

    def _begin_dispatch(
        self, *, stage: str, purpose: str, role: str, status: str = "scheduled",
        action: Action | None = None, participants: list[Participant] | None = None,
        turn: int | None = None, idea_id: str = "",
    ) -> dict[str, Any]:
        scheduler = describe_action(action) if action else {}
        key = scheduler.get("target_key") or role
        roster = [
            {
                "key": seat.key, "role": seat.role, "label": seat.label,
                "role_label": ROLE_LABELS.get(seat.role, seat.role),
                "model_label": self._profile_label(seat.profile) if seat.profile else "",
            }
            for seat in participants or []
        ]
        if not any(seat["key"] == key for seat in roster):
            label = ROLE_LABELS.get(role, role)
            roster.append({
                "key": key, "role": role, "label": "文献检索" if status == "retrieving" else label,
                "role_label": label, "model_label": "", "tool": status == "retrieving",
            })
        dispatch = {
            **scheduler,
            "id": new_id("dispatch"), "stage": stage, "purpose": purpose, "role": role,
            "status": status, "roster": roster, "target_key": key, "turn": turn,
            "idea_id": idea_id or scheduler.get("idea_id") or self.state.get("current_idea_id") or "",
            "actual_model": "", "message_id": None,
            "type": scheduler.get("type", "RETRIEVE" if status == "retrieving" else "CALL"),
        }
        self.state.setdefault("dispatch", {})["active"] = dispatch
        self._publish_dispatch()
        return dispatch

    def _update_dispatch(self, dispatch: dict[str, Any], **changes: Any) -> None:
        if self._active_dispatch() is dispatch and any(dispatch.get(key) != value for key, value in changes.items()):
            dispatch.update(changes)
            self._publish_dispatch()

    def _finish_dispatch(
        self, *, status: str = "completed", message_id: int | None = None, outcome: str = "",
    ) -> None:
        active = self._active_dispatch()
        if not active:
            return
        result = {**active, "status": status, "message_id": message_id or active.get("message_id"), "outcome": outcome}
        board = self.state["dispatch"]
        board["active"] = None
        board["last"] = result
        if active.get("turn") is not None or active.get("stage") in {"P2.1", "P2.3"}:
            board["history"] = [*(board.get("history") or []), result][-160:]
        self._publish_dispatch()

    def _record_message(
        self,
        role: str,
        content: str,
        structured: dict[str, Any] | None = None,
        message_type: str = "speech",
        *,
        speaker_label: str = "",
        input_entry: dict[str, Any] | None = None,
    ) -> int:
        label = speaker_label or ROLE_LABELS.get(role, role)
        created = utc_now()
        scope = current_scope(self.state)
        sql = (
            "INSERT INTO messages (run_id, role, role_label, message_type, content, structured_json, scope, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        params = (
            self.run_id, role, label, message_type, content,
            json.dumps(structured or {}, ensure_ascii=False), scope, created,
        )
        recent = {
            "scope": scope, "role": label, "content": content[:3000],
            "type": message_type, "created_at": created,
        }
        history = self.state.get("recent_messages", [])
        if input_entry is not None:
            inputs = (self.state.get("user_inputs", []) + [input_entry])[-20:]
            with self.db.connection() as connection:
                changed = connection.execute(
                    "UPDATE run_inputs SET consumed = 1 WHERE id = ? AND run_id = ? AND consumed = 0",
                    (input_entry["id"], self.run_id),
                ).rowcount
                if not changed:
                    return 0
                message_id = int(connection.execute(sql, params).lastrowid)
                recent_messages = (history + [{**recent, "id": message_id}])[-16:]
                connection.execute(
                    "UPDATE runs SET state_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps({
                        **self.state, "user_inputs": inputs, "recent_messages": recent_messages,
                    }, ensure_ascii=False), created, self.run_id),
                )
            self.state["user_inputs"] = inputs
        else:
            message_id = self.db.insert(sql, params)
            recent_messages = (history + [{**recent, "id": message_id}])[-16:]
        self.state["recent_messages"] = recent_messages
        dispatch = self._active_dispatch()
        if dispatch and dispatch.get("status") == "retrieving" and message_type == "literature":
            dispatch["message_id"] = message_id
        self.artifacts.append_message(
            self.project["id"],
            self.run_id,
            {
                "id": message_id,
                "role": role,
                "role_label": label,
                "type": message_type,
                "content": content,
                "created_at": created,
            },
        )
        self.events.publish(
            self.run_id,
            "message.created",
            {
                "id": message_id,
                "role": role,
                "role_label": label,
                "message_type": message_type,
                "content": content,
                "created_at": created,
            },
        )
        if input_entry is None:
            self._save_state()
        return message_id

    def _make_live_emitter(self, purpose: str, role: str):
        """把流式增量转发给前端 live feed；按 ~0.3s / 240 字符合批，避免事件风暴。"""
        feed = self.live_feed
        if feed is None:
            return None
        label = ROLE_LABELS.get(role, role)
        state: dict[str, Any] = {"reasoning": "", "content": "", "last": 0.0}

        def flush(force: bool = False) -> None:
            now = time.monotonic()
            if not force and now - state["last"] < 0.3:
                return
            payload: dict[str, Any] = {
                "type": "live.delta",
                "purpose": purpose[:150],
                "role_label": label,
            }
            moved = False
            for kind in ("reasoning", "content"):
                chunk = state[kind]
                if chunk:
                    payload[kind] = chunk
                    state[kind] = ""
                    moved = True
            if moved:
                feed.publish(self.run_id, payload)
            state["last"] = now

        def on_delta(kind: str, text: str) -> None:
            state[kind] = state.get(kind, "") + text
            if len(state["reasoning"]) + len(state["content"]) >= 240:
                flush(force=True)
            else:
                flush()

        def end() -> None:
            feed.publish(
                self.run_id, {"type": "live.call_end", "purpose": purpose[:150]}
            )

        class _Emitter:
            pass

        emitter = _Emitter()
        emitter.on_delta = on_delta
        emitter.flush = flush
        emitter.end = end
        return emitter

    def _record_thinking(self, role: str, purpose: str, reasoning: str) -> None:
        """Persist model reasoning for the UI only; it never enters agent context."""
        text = (reasoning or "").strip()
        if len(text) < 120:
            return
        label = ROLE_LABELS.get(role, role)
        created = utc_now()
        message_id = self.db.insert(
            "INSERT INTO messages (run_id, role, role_label, message_type, content, structured_json, scope, created_at) "
            "VALUES (?, ?, ?, 'thinking', ?, ?, ?, ?)",
            (
                self.run_id,
                role,
                label,
                f"[{purpose}]\n\n{text[:20000]}",
                "{}",
                current_scope(self.state),
                created,
            ),
        )
        self.events.publish(
            self.run_id,
            "thinking.created",
            {"id": message_id, "role": role, "purpose": purpose[:120]},
        )

    def _record_decision(self, title: str, content: str, rationale: str = "") -> None:
        created = utc_now()
        decision_id = self.db.insert(
            "INSERT INTO decisions (run_id, title, content, rationale, created_at) VALUES (?, ?, ?, ?, ?)",
            (self.run_id, title, content, rationale, created),
        )
        self.artifacts.append_decision(
            self.project["id"],
            self.run_id,
            {"id": decision_id, "title": title, "content": content, "rationale": rationale, "created_at": created},
        )
        self.events.publish(
            self.run_id,
            "decision.created",
            {"id": decision_id, "title": title, "content": content, "rationale": rationale},
        )
        digest = self.state.setdefault("decision_digest", [])
        digest.append(f"{title}：{content[:300]}" + (f"（理由：{rationale[:160]}）" if rationale else ""))
        self.state["decision_digest"] = digest[-20:]
        self._save_state()

    def _publish_budget(self) -> None:
        run = self._get_run()
        self.events.publish(
            self.run_id,
            "budget.updated",
            {"used": run["budget_used"], "max": run["budget_max"]},
        )

    def _consume_inputs(self) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT id, kind, content, created_at AS at FROM run_inputs "
            "WHERE run_id = ? AND consumed = 0 ORDER BY id ASC",
            (self.run_id,),
        )
        consumed = []
        for row in rows:
            message_id = self._record_message(
                "user",
                f"[{row['kind']}] {row['content']}",
                {"kind": row["kind"], "input_id": row["id"]},
                "user_input",
                input_entry=row,
            )
            if message_id:
                consumed.append(row)
        return consumed

    def _guard(self) -> dict[str, Any]:
        if self.cancel_requested:
            raise RunCancelled()
        current = self._get_run()
        if current["status"] == "CANCELLED":
            raise RunCancelled()
        if self.pause_requested or current["status"] == "PAUSED":
            raise RunPaused()
        self._check_cost_cap()
        return current

    def _check_cost_cap(self) -> None:
        """Plan §11.1: enforce the global cost budget when configured (0 = unlimited)."""
        settings_store = getattr(self, "settings_store", None)
        if settings_store is None:
            return
        cap = float(getattr(settings_store.load(), "max_run_cost_usd", 0.0) or 0.0)
        if cap <= 0:
            return
        row = self.db.query_one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM model_calls WHERE run_id = ?",
            (self.run_id,),
        )
        used = float((row or {}).get("total") or 0.0)
        self.state["cost_used"] = used
        if used > cap:
            raise BudgetExhausted(
                f"累计费用 ${used:.4f} 已超过运行费用上限 ${cap:.4f}；可在设置中提高 max_run_cost_usd 后恢复"
            )

    def _settings(self) -> AppSettings:
        return self.settings_store.load()

    def _settings_for(self, profile_id: str | None, *, role: str = "pi") -> ModelCallSettings:
        settings = self._settings()
        if profile_id is None:
            bindings = self._profile_ids_for(role, settings)
            if not bindings:
                raise ModelCallError(f"请在模型团队中为 {ROLE_LABELS.get(role, role)} 分配模型")
            profile_id = bindings[0]
        profile = settings.profile(profile_id)
        if profile is None:
            raise ModelCallError(f"模型档案 {profile_id} 不存在，请检查模型团队")
        call = settings.call_settings(profile)
        # Preserve legacy per-run limits as caps on the selected model's own limits.
        if limit := self.config.get("context_window_tokens"):
            call.context_window_tokens = min(call.context_window_tokens, int(limit))
        if limit := self.config.get("max_input_tokens"):
            call.max_input_tokens = min(call.max_input_tokens or int(limit), int(limit))
        return call

    ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
        "method_architect": ("method_design", "reasoning", "novelty_analysis"),
        "experiment_designer": ("experiment_design", "critical_review"),
        "domain_researcher_1": ("research", "novelty_analysis"),
        "literature_researcher": ("research", "novelty_analysis"),
        "coherence_editor": ("critical_review", "academic_writing"),
        "writer": ("academic_writing", "synthesis"),
        "pi": ("synthesis", "reasoning"),
    }

    def _profile_ids_for(self, role: str, settings: AppSettings | None = None) -> list[str]:
        """Bound profile ids for a role, ordered by capability match first (plan §2.2)."""
        settings = settings or self._settings()
        if role == "pi" and (override := self.config.get("pi_model_profile_id")):
            return [str(override)]
        bound = model_bindings(settings, role)
        if len(bound) <= 1:
            return bound
        wanted = self.ROLE_CAPABILITIES.get(role, ())

        def score(profile_id: str) -> int:
            profile = settings.profile(profile_id)
            strengths = {str(tag).strip().lower() for tag in (getattr(profile, "strengths", None) or [])}
            return -len([tag for tag in wanted if tag in strengths])

        return sorted(bound, key=lambda pid: (score(pid), bound.index(pid)))

    def _profile_label(self, profile_id: str | None) -> str:
        if not profile_id:
            return "未分配模型"
        profile = self._settings().profile(profile_id)
        return (profile.name or profile.id) if profile else profile_id

    def _remaining_calls(self) -> int:
        run = self._get_run()
        return max(0, int(run["budget_max"]) - int(run["budget_used"]))

    def _p2_reserve_calls(self) -> int:
        """Reserve P3/P4 plus the mandatory debate and decision for every Idea."""
        ideas = max(1, int(self.run.get("idea_count") or 1))
        participants = len(self._debate_participants())
        per_idea = 4 if self.config.get("deliverable") == "idea_brief" else 8
        return (per_idea + (participants if participants >= 2 else 0)) * ideas + 2

    def _p3_required_calls(self) -> int:
        stages = {
            "method": 7, "experiment": 6, "gate": 5, "draft": 4,
            "draft_a": 4, "draft_b": 3, "draft_c": 2, "coherence": 1,
            "redebate": 8, "redecide": 8,
            "revise": 3, "revise_a": 3, "revise_b": 2, "revise_c": 1,
            "save": 0, "done": 0,
        }
        if self.config.get("deliverable") == "idea_brief":
            stages.update(method=3, experiment=2, gate=1, redebate=4, redecide=4)
        selected = self.state.get("selected", [])[:int(self.run["idea_count"])]
        return 1 + sum(stages.get(item.get("stage", "method"), 7) for item in selected)

    def _debate_reserve_calls(self, target: dict[str, Any]) -> int:
        remaining = [
            item for item in self.state.get("selected", [])
            if idea_key(item) != idea_key(target)
            and not (self.state.get("idea_states", {}).get(idea_key(item)) or {}).get("debate_done")
        ]
        models = len(self._debate_participants())
        return self._p3_required_calls() + 1 + len(remaining) * (1 + (models if models >= 2 else 0))

    def _max_team_turns(self) -> int:
        return max(0, int(self.config.get("max_team_turns", 20) or 0))

    def _max_debate_turns(self) -> int:
        return max(0, int(self.config.get("max_debate_turns", 15) or 0))

    async def _prepare_context(self, purpose: str, force: bool = False, settings: ModelCallSettings | None = None) -> None:
        settings = settings or self._settings_for(None)
        manager = ContextManager(self.db, self.run_id, self.state, settings)
        base_tokens = 1800 + estimate_tokens(self.skills_bundle)
        phase = self.state.get("phase")
        if phase == "P3":
            reserve = self._p3_required_calls()
        elif phase in ("P0", "P1", "P1L", "P2"):
            reserve = self._p2_reserve_calls()
            floor = self.state.get("discussion") or {}
            if self.config.get("discussion_models") is not None and not floor.get("closed"):
                counts = floor.get("counts") or {}
                reserve += sum(max(0, int(self.run["team_rounds"]) - counts.get(member.key, 0))
                               for member in self._discussion_participants())
            if phase in ("P0", "P1", "P1L"):
                reserve += max(0, int(self.run["candidate_count"]) - int(self.state.get("candidate_next", 0))) + 2
        else:
            reserve = 2
        snapshot = await manager.prepare(
            purpose=purpose,
            base_tokens=base_tokens,
            summarize=self._summarize_context if self._remaining_calls() > reserve + 1 else None,
            force=force,
        )
        self._pending_snapshot_id = snapshot.id
        self._pending_snapshot_compressed = snapshot.compressed
        self._save_state()
        if snapshot.compressed:
            self.events.publish(
                self.run_id,
                "context.compacted",
                {
                    "run_id": self.run_id,
                    "scope": manager.scope,
                    "strategy": snapshot.strategy,
                    "estimated_tokens": snapshot.estimated_tokens,
                    "budget_tokens": snapshot.budget_tokens,
                    "summary_id": snapshot.summary_id,
                },
            )

    async def _summarize_context(
        self, existing: str, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        settings = self._settings_for(None).model_copy(update={"reasoning_effort": "off"})
        body = "\n\n".join(
            f"[{item.get('role') or '成员'}] {str(item.get('content') or '')[:1600]}"
            for item in messages
        )
        result = await self.gateway.complete(
            run_id=self.run_id,
            role="context_compressor",
            purpose="上下文压缩",
            messages=[
                {"role": "system", "content": SUMMARY_SYSTEM},
                {
                    "role": "user",
                    "content": SUMMARY_REQUEST.format(
                        existing=existing or "无",
                        messages=body or "无",
                    ),
                },
            ],
            settings=settings,
            temperature=0.1,
            max_tokens=4000,
        )
        self._publish_budget()
        if result.finish_reason == "length":
            raise ModelCallError("上下文摘要被输出上限截断")
        return extract_json(result.content)

    async def _call_raw(
        self,
        *,
        role: str,
        purpose: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        profile_id: str | None = None,
    ):
        self._guard()
        settings = self._settings_for(profile_id, role=role)
        window = max(4_000, int(settings.context_window_tokens))
        budget = min(
            int(settings.max_input_tokens or int(window * 0.70)),
            int(window * 0.90),
        )
        estimated = estimate_messages(messages)
        strategy = "call"
        trigger = budget * float(settings.context_compression_trigger)
        if (
            settings.context_compression_enabled
            and estimated > trigger
            and not self._pending_snapshot_compressed
        ):
            try:
                await self._prepare_context(f"{purpose} / 自动压缩", force=True, settings=settings)
            except Exception as exc:
                self.state["last_compression_error"] = str(exc)[:500]
        run = self._guard()
        if run["budget_used"] >= run["budget_max"]:
            raise BudgetExhausted(f"已达到 {run['budget_max']} 次 LLM 调用上限")
        new_inputs = self._consume_inputs()
        inputs = new_inputs if len(new_inputs) > 6 else self.state.get("user_inputs", [])[-6:]
        if inputs:
            guidance = "\n".join(f"- [{item['kind']}] {item['content']}" for item in inputs)
            note = (
                "\n\n项目发起人追加的信息（按顺序，较新的明确决策优先）：\n"
                "将适用的约束与决策带入当前任务，把证据或观察按用户提供的材料处理，"
                "并保持本次要求的输出格式。\n" + guidance
            )
            if messages and messages[-1]["role"] == "user":
                messages = [*messages[:-1], {**messages[-1], "content": messages[-1]["content"] + note}]
            else:
                messages = [*messages, {"role": "user", "content": note.lstrip()}]
            estimated = estimate_messages(messages)
        if estimated > budget:
            messages = hard_trim_messages(messages, budget)
            messages = fit_messages_to_budget(messages, budget)
            estimated = estimate_messages(messages)
            strategy = "prompt_trim"
        snapshot_id = self._pending_snapshot_id
        self._pending_snapshot_id = None
        self._pending_snapshot_compressed = False
        if snapshot_id is None:
            snapshot_id = self.db.insert(
                "INSERT INTO context_snapshots "
                "(run_id, phase, purpose, estimated_tokens, budget_tokens, ratio, compressed, strategy, "
                "details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.run_id,
                    str(self.state.get("phase") or ""),
                    purpose,
                    estimated,
                    budget,
                    round(estimated / budget, 4) if budget else 0,
                    1 if strategy != "call" else 0,
                    strategy,
                    json.dumps({"scope": current_scope(self.state)}, ensure_ascii=False),
                    utc_now(),
                ),
            )
        else:
            self.db.execute(
                "UPDATE context_snapshots SET estimated_tokens = ?, budget_tokens = ?, ratio = ? WHERE id = ?",
                (estimated, budget, round(estimated / budget, 4) if budget else 0, snapshot_id),
            )
        dispatch = self._active_dispatch()
        owns_dispatch = dispatch is None
        if dispatch is None:
            dispatch = self._begin_dispatch(
                stage=purpose.split(" ", 1)[0], purpose=purpose, role=role,
            )
        self._update_dispatch(dispatch, status="waiting", actual_model=settings.model)
        emitter = self._make_live_emitter(purpose, role)

        def on_delta(kind: str, text: str) -> None:
            if kind == "content":
                self._update_dispatch(dispatch, status="streaming")
            elif dispatch["status"] == "waiting":
                self._update_dispatch(dispatch, status="thinking")
            if emitter:
                emitter.on_delta(kind, text)

        result = None
        try:
            result = await self.gateway.complete(
                run_id=self.run_id,
                role=role,
                purpose=purpose,
                messages=messages,
                settings=settings,
                temperature=temperature,
                max_tokens=max_tokens,
                on_delta=on_delta,
            )
        finally:
            if emitter:
                emitter.flush(force=True)
                emitter.end()
            if owns_dispatch:
                self._finish_dispatch(status="completed" if result is not None else "interrupted")
            elif result is not None:
                self._update_dispatch(dispatch, status="processing")
        self._last_response_model = settings.model
        if result.reasoning_text:
            self._record_thinking(role, purpose, result.reasoning_text)
        self.db.execute(
            "UPDATE context_snapshots SET actual_prompt_tokens = ?, model_call_id = ? WHERE id = ?",
            (result.prompt_tokens, result.call_id, snapshot_id),
        )
        self._publish_budget()
        return result

    async def _call_json(
        self,
        *,
        role: str,
        purpose: str,
        messages: list[dict[str, str]],
        temperature: float = 0.4,
        max_tokens: int = 8000,
        profile_id: str | None = None,
        contract: str | None = None,
    ) -> dict[str, Any]:
        def parse(result) -> dict[str, Any]:
            try:
                if result.finish_reason == "length":
                    raise ValueError("JSON 输出被输出上限截断")
                value = extract_json(result.content)
                return validate_model_output(contract, value) if contract else value
            except ValueError as exc:
                if result.call_id:
                    self.db.execute(
                        "UPDATE model_calls SET status = 'invalid_output', error = ? WHERE id = ?",
                        (str(exc)[:1000], result.call_id),
                    )
                raise

        result = await self._call_raw(
            role=role,
            purpose=purpose,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            profile_id=profile_id,
        )
        try:
            return parse(result)
        except ValueError as exc:
            repair_messages = list(messages)
            repair_messages.append({"role": "assistant", "content": result.content[:12000]})
            repair_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"上面的回答不完整或不是合法 JSON（原因：{exc}）。"
                        "请保持原有内容含义，压缩重复表述，只返回一个完整、合法、可解析的 JSON 对象。"
                        "注意：不要代码围栏；不要注释；不要尾随逗号；"
                        "字符串内部不要直接换行（需要换行请写成 \\n）；使用双引号。"
                    ),
                }
            )
            repaired = await self._call_raw(
                role=role,
                purpose=f"{purpose} / JSON 修复",
                messages=repair_messages,
                temperature=0.1,
                max_tokens=min(24000, max_tokens + 6000),
                profile_id=profile_id,
            )
            try:
                return parse(repaired)
            except ValueError as repaired_error:
                raise InvalidModelOutput(f"{purpose} 的 JSON 修复后仍不符合契约：{repaired_error}") from repaired_error

    async def _call_gate(self, **kwargs: Any) -> dict[str, Any]:
        try:
            return await self._call_json(contract="gate", **kwargs)
        except InvalidModelOutput as exc:
            return {
                "verdict": "unconfirmed",
                "issues": [{"problem": "方法与实验的一致性判定缺失", "fix": str(exc)}],
            }

    async def _call_markdown(
        self,
        *,
        role: str,
        purpose: str,
        messages: list[dict[str, str]],
        temperature: float = 0.55,
        max_tokens: int = 12000,
        required_markers: list[str] | None = None,
        profile_id: str | None = None,
    ) -> str:
        markers = required_markers or []
        current_messages = list(messages)
        for attempt in range(2):
            result = await self._call_raw(
                role=role,
                purpose=purpose if attempt == 0 else f"{purpose} / 完整性重写",
                messages=current_messages,
                temperature=temperature if attempt == 0 else 0.25,
                max_tokens=max_tokens,
                profile_id=profile_id,
            )
            text = strip_markdown_fence(result.content)
            missing = [marker for marker in markers if marker not in text]
            if result.finish_reason != "length" and not missing:
                return text
            if attempt == 0:
                current_messages = list(messages)
                current_messages.append({"role": "assistant", "content": text[:12000]})
                instruction = (
                    "上一次输出被截断或没有写完本部分。请保持论证质量，但压缩重复表述，"
                    "用更精炼的文段完整重写这一部分，必须包含："
                    + "、".join(markers or ["本部分要求的全部章节"])
                    + "。不要解释，不要输出代码围栏。"
                )
                current_messages.append({"role": "user", "content": instruction})
        raise ModelCallError(
            f"{purpose} 连续两次输出不完整"
            + (f"，缺少：{', '.join(markers)}" if markers else "")
        )

    async def _panel_json(
        self,
        *,
        role: str,
        purpose: str,
        kind: str,
        idea: dict[str, Any],
        messages: list[dict[str, str]],
        profiles: list[str],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        progress = self.state.setdefault("panel_progress", {})
        checkpoint = progress.setdefault(purpose, {"proposals": []})
        proposals = checkpoint["proposals"]
        for item in proposals[:]:
            try:
                validate_model_output(kind, item["result"])
            except ValueError:
                proposals.remove(item)
                checkpoint.pop("synthesis", None)
        skipped = checkpoint.setdefault("invalid_profiles", [])
        completed = {item["profile_id"] for item in proposals} | set(skipped)
        for profile_id in profiles:
            if profile_id in completed:
                continue
            label = self._profile_label(profile_id)
            try:
                result = await self._call_json(
                    role=role,
                    purpose=f"{purpose} / {label}",
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    profile_id=profile_id,
                    contract=kind,
                )
            except ModelCallError as exc:
                skipped.append(profile_id)
                self._record_message(
                    "pi", f"未取得 {label} 的有效提案，本次汇总使用其余模型的结果。", {"profile_id": profile_id, "error": str(exc)}, "system"
                )
                self._save_state()
                continue
            proposals.append({"profile_id": profile_id, "profile": label, "result": result})
            self._record_message(
                role,
                f"{label} 的{kind}提案：{result.get('speech') or result.get('core_insight') or '已提交'}",
                {"profile_id": profile_id, "kind": kind, "result": result},
                "panel",
            )
            self._save_state()
        if not proposals:
            # A subsequent resume may use a corrected provider or prompt.
            checkpoint["invalid_profiles"] = []
            self._save_state()
            raise InvalidModelOutput(f"{purpose} 未获得符合契约的提案")
        if len(proposals) == 1:
            return {**proposals[0]["result"], "panel_proposals": proposals}
        synthesis = checkpoint.get("synthesis")
        if synthesis is not None:
            try:
                validate_model_output(kind, synthesis)
            except ValueError:
                synthesis = None
        if synthesis is None:
            synthesis = await self._call_json(
                role="pi",
                purpose=f"{purpose} 多模型综合",
                messages=panel_synthesis_messages(
                    self.project, self.run, self.skills_bundle, idea, kind, proposals
                ),
                temperature=0.3,
                max_tokens=max_tokens,
                profile_id=(self._profile_ids_for("pi") or [None])[0],
                contract=kind,
            )
            checkpoint["synthesis"] = synthesis
            self._save_state()
        return {**synthesis, "panel_proposals": proposals}

    async def _drive(self) -> None:
        while True:
            self._guard()
            phase = self.state.get("phase", "P0")
            if self.run["mode"] == "REVISE" and phase.startswith("R"):
                await self._drive_revision_phase(phase)
            elif phase == "P0":
                await self._phase_p0()
            elif phase == "P1":
                await self._phase_p1()
            elif phase == "P1L":
                await self._phase_p1l()
            elif phase == "P2":
                await self._phase_p2()
            elif phase == "P3":
                await self._phase_p3()
            elif phase == "P4":
                await self._phase_p4()
            elif phase == "DONE":
                return
            else:
                raise RuntimeError(f"未知阶段：{phase}")

    async def _phase_p0(self) -> None:
        if self.state.get("p0_done"):
            self._set_phase("P1", "独立构思")
            return
        self._set_phase("P0", "项目定义")
        if "project_memory" not in self.state:
            self.state["project_memory"] = self._project_memory()
            self._save_state()
        await self._prepare_context("P0 项目定义")
        result = await self._call_json(
            role="pi",
            purpose="P0 项目定义与议程",
            contract="agenda",
            messages=pi_plan_messages(self.project, self.run, self.skills_bundle, self.state["project_memory"]),
            temperature=0.45,
            max_tokens=18000,
        )
        self.state["agenda"] = result.get("agenda", "")
        self.state["problem_frame"] = result.get("problem_frame", "")
        self.state["hard_constraints"] = result.get("hard_constraints", []) or []
        self.state["soft_guidance"] = result.get("soft_guidance", []) or []
        self.state["directions"] = result.get("directions", []) or []
        self.state["selection_criteria"] = result.get("selection_criteria", []) or []
        self.state["p0_done"] = True
        self._record_message("pi", result.get("speech") or self.state["agenda"], result)
        self._record_decision(
            "项目议程",
            self.state["agenda"],
            json.dumps(result.get("selection_criteria", []), ensure_ascii=False),
        )
        self._set_phase("P1", "独立构思")

    def _project_memory(self) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT i.id, i.title, i.summary, v.version, v.manifest_json FROM ideas i "
            "JOIN idea_versions v ON v.idea_id = i.id AND v.version = i.current_version "
            "WHERE i.project_id = ? AND i.run_id != ? ORDER BY i.updated_at DESC LIMIT 6",
            (self.project["id"], self.run_id),
        )
        memory = []
        for row in rows:
            manifest = json.loads(row["manifest_json"] or "{}")
            if not isinstance(manifest, dict):
                continue
            decision = manifest.get("consensus")
            decision = decision if isinstance(decision, dict) else {}
            card = manifest.get("validation_card")
            card = card if isinstance(card, dict) else {}
            unresolved = decision.get("unresolved")
            memory.append({
                "idea_id": row["id"],
                "version": row["version"],
                "title": row["title"],
                "hypothesis": str(decision.get("thesis") or row["summary"] or "")[:450],
                "validation_status": card.get("status") or "unknown",
                "unresolved": unresolved[:3] if isinstance(unresolved, list) else [],
            })
        return memory

    async def _phase_p1(self) -> None:
        lenses = [
            "问题重构",
            "机制迁移",
            "经验观察转机制",
            "系统与资源约束",
            "评价协议缺口",
            "跨方向组合",
            "简洁反例",
            "新场景设定",
            "鲁棒性与失败边界",
            "理论与可解释性",
            "数据或基准设计",
            "部署与人机协作",
        ]
        while int(self.state.get("candidate_next", 0)) < int(self.run["candidate_count"]):
            self._guard()
            index = int(self.state.get("candidate_next", 0))
            lens = lenses[index % len(lenses)]
            await self._prepare_context(f"P1 独立候选 {index + 1}")
            attempts = int(self.state.get("candidate_attempts", 0))
            try:
                result = await self._call_json(
                    role=f"domain_researcher_{(index % 3) + 1}",
                    purpose=f"P1 独立候选 {index + 1}",
                    contract="candidate",
                    messages=candidate_messages(
                        self.project, self.run, self.skills_bundle, self.state, index, lens
                    ),
                    temperature=0.75,
                    max_tokens=8000,
                )
            except (ValueError, ModelCallError) as exc:
                if attempts < 1:
                    self.state["candidate_attempts"] = attempts + 1
                    self._record_message(
                        "pi",
                        f"候选 {index + 1} 的输出未能解析为合法 JSON（{exc}），正在重试一次。",
                        {"retry": True, "candidate": index + 1},
                        "system",
                    )
                    self._save_state()
                    continue
                self.state["candidate_attempts"] = 0
                self.state["candidate_next"] = index + 1
                self._record_message(
                    "pi",
                    f"候选 {index + 1} 连续两次生成失败，已跳过；讨论继续使用其余候选。",
                    {"skipped": True, "candidate": index + 1},
                    "system",
                )
                self._save_state()
                continue
            self.state["candidate_attempts"] = 0
            result["id"] = f"candidate_{index + 1}"
            result["lens"] = lens
            if not result.get("title"):
                raise RuntimeError("候选缺少标题")
            self.state.setdefault("candidates", []).append(result)
            speech = result.get("speech") or (
                f"候选：{result.get('title')}\n{result.get('one_sentence', '')}\n"
                f"缺口：{result.get('gap', '')}\n机制：{result.get('mechanism', '')}"
            )
            self._record_message(
                f"domain_researcher_{(index % 3) + 1}", speech, result
            )
            self.state["candidate_next"] = index + 1
            self._save_state()
        self._check_candidate_diversity()
        self._set_phase("P1L", "文献调研")

    def _check_candidate_diversity(self) -> None:
        """轻量聚类：只标记机制相近的候选，不删除、不改写。"""
        candidates = self.state.get("candidates", [])
        if len(candidates) < 2:
            return

        def tokens(item: dict[str, Any]) -> set[str]:
            text = " ".join(
                str(item.get(key) or "")
                for key in ("title", "one_sentence", "mechanism", "gap")
            )
            words = set(re.findall(r"[a-z][a-z0-9\-]{2,}", text.lower()))
            cjk = re.sub(r"[^\u4e00-\u9fff]", "", text)
            words.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
            return words

        sets = [tokens(item) for item in candidates]
        parent = list(range(len(candidates)))

        def find(node: int) -> int:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: int, right: int) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        pairs: list[dict[str, Any]] = []
        for left in range(len(candidates)):
            for right in range(left + 1, len(candidates)):
                shared = len(sets[left] & sets[right])
                union_size = len(sets[left] | sets[right]) or 1
                score = shared / union_size
                if shared >= 4 and score >= 0.35:
                    union(left, right)
                    pairs.append(
                        {
                            "left": candidates[left]["id"],
                            "right": candidates[right]["id"],
                            "score": round(score, 2),
                        }
                    )
        groups: dict[int, list[str]] = {}
        for index, item in enumerate(candidates):
            groups.setdefault(find(index), []).append(item["id"])
        group_list = [items for items in groups.values() if len(items) > 1]
        for group_index, ids in enumerate(group_list, start=1):
            for candidate in candidates:
                if candidate["id"] in ids:
                    candidate["duplicate_group"] = f"G{group_index}"
        self.state["candidate_diversity"] = {"groups": group_list, "pairs": pairs}
        if group_list:
            self._record_message(
                "pi",
                "候选多样性检查：发现 "
                + "、".join(" / ".join(ids) for ids in group_list)
                + " 的机制可能相近；仅标记，不删除，取舍时需说明差异化路径。",
                self.state["candidate_diversity"],
                "system",
            )
            self._save_state()

    def _derive_literature_query(self, candidate: dict[str, Any]) -> str:
        for key in ("title", "one_sentence"):
            text = str(candidate.get(key) or "").strip()
            if text:
                return text[:120]
        return ""

    async def _run_literature_queries(
        self, queries: Any, title: str, label: str, idea_id: str = ""
    ) -> list[dict[str, Any]]:
        """Run live literature/web queries. Pure API calls: no LLM budget is consumed."""
        clean: list[str] = []
        for query in queries or []:
            text = str(query).strip()
            if text and text not in clean:
                clean.append(text)
            if len(clean) >= 2:
                break
        if not clean:
            return []
        owns_dispatch = self._active_dispatch() is None
        if owns_dispatch:
            self._begin_dispatch(
                stage=str(self.state.get("phase") or ""), purpose=label, role="literature_researcher",
                status="retrieving", idea_id=idea_id,
            )
        completed = False
        try:
            records = await self._collect_literature(clean, title, label, idea_id)
            completed = True
            return records
        finally:
            if owns_dispatch:
                self._finish_dispatch(status="completed" if completed else "interrupted")

    async def _collect_literature(
        self, clean: list[str], title: str, label: str, idea_id: str,
    ) -> list[dict[str, Any]]:
        settings = self._settings()
        lines: list[str] = []
        compact: list[dict[str, Any]] = []
        steps: list[dict[str, Any]] = []
        for query in clean:
            try:
                result = await check_literature(
                    title=title,
                    query=query,
                    limit=4,
                    tavily_api_key=settings.tavily_api_key,
                )
            except Exception as exc:
                result = {
                    "query": query,
                    "status": "ERROR",
                    "works": [],
                    "sources": [],
                    "steps": [f"检索失败：{exc}"],
                }
            steps.append(
                {
                    "query": query,
                    "status": result.get("status"),
                    "sources": result.get("sources", []),
                }
            )
            for work in result.get("works", [])[:4]:
                compact.append(
                    {
                        "query": query,
                        "source": work.get("source", ""),
                        "title": work.get("title", ""),
                        "year": work.get("year"),
                        "venue": work.get("venue", ""),
                        "url": work.get("url") or work.get("doi", ""),
                        "doi": work.get("doi", ""),
                        "abstract": str(work.get("abstract") or "")[:500],
                    }
                )
                lines.append(
                    f"- [{work.get('source', '')}] {work.get('title', '')} "
                    f"({work.get('year') or '年份未知'}, {work.get('venue') or 'venue 未知'})"
                )
        digest = [
            f"{item['title']} ({item.get('year') or 'n/a'}, {item.get('source')})"
            for item in compact[:10]
        ]
        if digest:
            self.state["recent_literature"] = (
                self.state.get("recent_literature") or []
            )[-14:] + digest
            if idea_id:
                by_idea = self.state.setdefault("literature_by_idea", {})
                by_idea[idea_id] = (by_idea.get(idea_id) or [])[-16:] + digest
        records = record_evidence(
            self.state, compact, query=" | ".join(clean), idea_id=idea_id
        )
        if compact:
            self._record_message(
                "literature_researcher",
                f"文献检索（{label}）：\n" + "\n".join(lines),
                {
                    "label": label,
                    "steps": steps,
                    "works": compact,
                    "evidence": [
                        {
                            "id": item["id"],
                            "title": item["title"],
                            "verification": item["verification"],
                        }
                        for item in records
                    ],
                },
                "literature",
            )
            self._save_state()
        return records

    async def _phase_p1l(self) -> None:
        candidates = self.state.get("candidates", [])
        while int(self.state.get("lit_next", 0)) < len(candidates):
            self._guard()
            index = int(self.state.get("lit_next", 0))
            candidate = candidates[index]
            queries = [
                str(query).strip()
                for query in (candidate.get("literature_queries") or [])
                if str(query).strip()
            ][:3]
            if not queries:
                fallback = self._derive_literature_query(candidate)
                queries = [fallback] if fallback else []
            works = await self._run_literature_queries(
                queries,
                candidate.get("title", ""),
                f"候选 {index + 1} · {candidate.get('title', '')[:40]}",
                idea_id=candidate["id"],
            )
            candidate["literature"] = {"queries": queries, "works": works[:8]}
            self.state["lit_next"] = index + 1
            self._save_state()
        if not self.state.get("lit_review_done"):
            if self._remaining_calls() < 2:
                self._record_message(
                    "pi",
                    "剩余调用预算不足以完成文献综述，跳过综述；检索结果已广播并写入候选。",
                    {"skipped": True},
                    "literature",
                )
                self.state["lit_review_done"] = True
                self._save_state()
            else:
                self._guard()
                await self._prepare_context("P1L 文献综述")
                result = await self._call_json(
                    role="literature_researcher",
                    purpose="P1L 文献综述",
                    contract="literature",
                    messages=literature_review_messages(
                        self.project, self.run, self.skills_bundle, self.state
                    ),
                    temperature=0.3,
                    max_tokens=8000,
                )
                candidate_map = {item["id"]: item for item in self.state.get("candidates", [])}
                for item in result.get("candidates", []) or []:
                    if isinstance(item, dict) and item.get("candidate_id") in candidate_map:
                        candidate_map[item["candidate_id"]]["literature_review"] = item
                self._record_message(
                    "literature_researcher",
                    result.get("speech") or "文献综述完成。",
                    result,
                )
                self.state["lit_review_done"] = True
                self._save_state()
        self._set_phase("P2", "团队讨论")

    async def _phase_p2(self) -> None:
        ensure_research_state(self.state)
        await self._run_discussion()
        if not self.state.get("selection_done"):
            await self._select_ideas()
        selected = self.state.get("selected") or []
        for index, idea in enumerate(selected):
            if index >= int(self.run["idea_count"]):
                break
            await self._deliberate_idea(idea)
        self.state.pop("current_idea_id", None)
        self._set_phase("P3", "分 Idea 深化")

    async def _select_ideas(self) -> None:
        self._guard()
        await self._prepare_context("P2.2 路线取舍")
        result = await self._call_json(
            role="pi",
            purpose="P2.2 路线取舍",
            contract="selection",
            messages=selection_messages(self.project, self.run, self.skills_bundle, self.state),
            temperature=0.35,
            max_tokens=18000,
        )
        candidates = self.state.get("candidates", [])
        candidate_map = {item["id"]: item for item in candidates}
        assessments = {
            str(item.get("candidate_id")): item
            for item in result.get("assessment") or []
            if isinstance(item, dict) and item.get("candidate_id")
        }
        rejected_ids = {
            str(item.get("candidate_id") or item.get("id") or "")
            for item in (result.get("rejected") or [])
            if isinstance(item, dict)
        }
        rejected_ids.update(key for key, item in assessments.items() if item.get("status") == "rejected")
        selected: list[dict[str, Any]] = []
        for item in result.get("selected", []):
            if not isinstance(item, dict):
                continue
            candidate = candidate_map.get(item.get("candidate_id"))
            if not candidate or candidate["id"] in rejected_ids or any(entry["candidate_id"] == candidate["id"] for entry in selected):
                continue
            merged = {**candidate, **{key: value for key, value in item.items() if value}}
            merged["candidate_id"] = candidate["id"]
            merged["status"] = "selected"
            selected.append(merged)
            if len(selected) >= int(self.run["idea_count"]):
                break
        if len(selected) < int(self.run["idea_count"]):
            for candidate in candidates:
                if (
                    candidate["id"] not in rejected_ids
                    and assessments.get(candidate["id"], {}).get("status") != "parked"
                    and all(item["candidate_id"] != candidate["id"] for item in selected)
                ):
                    selected.append({**candidate, "candidate_id": candidate["id"], "status": "selected"})
                if len(selected) >= int(self.run["idea_count"]):
                    break
        backup_id = str(result.get("backup_candidate_id") or result.get("fallback_candidate_id") or "")
        selected_ids = {item["candidate_id"] for item in selected}
        excluded = selected_ids | rejected_ids
        backup = candidate_map.get(backup_id) if backup_id not in excluded else None
        if not backup:
            backup = next(
                (
                    item
                    for item in candidates
                    if item["id"] not in excluded
                ),
                None,
            )
        for candidate in candidates:
            if candidate["id"] in assessments:
                candidate["assessment"] = assessments[candidate["id"]]
            if candidate["id"] in selected_ids:
                candidate["status"] = "selected"
            elif backup and candidate["id"] == backup.get("id"):
                candidate["status"] = "backup"
            elif candidate["id"] in rejected_ids:
                candidate["status"] = "rejected"
            else:
                candidate["status"] = "parked"

        self.state["selected"] = selected
        self.state["backup_candidate"] = backup or {}
        self.state["fallback_candidate_id"] = (backup or {}).get("id", "")
        self.state["selection_done"] = True
        self._record_message("pi", result.get("speech") or "已完成路线取舍。", result)
        self._record_decision(
            "Idea 组合选择",
            "\n".join(f"- {item.get('title')}" for item in selected) or "本轮没有达到深化条件的路线。",
            result.get("decision_rationale", ""),
        )
        self._save_state()

    async def _deliberate_idea(self, idea: dict[str, Any]) -> None:
        item = ensure_idea_state(self.state, idea)
        if item.get("debate_done"):
            return
        self.state["current_idea_id"] = idea_key(idea)
        if self._remaining_calls() <= self._p3_required_calls():
            self._record_message(
                "pi",
                f"预算不足以对《{idea.get('title', '')}》进行辩论与决策，保留候选方案直接进入深化。",
                {"skipped": True, "idea_id": idea_key(idea)},
                "debate",
            )
            self.state.setdefault("idea_states", {}).setdefault(idea_key(idea), {})[
                "debate_done"
            ] = True
            idea.setdefault(
                "consensus",
                {
                    "thesis": idea.get("one_sentence", ""),
                    "core_mechanism": idea.get("mechanism", ""),
                    "unresolved": [],
                    "decision_rationale": "预算不足，未执行辩论与决策，沿用候选方案。",
                },
            )
            self._save_state()
            return
        focus = str(idea.pop("rollback_focus", "") or "")
        await self._run_debate_floor(idea, focus)
        if self._remaining_calls() > self._p3_required_calls():
            await self._decide_idea(idea)
        else:
            fallback = {
                "thesis": idea.get("one_sentence", ""),
                "core_mechanism": idea.get("debate_plan", ""),
                "unresolved": unresolved_for(self.state, idea_key(idea)),
                "decision_rationale": "预算到达 P3 保留线，以最后一版辩论方案作为决策版本。",
            }
            idea["consensus"] = fallback
            idea["decision"] = fallback
            record_decision(self.state, idea, fallback, unresolved=fallback["unresolved"])
            self._record_message(
                "pi",
                "预算到达 P3 保留线，未单独调用决策；以最后一版辩论方案作为决策版本。",
                fallback,
                "debate",
            )
        self.state.setdefault("idea_states", {}).setdefault(idea_key(idea), {})[
            "debate_done"
        ] = True
        self._save_state()

    def _model_discussion_participants(self) -> list[Participant] | None:
        roster = self.config.get("discussion_models")
        if roster is None:
            return None
        settings = self._settings()
        roles: dict[str, str] = {}
        for role in ("method_architect", "experiment_designer"):
            for profile_id in model_bindings(settings, role)[:1]:
                roles.setdefault(profile_id, role)
        participants = []
        for profile_id in roster:
            profile = settings.profile(profile_id)
            if profile is None:
                raise ModelCallError(f"本次讨论的模型档案 {profile_id} 不存在")
            participants.append(Participant(
                key=f"model|{profile_id}", role=roles.get(profile_id, "domain_researcher_1"),
                label=profile.name or profile.model, profile=profile_id,
            ))
        return participants

    def _discussion_participants(self) -> list[Participant]:
        participants = self._model_discussion_participants()
        if participants is not None:
            return participants
        return [
            Participant(key=role, role=role, label=ROLE_LABELS[role],
                        profile=next(iter(self._profile_ids_for(role)), None))
            for role in ("domain_researcher_1", "method_architect", "experiment_designer")
        ]

    async def _run_discussion(self) -> None:
        """P2.1：Action 驱动，参与保障完成后按问题优先级动态发言。"""
        participants = self._discussion_participants()
        floor = self.state.get("discussion")
        if not floor:
            legacy = self.state.get("review_floor") or {}
            floor = {
                "counts": legacy.get("counts", {}),
                "turns": int(legacy.get("turns", 0)),
                "last": legacy.get("last", ""),
                "closed": False,
                "low_gain": 0,
                "requests": [],
                "mentions": {},
                "actions": [],
                "close_reason": "",
            }
            self.state["discussion"] = floor
        if floor.get("closed"):
            return
        required = {item.key: max(1, int(self.run["team_rounds"])) for item in participants}
        label_map = {item.label: item.key for item in participants}
        for item in participants:
            label_map.setdefault(item.role, item.key)
        while not floor.get("closed"):
            self._guard()
            ctx = self._discussion_context(floor, participants, required)
            action = plan_next_action(ctx)
            if not action:
                self._close_discussion(floor, required, self._floor_stop_reason(ctx))
                break
            if action.type == "RETRIEVE":
                self._begin_dispatch(
                    stage="P2.1", purpose=action.instruction, role="literature_researcher",
                    status="retrieving", action=action, participants=participants,
                )
                await self._execute_retrieval(action)
                floor["actions"].append(describe_action(action))
                floor["actions"] = floor["actions"][-80:]
                self._finish_dispatch()
                self._save_state()
                continue
            target = action.target
            if not target:
                self._close_discussion(floor, required, "没有可执行的发言动作")
                break
            turn = int(floor.get("turns", 0)) + 1
            spoken = int(floor.get("counts", {}).get(target.key, 0))
            purpose = f"P2.1 团队讨论 {turn} / {target.label} · {action.type}"
            self._begin_dispatch(
                stage="P2.1", purpose=purpose, role=target.role, action=action, participants=participants, turn=turn,
            )
            await self._prepare_context(purpose)
            self._last_response_model = ""
            result = await self._call_json(
                role=target.role,
                purpose=purpose,
                contract="discussion",
                messages=review_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    self.state,
                    target.role,
                    spoken,
                    describe_action(action),
                    [item.label for item in participants],
                    speaker_label=target.label,
                ),
                temperature=0.5,
                max_tokens=18000,
                profile_id=target.profile,
            )
            result["role"] = target.label
            result["actual_model"] = self._last_response_model
            result["turn"] = turn
            routes = accept_routes(self.state, result.get("new_routes"), turn)
            result["accepted_routes"] = [item["id"] for item in routes]
            self.state.setdefault("review_notes", []).append(result)
            is_pass = str(result.get("stance") or "").lower() == "pass"
            message_id = self._record_message(
                target.role,
                result.get("speech") or ("本轮没有新增判断。" if is_pass else "已提交讨论意见。"),
                {**result, "action": action.type, "scheduler": describe_action(action)},
                speaker_label=target.label,
            )
            added = self._add_result_issues(
                result, target, turn, "P2.1 讨论", action.idea_id, label_map
            )
            touched, resolved = self._apply_result_responses(result, target, turn)
            if added:
                self._record_message(
                    "pi",
                    f"{target.label} 提出 {len(added)} 个新问题："
                    + "、".join(issue["id"] for issue in added),
                    {"issues": [issue["id"] for issue in added]},
                    "debate",
                )
            self._finish_dispatch(
                message_id=message_id,
                outcome=f"新增 {len(routes)} 条候选路线" if routes else "跳过发言" if is_pass else f"解决 {resolved} 条异议" if resolved else "",
            )
            await self._run_literature_queries(
                result.get("literature_queries"),
                "团队讨论",
                f"P2.1 第{turn}次发言 {target.label} 提出的检索",
                idea_id=action.idea_id,
            )
            gain = self._absorb_turn(
                floor, result, target, action, turn, "P2.1 讨论", action.idea_id, added, resolved, is_pass
            )
            floor.setdefault("counts", {})[target.key] = spoken + 1
            floor["turns"] = turn
            floor["last"] = target.key
            self._note_mentions(floor, result, added, action.idea_id)
            self._advance_floor_requests(floor, action, result, label_map, target.key)
            floor.setdefault("actions", []).append(
                {**describe_action(action), "information_gain": gain}
            )
            floor["actions"] = floor["actions"][-80:]
            self._save_state()

    def _discussion_context(
        self,
        floor: dict[str, Any],
        participants: list[Participant],
        required: dict[str, int],
    ) -> FloorContext:
        counts = dict(floor.get("counts", {}))
        reserve = self._p2_reserve_calls()
        if len(self._debate_participants()) >= 2 and all(
            counts.get(key, 0) >= minimum for key, minimum in required.items()
        ):
            # Give each Idea room for follow-up after all models have spoken.
            reserve += 2 * int(self.run["idea_count"])
        return FloorContext(
            phase="discussion",
            participants=participants,
            counts=counts,
            required=required,
            issues=open_issues(self.state),
            requests=floor.get("requests", []),
            ideas=[item for item in self.state.get("candidates", [])],
            idea_mentions=dict(floor.get("mentions", {})),
            last_key=str(floor.get("last") or ""),
            low_gain_streak=int(floor.get("low_gain") or 0),
            turn=int(floor.get("turns") or 0),
            budget_left=self._remaining_calls(),
            budget_reserve=reserve,
            max_turns=self._max_team_turns(),
        )

    def _floor_stop_reason(self, ctx: FloorContext) -> str:
        if ctx.budget_left <= ctx.budget_reserve:
            return "调用预算到达后续阶段保留线"
        if ctx.max_turns and ctx.turn >= ctx.max_turns:
            return "达到回合上限"
        if ctx.low_gain_streak >= 3:
            return "连续三次低信息增量"
        return "没有待执行的高价值动作"

    def _close_discussion(
        self, floor: dict[str, Any], required: dict[str, int], reason: str
    ) -> None:
        floor["closed"] = True
        floor["close_reason"] = reason
        counts = floor.get("counts", {})
        covered = min((counts.get(key, 0) for key in required), default=0)
        target = min(required.values()) if required else 0
        self._record_message(
            "pi",
            f"团队讨论收束（{reason}）：每人覆盖 {covered}/{target} 轮，共 {floor.get('turns', 0)} 次发言。",
            {"covered": covered, "required": target, "turns": floor.get("turns", 0), "reason": reason},
            "system",
        )
        self._save_state()

    async def _execute_retrieval(self, action: Action) -> None:
        issue = next(
            (item for item in self.state.get("objections", []) if item.get("id") == action.issue_id),
            None,
        )
        queries = list(action.queries)
        if issue:
            fallback = str(issue.get("evidence_query") or issue.get("issue") or "").strip()
            if not queries and fallback:
                queries = [fallback[:120]]
        if not queries:
            return
        records = await self._run_literature_queries(
            queries,
            (issue or {}).get("target", "") or "研究主张核验",
            f"定向核验 {action.issue_id or ''}".strip(),
            idea_id=action.idea_id,
        )
        if issue:
            issue["retrieval_done"] = True
            ids = [item["id"] for item in records]
            current = list(issue.get("evidence_ids") or [])
            for evidence_id in ids:
                if evidence_id not in current:
                    current.append(evidence_id)
            issue["evidence_ids"] = current
            issue["next_action"] = (
                "证据已补充，等待回应" if records else "未检索到可核验材料，保留为未决"
            )
            message_id = self._record_message(
                "pi",
                f"定向检索完成（{issue['id']}）：获得 {len(records)} 条证据。",
                {"issue_id": issue["id"], "evidence_ids": ids},
                "system",
            )
            dispatch = self._active_dispatch()
            if dispatch and not dispatch.get("message_id"):
                dispatch["message_id"] = message_id
        self._save_state()

    def _add_result_issues(
        self,
        result: dict[str, Any],
        speaker: Participant,
        turn: int,
        stage: str,
        idea_id: str,
        label_map: dict[str, str],
        default_assignee: str = "",
    ) -> list[dict[str, Any]]:
        added: list[dict[str, Any]] = []
        candidate_ids = {str(item["id"]) for item in self.state.get("candidates", [])}
        for payload in result.get("objections") or []:
            if not isinstance(payload, dict) or not payload.get("issue"):
                continue
            assignee = label_map.get(str(payload.get("respond_to") or "").strip(), "")
            target = str(payload.get("target") or "").strip()
            scope = target if stage.startswith("P2.1") and target in candidate_ids else idea_id
            issue = add_issue(
                self.state,
                payload,
                speaker_key=speaker.key,
                speaker_label=speaker.label,
                turn=turn,
                stage=stage,
                idea_id=scope,
                default_assignee=assignee or default_assignee,
            )
            if issue:
                added.append(issue)
        return added

    def _apply_result_responses(
        self, result: dict[str, Any], speaker: Participant, turn: int,
        idea: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        touched = 0
        resolved = 0
        evidence_ids = [
            str(item) for item in (result.get("evidence_ids") or []) if str(item).strip()
        ]
        allowed = {item["id"] for item in self._debate_issues(idea)} if idea is not None else None
        for payload in result.get("responses") or []:
            if not isinstance(payload, dict):
                continue
            issue_id = str(payload.get("objection_id") or "").strip()
            if allowed is not None and issue_id not in allowed:
                continue
            issue = apply_issue_response(
                self.state,
                issue_id,
                str(payload.get("decision") or ""),
                str(payload.get("why") or ""),
                speaker_key=speaker.key,
                evidence_ids=evidence_ids,
                revised=bool(result.get("substantive_revision") and payload.get("why")),
                turn=turn,
            )
            if issue:
                touched += 1
                resolved += 1 if issue.get("status") == "resolved" else 0
        return touched, resolved

    def _note_mentions(
        self,
        floor: dict[str, Any],
        result: dict[str, Any],
        added: list[dict[str, Any]],
        idea_id: str = "",
    ) -> None:
        mentions = floor.setdefault("mentions", {})
        ids = {str(item.get("id")) for item in self.state.get("candidates", [])}
        if idea_id and idea_id in ids:
            mentions[idea_id] = int(mentions.get(idea_id, 0)) + 1
        for raw in result.get("referenced_ideas") or []:
            key = str(raw)
            if key in ids:
                mentions[key] = int(mentions.get(key, 0)) + 1
        for issue in added:
            target = str(issue.get("target") or "")
            if target in ids:
                mentions[target] = int(mentions.get(target, 0)) + 1

    def _absorb_turn(
        self,
        floor: dict[str, Any],
        result: dict[str, Any],
        speaker: Participant,
        action: Action,
        turn: int,
        phase: str,
        idea_id: str,
        added: list[dict[str, Any]],
        resolved: int,
        is_pass: bool,
    ) -> str:
        substantive_issue = any(issue.get("severity") != "minor" for issue in added)
        built = bool(result.get("accepted_routes") or result.get("accepted_alternative") or result.get("substantive_revision"))
        gain = normalize_gain(
            result.get("information_gain"),
            revised=built,
            new_issue=substantive_issue,
            resolved=resolved,
            is_pass=is_pass,
        )
        if is_pass and not (substantive_issue or resolved or built):
            gain = "none"
        if gain in ("high", "medium") and not (substantive_issue or resolved or built):
            gain = "low"
        record_turn(
            self.state,
            phase=phase,
            speaker_key=speaker.key,
            speaker_label=speaker.label,
            model_id=self._last_response_model or speaker.profile or "",
            action=action.type,
            info_gain=gain,
            speech=str(result.get("speech") or ""),
            issue_ids=[issue["id"] for issue in added],
            idea_id=idea_id,
            reason=action.reason,
        )
        if gain in ("high", "medium"):
            floor["low_gain"] = 0
        else:
            floor["low_gain"] = int(floor.get("low_gain") or 0) + 1
        return gain

    def _capture_requests(
        self, result: dict[str, Any], label_map: dict[str, str], speaker_key: str
    ) -> list[dict[str, str]]:
        raw = result.get("request_response_from")
        targets: list[tuple[str, str, str]] = []
        reason = str(result.get("request_reason") or result.get("request") or "上一轮点名要求回应")
        for proposed in result.get("proposed_actions") or []:
            if not isinstance(proposed, dict):
                continue
            action_type = str(proposed.get("type") or "").upper()
            if action_type not in ("RESPOND", "CHALLENGE", "EXPLORE", "COMPARE", "REVISE"):
                continue
            target = proposed.get("to") or proposed.get("target") or ""
            if target:
                targets.append((str(target), action_type, str(proposed.get("instruction") or proposed.get("issue") or reason)))
        valid = set(label_map.values())
        requests: list[dict[str, str]] = []
        for target, action_type, instruction in targets:
            raw_target = target.strip()
            key = label_map.get(raw_target, raw_target)
            if key == speaker_key or key not in valid:
                continue
            requests.append({"to": key, "action_type": action_type, "issue": instruction[:300], "source_key": speaker_key})
        explicit_targets = {request["to"] for request in requests}
        direct_targets = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
        for target in direct_targets:
            label = str(target).strip()
            key = label_map.get(label, label)
            if key != speaker_key and key in valid and key not in explicit_targets:
                requests.append({"to": key, "action_type": "RESPOND", "issue": reason[:300], "source_key": speaker_key})
                explicit_targets.add(key)
        return requests

    def _advance_floor_requests(
        self, floor: dict[str, Any], action: Action, result: dict[str, Any],
        label_map: dict[str, str], speaker_key: str,
    ) -> None:
        consumed = action.meta.get("request_index")
        pending = [request for index, request in enumerate(floor.get("requests", [])) if index != consumed]
        known = {(request.get("to"), request.get("action_type", "RESPOND"), request.get("issue")) for request in pending}
        for request in self._capture_requests(result, label_map, speaker_key):
            key = (request["to"], request["action_type"], request["issue"])
            if key not in known:
                pending.append(request)
                known.add(key)
        floor["requests"] = pending[:24]

    def _debate_plan_text(self, target: dict[str, Any]) -> str:
        keys = (
            "title",
            "one_sentence",
            "summary",
            "gap",
            "root_cause",
            "mechanism",
            "discriminating_experiment",
            "novelty",
            "evidence",
        )
        payload = {key: target.get(key) for key in keys if target.get(key)}
        if not payload:
            payload = {key: value for key, value in target.items() if isinstance(value, str)}
        return json.dumps(payload, ensure_ascii=False)[:8000]

    def _debate_participants(self) -> list[Participant]:
        roster = self._model_discussion_participants()
        if roster is not None:
            return roster
        participants: list[Participant] = []
        seen: set[str] = set()
        for role in ("method_architect", "experiment_designer"):
            for profile_id in self._profile_ids_for(role):
                if profile_id in seen:
                    continue
                seen.add(profile_id)
                participants.append(
                    Participant(
                        key=f"{role}|{profile_id}",
                        role=role,
                        label=self._profile_label(profile_id),
                        profile=profile_id,
                    )
                )
        return participants

    def _debate_issues(self, idea: dict[str, Any]) -> list[dict[str, Any]]:
        keys = {idea_key(idea), str(idea.get("candidate_id") or "")} - {""}
        candidates = {str(item["id"]) for item in self.state.get("candidates", [])}
        return [
            item
            for item in open_issues(self.state)
            if item.get("idea_id") in keys
            or not item.get("idea_id") and (item.get("target") not in candidates or item.get("target") in keys)
        ]

    def _idea_evidence(self, idea: dict[str, Any]) -> list[dict[str, Any]]:
        keys = {idea_key(idea), str(idea.get("candidate_id") or "")}
        keys.update(idea.get("parent_ids") or [])
        records = self.state.get("evidence", [])
        own = [item for item in records if keys.intersection(item.get("idea_ids") or [])]
        shared = [item for item in records if not item.get("idea_ids")]
        return shared[-4:] + own[-16:]

    def _reopen_debate(self, idea: dict[str, Any], focus: str) -> None:
        key = idea_key(idea)
        floors = self.state.setdefault("debate_floors", {})
        floor = floors.get(key)
        participants = self._debate_participants()
        if not floor:
            floor = {
                "counts": {},
                "turns": 0,
                "last": "",
                "closed": False,
                "low_gain": 0,
                "requests": [],
                "log": [],
                "actions": [],
                "plan": self._debate_plan_text(idea),
                "owner": participants[0].key if participants else "",
                "focus": focus,
                "close_reason": "",
            }
            floors[key] = floor
        else:
            floor["closed"] = False
            floor["finalized"] = False
            floor["low_gain"] = 0
            floor["focus"] = focus
            floor["close_reason"] = ""
        if focus and not any(
            item.get("idea_id") == key
            and item.get("status") in ("open", "contested")
            and item.get("issue") == focus
            for item in self.state.get("objections", [])
        ):
            add_issue(
                self.state,
                {
                    "issue": focus,
                    "type": "一致性",
                    "severity": "blocking",
                    "fix_direction": "修订核心机制或实验设置，使两者指向同一版本",
                },
                speaker_key="coherence_editor",
                speaker_label=ROLE_LABELS["coherence_editor"],
                turn=int(floor.get("turns") or 0),
                stage="P3 门禁",
                idea_id=key,
                default_assignee=participants[0].key if participants else "",
            )

    def _add_pending_issues(
        self, idea: dict[str, Any], issues: list[dict[str, Any]], stage: str
    ) -> None:
        key = idea_key(idea)
        decision = idea.get("decision") or idea.get("consensus") or {}
        idea["decision"] = idea["consensus"] = decision
        unresolved = decision.setdefault("unresolved", [])
        for item in issues[:4]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("problem") or item.get("issue") or "").strip()
            if not text:
                continue
            unresolved.append(
                {
                    "issue": text[:600],
                    "how_to_handle": str(item.get("fix") or "在 P3 修订时处理")[:400],
                }
            )
            issue = add_issue(
                self.state,
                {
                    "issue": text,
                    "type": "一致性",
                    "severity": "major",
                    "fix_direction": str(item.get("fix") or "")[:400],
                },
                speaker_key="coherence_editor",
                speaker_label=ROLE_LABELS["coherence_editor"],
                turn=0,
                stage=stage,
                idea_id=key,
            )
            if issue:
                issue["status"] = "unresolved"
                issue["next_action"] = "记录在决策版本的 unresolved 中"
                ids = self.state.setdefault("idea_states", {}).setdefault(key, {}).setdefault(
                    "open_issue_ids", []
                )
                if issue["id"] in ids:
                    ids.remove(issue["id"])
        self._save_state()

    async def _run_debate_floor(self, target: dict[str, Any], focus: str = "") -> None:
        """P2.3：每个 Idea 独立辩论，Action 驱动，不预设攻防顺序。"""
        participants = self._debate_participants()
        if len(participants) < 2:
            self._record_message(
                "pi",
                f"《{target.get('title', '')}》只绑定了一个模型，无法形成多模型辩论；直接进入研究决策。",
                {"skipped": True, "participants": len(participants)},
                "debate",
            )
            target["debate_turns"] = 0
            self._save_state()
            return
        key = idea_key(target)
        self.state["current_idea_id"] = key
        floors = self.state.setdefault("debate_floors", {})
        floor = floors.get(key)
        if not floor:
            floor = {
                "counts": {},
                "turns": 0,
                "last": "",
                "closed": False,
                "low_gain": 0,
                "requests": [],
                "log": [],
                "actions": [],
                "plan": self._debate_plan_text(target),
                "owner": participants[0].key,
                "focus": focus,
                "close_reason": "",
            }
            floors[key] = floor
        if focus:
            floor["focus"] = focus
        if floor.get("finalized"):
            return
        if not focus and "later_debate_buffer" not in floor:
            reserve = self._debate_reserve_calls(target)
            extra = max(0, self._remaining_calls() - reserve - len(participants))
            later = sum(
                idea_key(item) != key
                and not (self.state.get("idea_states", {}).get(idea_key(item)) or {}).get("debate_done")
                for item in self.state.get("selected", [])
            )
            floor["later_debate_buffer"] = min(2 * later, extra // 2)
        label_map = {item.label: item.key for item in participants}
        for item in participants:
            label_map.setdefault(item.role, item.key)
        while not floor.get("closed"):
            self._guard()
            ctx = FloorContext(
                phase="debate",
                participants=participants,
                counts=dict(floor.get("counts", {})),
                required={item.key: 1 for item in participants},
                issues=self._debate_issues(target),
                requests=floor.get("requests", []),
                last_key=str(floor.get("last") or ""),
                low_gain_streak=int(floor.get("low_gain") or 0),
                turn=int(floor.get("turns") or 0),
                budget_left=self._remaining_calls(),
                budget_reserve=(
                    self._p3_required_calls() if focus
                    else self._debate_reserve_calls(target) + int(floor.get("later_debate_buffer", 0))
                ),
                max_turns=self._max_debate_turns(),
                active_idea_id=key,
                challenged=bool(floor.get("challenged")),
                candidate_id=str(target.get("candidate_id") or ""),
                development_done=bool(floor.get("development_done") or focus),
                owner_key=str(floor.get("owner") or ""),
            )
            action = plan_next_action(ctx)
            if not action:
                floor["closed"] = True
                floor["close_reason"] = self._floor_stop_reason(ctx)
                self._save_state()
                break
            if action.type == "RETRIEVE":
                self._begin_dispatch(
                    stage="P2.3", purpose=action.instruction, role="literature_researcher",
                    status="retrieving", action=action, participants=participants, idea_id=key,
                )
                await self._execute_retrieval(action)
                floor.setdefault("actions", []).append(describe_action(action))
                floor["actions"] = floor["actions"][-80:]
                self._finish_dispatch()
                self._save_state()
                continue
            speaker = action.target
            if not speaker:
                floor["closed"] = True
                floor["close_reason"] = "没有可执行的发言动作"
                self._save_state()
                break
            turn = int(floor.get("turns", 0)) + 1
            spoken = int(floor.get("counts", {}).get(speaker.key, 0))
            purpose = f"P2.3 辩论 {turn} / {speaker.label} · {action.type}"
            self._begin_dispatch(
                stage="P2.3", purpose=purpose, role=speaker.role, action=action,
                participants=participants, turn=turn, idea_id=key,
            )
            await self._prepare_context(purpose)
            self._last_response_model = ""
            result = await self._call_json(
                role=speaker.role,
                purpose=purpose,
                contract="debate",
                messages=debate_floor_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    {k: v for k, v in target.items() if k not in ("consensus", "decision")},
                    str(floor.get("plan") or self._debate_plan_text(target)),
                    {
                        "key": speaker.key,
                        "role": speaker.role,
                        "label": speaker.label,
                        "profile": speaker.profile,
                    },
                    floor.get("log", [])[-8:],
                    self._debate_issues(target),
                    turn,
                    spoken,
                    [item.label for item in participants],
                    str(floor.get("focus") or ""),
                    describe_action(action),
                    self._idea_evidence(target),
                ),
                temperature=0.55,
                max_tokens=18000,
                profile_id=speaker.profile,
            )
            stance = str(result.get("stance") or action.type.lower())
            is_pass = stance == "pass"
            floor["challenged"] = bool(
                floor.get("challenged")
                or action.type == "CHALLENGE"
                or stance in ("challenge", "attack")
            )
            message_id = self._record_message(
                speaker.role,
                f"【{speaker.label} · {action.type}】"
                f"{result.get('speech') or ('本轮没有新增判断。' if is_pass else '已发言。')}",
                {
                    "turn": turn,
                    "stance": stance,
                    "action": action.type,
                    "profile": speaker.profile,
                    "actual_model": self._last_response_model,
                    "scheduler": describe_action(action),
                    "result": result,
                },
                "debate",
            )
            if isinstance(result.get("revised_plan"), str) and result["revised_plan"].strip():
                plan = result["revised_plan"].strip()[:12000]
                result["substantive_revision"] = bool(
                    result.get("changes") and " ".join(plan.split()) != " ".join(floor["plan"].split())
                )
                floor["plan"] = plan
                floor["owner"] = speaker.key
            alternative = result.get("alternative_plan")
            alternatives = target.setdefault("debate_alternatives", [])
            if isinstance(alternative, dict) and len(alternatives) < 3:
                fields = {
                    key: alternative[key].strip()[:1600]
                    for key in ("mechanism", "hypothesis_delta", "discriminating_test")
                    if isinstance(alternative.get(key), str) and alternative[key].strip()
                }
                if len(fields) == 3 and all(item["mechanism"] != fields["mechanism"] for item in alternatives):
                    alternatives.append({**fields, "turn": turn, "speaker": speaker.label})
                    result["accepted_alternative"] = True
            default_assignee = (
                str(floor.get("owner") or "") if floor.get("owner") != speaker.key else ""
            )
            added = self._add_result_issues(
                result,
                speaker,
                turn,
                "P2.3 自由辩论",
                key,
                label_map,
                default_assignee,
            )
            touched, resolved = self._apply_result_responses(result, speaker, turn, target)
            outcomes = []
            if result.get("substantive_revision"):
                outcomes.append("修订当前方案")
            if result.get("accepted_alternative"):
                outcomes.append("登记替代方案")
            if resolved:
                outcomes.append(f"解决 {resolved} 条异议")
            if is_pass:
                outcomes.append("跳过发言")
            self._finish_dispatch(message_id=message_id, outcome=" · ".join(outcomes))
            gain = self._absorb_turn(
                floor,
                result,
                speaker,
                action,
                turn,
                "P2.3 自由辩论",
                key,
                added,
                resolved,
                is_pass,
            )
            floor.setdefault("counts", {})[speaker.key] = spoken + 1
            floor["turns"] = turn
            floor["last"] = speaker.key
            if action.type == "DEVELOP":
                floor["development_done"] = True
            self._advance_floor_requests(floor, action, result, label_map, speaker.key)
            floor.setdefault("log", []).append(
                {
                    "turn": turn,
                    "speaker": speaker.label,
                    "role": ROLE_LABELS.get(speaker.role, speaker.role),
                    "stance": stance,
                    "action": action.type,
                    "instruction": action.instruction,
                    "speech": str(result.get("speech") or "")[:1500],
                    "attacks": result.get("attacks") or [],
                    "objections": result.get("objections") or [],
                    "responses": result.get("responses") or [],
                    "revised": bool(result.get("revised_plan")),
                    "information_gain": gain,
                }
            )
            floor.setdefault("actions", []).append(
                {**describe_action(action), "information_gain": gain}
            )
            floor["actions"] = floor["actions"][-80:]
            self._save_state()
        target["debate_plan"] = floor.get("plan", "")
        target["debate_turns"] = floor.get("turns", 0)
        target["debate_log"] = floor.get("log", [])[-12:]
        target["debate_close_reason"] = floor.get("close_reason", "")
        floor["finalized"] = True
        self._record_message(
            "pi",
            f"《{target.get('title', '')}》辩论收束（{floor.get('close_reason') or '无待执行动作'}）："
            f"共 {floor.get('turns', 0)} 次发言。",
            {
                "idea_id": key,
                "turns": floor.get("turns", 0),
                "reason": floor.get("close_reason", ""),
            },
            "debate",
        )
        self._save_state()

    async def _decide_idea(self, idea: dict[str, Any]) -> None:
        """P2.4：把讨论收敛为该 Idea 的研究决策版本，不要求主观一致。"""
        key = idea_key(idea)
        floor = (self.state.get("debate_floors") or {}).get(key, {})
        purpose = f"P2.4 研究决策 / {str(idea.get('title') or '')[:30]}"
        await self._prepare_context(purpose)
        result = await self._call_json(
            role="pi",
            purpose=purpose,
            contract="decision",
            messages=decision_messages(
                self.project,
                self.run,
                self.skills_bundle,
                idea,
                self._debate_issues(idea),
                self._idea_evidence(idea),
                floor.get("log", []),
            ),
            temperature=0.3,
            max_tokens=18000,
            profile_id=(self._profile_ids_for("pi") or [None])[0],
        )
        decision = dict(result)
        decision.setdefault("thesis", idea.get("one_sentence", ""))
        decision.setdefault("core_mechanism", floor.get("plan", ""))
        unresolved = result.get("unresolved")
        unresolved = unresolved if isinstance(unresolved, list) else []
        known = {str(item.get("issue") or "") for item in unresolved if isinstance(item, dict)}
        decision["unresolved"] = unresolved + [
            item for item in unresolved_for(self.state, key) if item.get("issue") not in known
        ]
        decision["version"] = int(
            (self.state.get("idea_states", {}).get(key) or {}).get("version") or 0
        ) + 1
        idea["consensus"] = decision
        idea["decision"] = decision
        record_decision(self.state, idea, decision, unresolved=decision["unresolved"])
        self._record_message(
            "pi",
            result.get("speech") or "已形成研究决策。",
            decision,
            "decision",
        )
        self._record_decision(
            f"研究决策 · {str(idea.get('title') or '')[:40]}",
            json.dumps(
                {
                    "thesis": decision.get("thesis"),
                    "core_mechanism": decision.get("core_mechanism"),
                    "contributions": decision.get("contributions"),
                    "key_experiments": decision.get("key_experiments"),
                },
                ensure_ascii=False,
            ),
            json.dumps(decision.get("unresolved", []), ensure_ascii=False),
        )
        self._save_state()

    def _ensure_idea_record(self, selected: dict[str, Any], index: int) -> dict[str, Any]:
        if selected.get("idea_id"):
            return {
                "id": selected["idea_id"],
                "slug": selected["idea_slug"],
                "index": index,
            }
        record = self.artifacts.create_idea(
            project_id=self.project["id"],
            run_id=self.run_id,
            title=selected.get("title", f"Idea {index + 1}"),
            summary=selected.get("summary", selected.get("one_sentence", "")),
            index=index,
        )
        old_key = str(selected.get("candidate_id") or "")
        selected["idea_id"] = record["id"]
        selected["idea_slug"] = record["slug"]
        if old_key and old_key != record["id"]:
            states = self.state.setdefault("idea_states", {})
            if old_key in states and record["id"] not in states:
                states[record["id"]] = states.pop(old_key)
            floors = self.state.setdefault("debate_floors", {})
            if old_key in floors and record["id"] not in floors:
                floors[record["id"]] = floors.pop(old_key)
            for issue in self.state.get("objections", []):
                if issue.get("idea_id") == old_key:
                    issue["idea_id"] = record["id"]
            literature = self.state.setdefault("literature_by_idea", {})
            candidate_ids = [*selected.get("parent_ids", []), old_key]
            scoped = [entry for key in candidate_ids for entry in literature.get(key, [])]
            if scoped:
                literature[record["id"]] = list(dict.fromkeys(scoped))[-12:]
        self.state["selected"] = self.state.get("selected", [])
        self._save_state()
        return record

    async def _phase_p3(self) -> None:
        selected_items = self.state.get("selected", [])
        while int(self.state.get("idea_next", 0)) < min(len(selected_items), int(self.run["idea_count"])):
            self._guard()
            index = int(self.state.get("idea_next", 0))
            selected = selected_items[index]
            idea_record = self._ensure_idea_record(selected, index)
            self.state["current_idea_id"] = idea_record["id"]
            stage = selected.get("stage", "method")
            if stage == "draft":
                selected["stage"] = "draft_a"
                self._save_state()
                stage = "draft_a"
            if stage == "revise":
                selected["stage"] = "revise_a"
                self._save_state()
                stage = "revise_a"
            self._activate_skills(skill_kind_for("P3", stage))
            await self._prepare_context(
                f"P3 idea{index + 1} {str(selected.get('title') or '')[:30]}"
            )
            label = f"深化 {index + 1}/{len(selected_items)} · {selected.get('title', '')[:30]}"
            self.db.execute(
                "UPDATE runs SET phase_label = ?, updated_at = ? WHERE id = ?",
                (label, utc_now(), self.run_id),
            )
            if stage == "redebate":
                await self._run_debate_floor(selected, selected["rollback_focus"])
                selected["stage"] = "redecide"
                self._save_state()
                continue
            if stage == "redecide":
                previous_version = int(selected.get("rollback_from_version") or 0)
                if int((selected.get("decision") or {}).get("version") or 0) <= previous_version:
                    await self._decide_idea(selected)
                selected["method"] = {}
                selected["experiment"] = {}
                selected["stage"] = "method"
                selected.pop("rollback_focus", None)
                self._save_state()
                continue
            if stage == "method":
                method_profiles = self._profile_ids_for("method_architect")
                method_prompt = method_messages(
                    self.project, self.run, self.skills_bundle, selected, self.state
                )
                gate_revision = bool(selected.get("gate_notes"))
                if gate_revision:
                    # 门禁回修是定向修订：单模型处理 gate 指出的问题，不重跑整个面板。
                    purpose = f"P3 idea{index + 1} 方法修订"
                    result = await self._call_json(
                        role="method_architect",
                        purpose=purpose,
                        contract="method",
                        messages=method_prompt,
                        temperature=0.4,
                        max_tokens=18000,
                        profile_id=method_profiles[0] if method_profiles else None,
                    )
                elif len(method_profiles) > 1 and self._remaining_calls() >= self._p3_required_calls() + len(method_profiles):
                    purpose = f"P3 idea{index + 1} 方法机制"
                    result = await self._panel_json(
                        role="method_architect",
                        purpose=purpose,
                        kind="method",
                        idea=selected,
                        messages=method_prompt,
                        profiles=method_profiles,
                        temperature=0.55,
                        max_tokens=18000,
                    )
                else:
                    purpose = f"P3 idea{index + 1} 方法机制"
                    result = await self._call_json(
                        role="method_architect",
                        purpose=purpose,
                        contract="method",
                        messages=method_prompt,
                        temperature=0.55,
                        max_tokens=18000,
                        profile_id=method_profiles[0] if method_profiles else None,
                    )
                selected["method"] = result
                selected["method_panel"] = result.pop("panel_proposals", [])
                (self.state.get("panel_progress") or {}).pop(f"P3 idea{index + 1} 方法机制", None)
                self._record_message("method_architect", result.get("speech") or "已完成方法蓝图。", result)
                await self._run_literature_queries(
                    result.get("literature_queries"),
                    selected.get("title", ""),
                    f"P3 {selected.get('title', '')[:36]} 方法阶段检索",
                    idea_id=idea_key(selected),
                )
                selected["stage"] = "experiment"
                self._save_state()
                continue
            if stage == "experiment":
                experiment_profiles = self._profile_ids_for("experiment_designer")
                experiment_prompt = experiment_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    selected,
                    selected.get("method", {}),
                    self.state,
                )
                gate_revision = bool(selected.get("gate_notes"))
                if (
                    not gate_revision
                    and len(experiment_profiles) > 1
                    and self._remaining_calls() >= self._p3_required_calls() + len(experiment_profiles)
                ):
                    result = await self._panel_json(
                        role="experiment_designer",
                        purpose=f"P3 idea{index + 1} 实验设计",
                        kind="experiment",
                        idea=selected,
                        messages=experiment_prompt,
                        profiles=experiment_profiles,
                        temperature=0.45,
                        max_tokens=18000,
                    )
                else:
                    result = await self._call_json(
                        role="experiment_designer",
                        purpose=f"P3 idea{index + 1} 实验设计" if not gate_revision else f"P3 idea{index + 1} 实验修订",
                        contract="experiment",
                        messages=experiment_prompt,
                        temperature=0.45,
                        max_tokens=18000,
                        profile_id=experiment_profiles[0] if experiment_profiles else None,
                    )
                selected["experiment"] = result
                selected["experiment_panel"] = result.pop("panel_proposals", [])
                (self.state.get("panel_progress") or {}).pop(f"P3 idea{index + 1} 实验设计", None)
                self._record_message("experiment_designer", result.get("speech") or "已完成实验设计。", result)
                await self._run_literature_queries(
                    result.get("literature_queries"),
                    selected.get("title", ""),
                    f"P3 {selected.get('title', '')[:36]} 实验阶段检索",
                    idea_id=idea_key(selected),
                )
                selected["stage"] = "gate"
                selected.pop("gate_notes", None)
                self._save_state()
                continue
            if stage == "gate":
                result = await self._call_gate(
                    role="coherence_editor",
                    purpose=f"P3 idea{index + 1} 方法-实验门禁",
                    messages=gate_messages(
                        self.project,
                        self.run,
                        self.skills_bundle,
                        selected,
                        selected.get("method", {}),
                        selected.get("experiment", {}),
                    ),
                    temperature=0.2,
                    max_tokens=18000,
                )
                verdict = str(result.get("verdict") or "").lower()
                if verdict not in {"pass", "method_revise", "thesis_conflict"}:
                    verdict = "unconfirmed"
                selected["validation_card"] = validation_card(selected.get("experiment", {}), verdict)
                issues = result.get("issues") if isinstance(result.get("issues"), list) else []
                if verdict == "unconfirmed" and not issues:
                    issues = [{"problem": "方法与实验的一致性判定缺失", "fix": "在启动实验前重新核对中心假设与对照。"}]
                self._record_message(
                    "coherence_editor",
                    result.get("speech") or f"方法-实验门禁：{verdict}",
                    {"verdict": verdict, "issues": issues},
                )
                retries = int(selected.get("gate_retries") or 0)
                if verdict == "method_revise" and retries < 1 and self._remaining_calls() >= self._p3_required_calls() + 3:
                    selected["gate_retries"] = retries + 1
                    selected["gate_notes"] = issues[:4]
                    selected["stage"] = "method"
                    self._record_message(
                        "pi",
                        "门禁发现方法-实验不一致，回到方法阶段做一次局部修订。",
                        {"issues": issues[:4]},
                        "system",
                    )
                    self._save_state()
                    continue
                if verdict == "thesis_conflict" and int(selected.get("rollback_count") or 0) < 1:
                    if self._remaining_calls() >= self._p3_required_calls() + 4:
                        selected["rollback_count"] = int(selected.get("rollback_count") or 0) + 1
                        focus = "；".join(
                            str(item.get("problem") or item.get("issue") or "")
                            for item in issues[:3]
                            if isinstance(item, dict)
                        ) or "门禁判定中心主张与实验不一致"
                        self._reopen_debate(selected, focus)
                        selected["rollback_focus"] = focus
                        selected["rollback_from_version"] = int((selected.get("decision") or {}).get("version") or 0)
                        selected["stage"] = "redebate"
                        self._record_message(
                            "pi",
                            "门禁判定核心机制需要重新讨论，已回到该 Idea 的辩论。",
                            {"focus": focus},
                            "debate",
                        )
                        self._save_state()
                        continue
                    self._record_message(
                        "pi",
                        "门禁判定核心机制存在冲突。剩余预算不足以重新辩论，已记录为未决。",
                        {"issues": issues[:4]},
                        "system",
                    )
                    self._add_pending_issues(selected, issues, "P3 门禁")
                elif issues:
                    self._add_pending_issues(selected, issues, "P3 门禁")
                if self.config.get("deliverable") == "idea_brief":
                    selected["draft"] = render_idea_brief(selected, self._idea_evidence(selected))
                    selected["stage"] = "save"
                else:
                    selected["stage"] = "draft_a"
                self._save_state()
                continue
            if stage == "draft_a":
                document_a = await self._call_markdown(
                    role="writer",
                    purpose=f"P3 idea{index + 1} 初稿 A：摘要、引言与相关工作",
                    messages=writer_initial_part_messages(
                        self.project,
                        self.run,
                        self.skills_bundle,
                        selected,
                        selected.get("method", {}),
                        selected.get("experiment", {}),
                        "A",
                    ),
                    temperature=0.5,
                    max_tokens=16000,
                    required_markers=["## Abstract", "## 1. Introduction", "## 2. Related Work"],
                )
                selected["draft_a"] = document_a
                selected["stage"] = "draft_b"
                self._save_state()
                continue
            if stage == "draft_b":
                document_b = await self._call_markdown(
                    role="writer",
                    purpose=f"P3 idea{index + 1} 初稿 B：方法",
                    messages=writer_initial_part_messages(
                        self.project,
                        self.run,
                        self.skills_bundle,
                        selected,
                        selected.get("method", {}),
                        selected.get("experiment", {}),
                        "B",
                        existing_document=selected.get("draft_a", ""),
                    ),
                    temperature=0.5,
                    max_tokens=16000,
                    required_markers=["## 3. Method", "### 3.5"],
                )
                selected["draft_b"] = document_b
                selected["stage"] = "draft_c"
                self._save_state()
                continue
            if stage == "draft_c":
                document_c = await self._call_markdown(
                    role="writer",
                    purpose=f"P3 idea{index + 1} 初稿 C：实验、结果与讨论",
                    messages=writer_initial_part_messages(
                        self.project,
                        self.run,
                        self.skills_bundle,
                        selected,
                        selected.get("method", {}),
                        selected.get("experiment", {}),
                        "C",
                        existing_document=(selected.get("draft_a", "") + "\n\n" + selected.get("draft_b", "")),
                    ),
                    temperature=0.45,
                    max_tokens=18000,
                    required_markers=[
                        "## 4. Experiment",
                        "## 5. Expected Results",
                        "## 6. Discussion",
                        "## 7. Reproducibility Plan",
                        "Appendix A",
                    ],
                )
                selected["draft_c"] = document_c
                selected["draft"] = (
                    selected.get("draft_a", "").rstrip()
                    + "\n\n"
                    + selected.get("draft_b", "").strip()
                    + "\n\n"
                    + document_c.strip()
                )
                self._record_message(
                    "writer",
                    f"已完成《{selected.get('title')}》完整初稿，正在做一致性检查。",
                    {"idea_id": idea_record["id"], "length": len(selected["draft"])},
                    "artifact",
                )
                selected["stage"] = "coherence"
                self._save_state()
                continue
            if stage == "coherence":
                result = await self._call_json(
                    role="coherence_editor",
                    purpose=f"P3 idea{index + 1} 一致性检查",
                    contract="coherence",
                    messages=coherence_messages(
                        self.project,
                        self.run,
                        self.skills_bundle,
                        selected,
                        selected.get("draft", ""),
                    ),
                    temperature=0.2,
                    max_tokens=5000,
                )
                issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
                selected["coherence_issues"] = issues[:2]
                self._record_message("coherence_editor", result.get("speech") or "检查完成。", result)
                selected["stage"] = (
                    "revise_a" if issues[:2] and self._remaining_calls() >= self._p3_required_calls() + 2
                    else "save"
                )
                self._save_state()
                continue
            if stage == "revise_a":
                notes = json.dumps(selected.get("coherence_issues", []), ensure_ascii=False, indent=2)
                document_a = await self._call_markdown(
                    role="writer",
                    purpose=f"P3 idea{index + 1} 修订 A：摘要、引言与相关工作",
                    messages=writer_initial_part_messages(
                        self.project,
                        self.run,
                        self.skills_bundle,
                        selected,
                        selected.get("method", {}),
                        selected.get("experiment", {}),
                        "A",
                        existing_document=selected.get("draft", ""),
                        revision_notes=notes,
                    ),
                    temperature=0.4,
                    max_tokens=16000,
                    required_markers=["## Abstract", "## 1. Introduction", "## 2. Related Work"],
                )
                selected["revised_a"] = document_a
                selected["stage"] = "revise_b"
                self._save_state()
                continue
            if stage == "revise_b":
                notes = json.dumps(selected.get("coherence_issues", []), ensure_ascii=False, indent=2)
                document_b = await self._call_markdown(
                    role="writer",
                    purpose=f"P3 idea{index + 1} 修订 B：方法",
                    messages=writer_initial_part_messages(
                        self.project,
                        self.run,
                        self.skills_bundle,
                        selected,
                        selected.get("method", {}),
                        selected.get("experiment", {}),
                        "B",
                        existing_document=selected.get("revised_a", ""),
                        revision_notes=notes,
                    ),
                    temperature=0.4,
                    max_tokens=16000,
                    required_markers=["## 3. Method", "### 3.5"],
                )
                selected["revised_b"] = document_b
                selected["stage"] = "revise_c"
                self._save_state()
                continue
            if stage == "revise_c":
                notes = json.dumps(selected.get("coherence_issues", []), ensure_ascii=False, indent=2)
                document_c = await self._call_markdown(
                    role="writer",
                    purpose=f"P3 idea{index + 1} 修订 C：实验、结果与讨论",
                    messages=writer_initial_part_messages(
                        self.project,
                        self.run,
                        self.skills_bundle,
                        selected,
                        selected.get("method", {}),
                        selected.get("experiment", {}),
                        "C",
                        existing_document=(
                            selected.get("revised_a", "") + "\n\n" + selected.get("revised_b", "")
                        ),
                        revision_notes=notes,
                    ),
                    temperature=0.35,
                    max_tokens=18000,
                    required_markers=[
                        "## 4. Experiment",
                        "## 5. Expected Results",
                        "## 6. Discussion",
                        "## 7. Reproducibility Plan",
                        "Appendix A",
                    ],
                )
                selected["draft"] = (
                    selected.get("revised_a", "").rstrip()
                    + "\n\n"
                    + selected.get("revised_b", "").strip()
                    + "\n\n"
                    + document_c.strip()
                )
                self._record_message(
                    "writer",
                    f"《{selected.get('title')}》已完成一次一致性修订。",
                    {"idea_id": idea_record["id"], "length": len(selected["draft"])},
                    "artifact",
                )
                selected["stage"] = "save"
                self._save_state()
                continue
            if stage == "save":
                brief = self.config.get("deliverable") == "idea_brief"
                missing_sections = [] if brief else document_completion_issues(selected.get("draft", ""))
                version = self.artifacts.save_version(
                    idea={"id": idea_record["id"], "slug": idea_record["slug"]},
                    project_id=self.project["id"],
                    document=selected.get("draft", ""),
                    operation_key=self.run_id,
                    manifest={
                        "title": selected.get("title"),
                        "deliverable": "idea_brief" if brief else "manuscript",
                        "status": "INCOMPLETE" if missing_sections else "COMPLETED",
                        "missing_sections": missing_sections,
                        "candidate_id": selected.get("candidate_id"),
                        "parent_candidate_ids": selected.get("parent_ids", []),
                        "validation_card": selected.get("validation_card", {}),
                        "method": selected.get("method", {}),
                        "experiment": selected.get("experiment", {}),
                        "method_panel": selected.get("method_panel", []),
                        "experiment_panel": selected.get("experiment_panel", []),
                        "consensus": selected.get("consensus", {}),
                        "coherence_issues": selected.get("coherence_issues", []),
                        "skills": self.skill_snapshot,
                        "skill_bundles": self.state.get("skill_bundles_snapshot", {}),
                        "specs": self.state.get("spec_snapshot", {}),
                        "model": self.run.get("model"),
                    },
                )
                self.events.publish(
                    self.run_id,
                    "artifact.updated",
                    {
                        "idea_id": idea_record["id"],
                        "version": version["version"],
                        "path": version["path"],
                        "title": selected.get("title"),
                    },
                )
                selected["stage"] = "done"
                selected["version"] = version["version"]
                self.state["idea_next"] = index + 1
                self._save_state()
                continue
            if stage == "done":
                self.state["idea_next"] = index + 1
                self._save_state()
                continue
            raise RuntimeError(f"未知 Idea 深化步骤：{stage}")
        self._set_phase("P4", "组合成稿")

    async def _phase_p4(self) -> None:
        await self._prepare_context("P4 组合总览")
        if self.state.get("portfolio_path"):
            self._complete_generation()
            return
        self._guard()
        ideas = self.db.query_all(
            "SELECT i.*, v.path AS version_path FROM ideas i "
            "LEFT JOIN idea_versions v ON v.idea_id = i.id AND v.version = i.current_version "
            "WHERE i.run_id = ? ORDER BY i.created_at ASC",
            (self.run_id,),
        )
        idea_payload = [
            {
                "title": item["title"],
                "status": item["status"],
                "document": self.artifacts.read_relative_text(item["version_path"]),
            }
            for item in ideas
        ]
        content = await self._call_markdown(
            role="pi",
            purpose="P4 项目组合总览",
            messages=portfolio_messages(self.project, self.run, self.skills_bundle, idea_payload),
            temperature=0.35,
            max_tokens=8000,
        )
        path = self.artifacts.save_portfolio(self.project["id"], self.run_id, content)
        self.state["portfolio_path"] = path
        self._record_message(
            "pi",
            "本次项目的 Idea 组合总览已生成，可在右侧文档区查看。",
            {"path": path},
            "artifact",
        )
        self.events.publish(self.run_id, "artifact.updated", {"portfolio_path": path})
        self._save_state()
        self._complete_generation()

    def _complete_generation(self) -> None:
        self.state["phase"] = "DONE"
        self._save_state()
        now = utc_now()
        self.db.execute(
            "UPDATE runs SET status = 'COMPLETED', phase = 'DONE', phase_label = '已完成', "
            "ended_at = ?, updated_at = ?, error = NULL "
            "WHERE id = ?",
            (now, now, self.run_id),
        )
        self.db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, self.project["id"]))
        self.events.publish(
            self.run_id,
            "run.completed",
            {
                "status": "COMPLETED",
                "portfolio_path": self.state.get("portfolio_path"),
            },
        )

    async def _drive_revision_phase(self, phase: str) -> None:
        if not self.state.get("revision_base_document"):
            self._load_revision_base()
        await self._prepare_context(f"{phase} 修订讨论")
        if phase == "R0":
            result = await self._call_json(
                role="pi",
                purpose="R0 用户反馈分析",
                contract="revision",
                messages=revision_plan_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    self.state["revision_base_document"],
                    self.state["revision_feedback"],
                    self.state.get("revision_instructions", ""),
                ),
                temperature=0.35,
                max_tokens=8000,
            )
            self.state["revision_plan"] = result
            self._record_message("pi", result.get("speech") or "已完成用户反馈分析。", result)
            self._record_decision(
                "方案修订路线",
                result.get("revision_type", "LOCAL_REVISE"),
                result.get("revision_plan", ""),
            )
            self._set_phase("R1", "方法修订")
            return
        if phase == "R1":
            revision_context = json.dumps(
                {
                    "feedback": self.state["revision_feedback"],
                    "instructions": self.state.get("revision_instructions", ""),
                    "plan": self.state.get("revision_plan", {}),
                },
                ensure_ascii=False,
            )
            result = await self._call_json(
                role="method_architect",
                purpose="R1 根据反馈修订方法",
                contract="method",
                messages=method_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    self.state["revision_idea"],
                    self.state,
                    revision_context,
                ),
                temperature=0.45,
                max_tokens=16000,
            )
            self.state["revision_method"] = result
            self._record_message("method_architect", result.get("speech") or "方法已按证据修订。", result)
            await self._run_literature_queries(
                result.get("literature_queries"),
                self.state["revision_idea"].get("title", ""),
                "R1 方法修订阶段检索",
            )
            self._set_phase("R2", "实验修订")
            return
        if phase == "R2":
            revision_context = json.dumps(
                {
                    "feedback": self.state["revision_feedback"],
                    "plan": self.state.get("revision_plan", {}),
                },
                ensure_ascii=False,
            )
            result = await self._call_json(
                role="experiment_designer",
                purpose="R2 根据反馈修订实验",
                contract="experiment",
                messages=experiment_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    self.state["revision_idea"],
                    self.state.get("revision_method", {}),
                    self.state,
                    revision_context,
                ),
                temperature=0.4,
                max_tokens=16000,
            )
            self.state["revision_experiment"] = result
            self._record_message("experiment_designer", result.get("speech") or "实验方案已修订。", result)
            await self._run_literature_queries(
                result.get("literature_queries"),
                self.state["revision_idea"].get("title", ""),
                "R2 实验修订阶段检索",
            )
            self._set_phase("R3", "修订成稿")
            return
        if phase == "R3":
            if self.state.get("revision_deliverable") == "idea_brief":
                idea = self.state["revision_idea"]
                method = self.state.get("revision_method", {})
                experiment = self.state.get("revision_experiment", {})
                gate = await self._call_gate(
                    role="coherence_editor",
                    purpose="R3 首轮验证门禁",
                    messages=gate_messages(self.project, self.run, self.skills_bundle, idea, method, experiment),
                    temperature=0.2,
                    max_tokens=5000,
                )
                issues = gate.get("issues")
                issues = issues if isinstance(issues, list) else []
                plan = self.state.get("revision_plan", {})
                prior = idea.get("consensus") or {}
                idea["consensus"] = {
                    **prior,
                    "thesis": plan.get("revised_thesis") or prior.get("thesis") or idea["summary"],
                    "decision_rationale": plan.get("revision_plan") or "",
                    "unresolved": [*(prior.get("unresolved") or []), *issues],
                }
                self.state["revision_issues"] = issues
                self.state["revision_validation_card"] = validation_card(experiment, str(gate.get("verdict") or "unconfirmed"))
                self.state["revision_document"] = render_idea_brief({
                    **idea, "method": method, "validation_card": self.state["revision_validation_card"],
                }, self._idea_evidence(idea))
                self._set_phase("R6", "保存新版本")
                return
            document = await self._call_markdown(
                role="writer",
                purpose="R3A1 修订版：摘要与引言",
                messages=writer_revision_part_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    self.state["revision_idea"],
                    self.state.get("revision_method", {}),
                    self.state.get("revision_experiment", {}),
                    self.state["revision_base_document"],
                    self.state["revision_feedback"],
                    self.state.get("revision_plan", {}),
                    "A1",
                ),
                temperature=0.45,
                max_tokens=16000,
                required_markers=["## 1. Introduction"],
            )
            self.state["revision_part_a1"] = document
            self._set_phase("R3A", "修订稿 · 相关工作与方法")
            return
        if phase == "R3A":
            document = await self._call_markdown(
                role="writer",
                purpose="R3A2 修订版：相关工作与方法",
                messages=writer_revision_part_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    self.state["revision_idea"],
                    self.state.get("revision_method", {}),
                    self.state.get("revision_experiment", {}),
                    self.state["revision_base_document"],
                    self.state["revision_feedback"],
                    self.state.get("revision_plan", {}),
                    "A2",
                    part_a=self.state.get("revision_part_a1", ""),
                ),
                temperature=0.45,
                max_tokens=18000,
                required_markers=["## 2. Related Work", "## 3. Method"],
            )
            self.state["revision_part_a2"] = document
            self._set_phase("R3B", "修订稿 · 实验与讨论")
            return
        if phase == "R3B":
            part_a = (
                self.state.get("revision_part_a")
                or (
                    self.state.get("revision_part_a1", "").rstrip()
                    + "\n\n"
                    + self.state.get("revision_part_a2", "").strip()
                )
            ).strip()
            part_b = await self._call_markdown(
                role="writer",
                purpose="R3B 修订版：实验、预期结果与讨论",
                messages=writer_revision_part_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    self.state["revision_idea"],
                    self.state.get("revision_method", {}),
                    self.state.get("revision_experiment", {}),
                    self.state["revision_base_document"],
                    self.state["revision_feedback"],
                    self.state.get("revision_plan", {}),
                    "B",
                    part_a=part_a,
                ),
                temperature=0.4,
                max_tokens=18000,
                required_markers=[
                        "## 4. Experiment",
                        "## 5. Expected Results",
                        "## 6. Discussion",
                        "## 7. Reproducibility Plan",
                        "Appendix A",
                    ],
            )
            document = part_a.rstrip() + "\n\n" + part_b.strip()
            self.state["revision_document"] = document
            self._record_message(
                "writer",
                "修订版完整文档已生成，正在执行一次一致性检查。",
                {"idea_id": self.run["base_idea_id"], "length": len(document)},
                "artifact",
            )
            self._set_phase("R4", "一致性检查")
            return
        if phase == "R4":
            result = await self._call_json(
                role="coherence_editor",
                purpose="R4 修订版一致性检查",
                contract="coherence",
                messages=coherence_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    self.state["revision_idea"],
                    self.state.get("revision_document", ""),
                ),
                temperature=0.2,
                max_tokens=5000,
            )
            issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
            self.state["revision_issues"] = issues[:2]
            self._record_message("coherence_editor", result.get("speech") or "检查完成。", result)
            self._set_phase("R5" if issues[:2] else "R6", "修订一致性修复" if issues else "保存新版本")
            return
        if phase == "R5":
            notes = json.dumps(self.state.get("revision_issues", []), ensure_ascii=False, indent=2)
            document = await self._call_markdown(
                role="writer",
                purpose="R5A1 修订版一致性修复：摘要与引言",
                messages=writer_revision_part_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    self.state["revision_idea"],
                    self.state.get("revision_method", {}),
                    self.state.get("revision_experiment", {}),
                    self.state.get("revision_document", ""),
                    self.state["revision_feedback"],
                    self.state.get("revision_plan", {}),
                    "A1",
                    revision_notes=notes,
                ),
                temperature=0.35,
                max_tokens=16000,
                required_markers=["## 1. Introduction"],
            )
            self.state["revision_part_a1_fixed"] = document
            self._set_phase("R5A", "一致性修复 · 相关工作与方法")
            return
        if phase == "R5A":
            notes = json.dumps(self.state.get("revision_issues", []), ensure_ascii=False, indent=2)
            document = await self._call_markdown(
                role="writer",
                purpose="R5A2 修订版一致性修复：相关工作与方法",
                messages=writer_revision_part_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    self.state["revision_idea"],
                    self.state.get("revision_method", {}),
                    self.state.get("revision_experiment", {}),
                    self.state.get("revision_document", ""),
                    self.state["revision_feedback"],
                    self.state.get("revision_plan", {}),
                    "A2",
                    part_a=self.state.get("revision_part_a1_fixed", ""),
                    revision_notes=notes,
                ),
                temperature=0.35,
                max_tokens=18000,
                required_markers=["## 2. Related Work", "## 3. Method"],
            )
            self.state["revision_part_a2_fixed"] = document
            self._set_phase("R5B", "一致性修复 · 实验与讨论")
            return
        if phase == "R5B":
            notes = json.dumps(self.state.get("revision_issues", []), ensure_ascii=False, indent=2)
            part_a = (
                self.state.get("revision_part_a1_fixed", "").rstrip()
                + "\n\n"
                + self.state.get("revision_part_a2_fixed", "").strip()
            ).strip()
            part_b = await self._call_markdown(
                role="writer",
                purpose="R5B 修订版一致性修复：实验与讨论",
                messages=writer_revision_part_messages(
                    self.project,
                    self.run,
                    self.skills_bundle,
                    self.state["revision_idea"],
                    self.state.get("revision_method", {}),
                    self.state.get("revision_experiment", {}),
                    self.state.get("revision_document", ""),
                    self.state["revision_feedback"],
                    self.state.get("revision_plan", {}),
                    "B",
                    part_a=part_a,
                    revision_notes=notes,
                ),
                temperature=0.35,
                max_tokens=18000,
                required_markers=[
                        "## 4. Experiment",
                        "## 5. Expected Results",
                        "## 6. Discussion",
                        "## 7. Reproducibility Plan",
                        "Appendix A",
                    ],
            )
            document = part_a.rstrip() + "\n\n" + part_b.strip()
            self.state["revision_document"] = document
            self._record_message("writer", "修订版已完成一致性修复。", {"length": len(document)}, "artifact")
            self._set_phase("R6", "保存新版本")
            return
        if phase == "R6":
            self._save_revision()
            return
        raise RuntimeError(f"未知修订阶段：{phase}")

    def _load_revision_base(self) -> None:
        idea = self.db.query_one("SELECT * FROM ideas WHERE id = ?", (self.run.get("base_idea_id"),))
        feedback = self.db.query_one("SELECT * FROM feedback WHERE id = ?", (self.run.get("feedback_id"),))
        if not idea or not feedback:
            raise RuntimeError("修订所需的基础 Idea 或反馈不存在")
        version = self.db.query_one(
            "SELECT * FROM idea_versions WHERE idea_id = ? AND version = ?",
            (idea["id"], feedback["base_version"]),
        )
        if not version:
            raise RuntimeError("基础版本不存在")
        manifest = json.loads(version["manifest_json"] or "{}")
        consensus = manifest.get("consensus")
        self.state["revision_deliverable"] = manifest.get("deliverable", "manuscript")
        self.state["revision_idea"] = {
            "title": idea["title"],
            "summary": idea["summary"],
            "idea_id": idea["id"],
            "consensus": consensus if isinstance(consensus, dict) else {},
        }
        self.state["revision_base_document"] = self.artifacts.read_relative_text(version["path"])
        self.state["revision_feedback"] = feedback["report_text"]
        self.state["revision_instructions"] = feedback["instructions"]
        parts = []
        if feedback["instructions"]:
            parts.append("**修订要求**\n\n" + feedback["instructions"][:12000])
        if feedback["report_text"]:
            parts.append("**补充的实验结果或观察**\n\n" + feedback["report_text"][:12000])
        self._record_message(
            "user",
            "\n\n".join(parts),
            {"feedback_id": feedback["id"], "base_version": feedback["base_version"]},
            "user_input",
        )

    def _save_revision(self) -> None:
        idea = self.db.query_one("SELECT * FROM ideas WHERE id = ?", (self.run.get("base_idea_id"),))
        if not idea:
            raise RuntimeError("基础 Idea 不存在")
        plan = self.state.get("revision_plan", {})
        revision_markdown = self._render_revision_markdown(plan)
        brief = self.state.get("revision_deliverable") == "idea_brief"
        missing_sections = [] if brief else document_completion_issues(self.state.get("revision_document", ""))
        version = self.artifacts.save_version(
            idea={"id": idea["id"], "slug": idea["slug"]},
            project_id=self.project["id"],
            document=self.state.get("revision_document", ""),
            operation_key=self.run_id,
            manifest={
                "title": self.state["revision_idea"].get("title"),
                "deliverable": "idea_brief" if brief else "manuscript",
                "validation_card": self.state.get("revision_validation_card", {}),
                "status": "INCOMPLETE" if missing_sections else "COMPLETED",
                "missing_sections": missing_sections,
                "revision_type": plan.get("revision_type", "LOCAL_REVISE"),
                "feedback_id": self.run.get("feedback_id"),
                "base_idea_id": idea["id"],
                "coherence_issues": self.state.get("revision_issues", []),
                "consensus": self.state.get("revision_idea", {}).get("consensus", {}),
                "skills": self.skill_snapshot,
                "specs": self.state.get("spec_snapshot", {}),
                "model": self.run.get("model"),
            },
            revision_markdown=revision_markdown,
        )
        self.state["revision_version"] = version["version"]
        self.state["revision_path"] = version["revision_path"]
        self.events.publish(
            self.run_id,
            "artifact.updated",
            {
                "idea_id": idea["id"],
                "version": version["version"],
                "path": version["path"],
                "revision_path": version["revision_path"],
                "title": idea["title"],
            },
        )
        self._record_message(
            "pi",
            f"已生成《{idea['title']}》的 v{version['version']:03d} 修订版本和修订说明。",
            {"idea_id": idea["id"], "version": version["version"], "path": version["path"]},
            "artifact",
        )
        self._complete_revision(version["version"])

    def _render_revision_markdown(self, plan: dict[str, Any]) -> str:
        mappings = plan.get("claim_mappings", []) or []
        mapping_lines = "\n".join(
            f"- **主张**：{item.get('claim', '')}\n  - 关系：{item.get('relation', '')}\n"
            f"  - 用户证据：{item.get('evidence', '')}\n  - 解释：{item.get('interpretation', '')}"
            for item in mappings
        ) or "- 用户报告未提供足以建立主张映射的明确结果。"
        decisions = "\n".join(f"- {item}" for item in plan.get("decisions", []) or []) or "- 保留基础版本并补充说明。"
        sections = ", ".join(plan.get("affected_sections", []) or [])
        return f"""# 修订说明

## 修订类型

`{plan.get('revision_type', 'LOCAL_REVISE')}`

## 用户提供的实验报告

以下内容来自用户提交，按 `USER_REPORTED` 保存，不代替用户对实验有效性的判断：

{self.state.get('revision_feedback', '')[:12000] or '本轮未提供实验报告。'}

## 主张映射

{mapping_lines}

## 修订决策

{decisions}

## 受影响章节

{sections or '未明确列出'}

## 修订计划

{plan.get('revision_plan', '')}

## 说明

模型对证据的解释属于 `MODEL_INFERENCE`，不代替用户对实验有效性的判断。文档中的剩余预期结果仍标记为 `EXPECTED`。
"""

    def _complete_revision(self, version: int) -> None:
        self.state["phase"] = "DONE"
        self._save_state()
        now = utc_now()
        self.db.execute(
            "UPDATE runs SET status = ?, phase = ?, phase_label = ?, ended_at = ?, updated_at = ?, error = ? "
            "WHERE id = ?",
            (
                "COMPLETED",
                "DONE",
                f"修订完成 · v{version:03d}",
                now,
                now,
                None,
                self.run_id,
            ),
        )
        self.db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, self.project["id"]))
        self.events.publish(
            self.run_id,
            "run.completed",
            {
                "status": "COMPLETED",
                "version": version,
                "revision_path": self.state.get("revision_path"),
            },
        )


class RunManager:
    def __init__(
        self,
        database: Database,
        settings_store: SettingsStore,
        artifacts: ArtifactStore,
        events: EventBus,
        gateway: ModelGateway,
        live_feed=None,
    ) -> None:
        self.db = database
        self.settings_store = settings_store
        self.artifacts = artifacts
        self.events = events
        self.gateway = gateway
        self.live_feed = live_feed
        self.engines: dict[str, RunEngine] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def start(self, run_id: str) -> None:
        existing = self.tasks.get(run_id)
        if existing and not existing.done():
            return
        engine = RunEngine(
            run_id,
            self.db,
            self.settings_store,
            self.artifacts,
            self.events,
            self.gateway,
            self.live_feed,
        )
        self.engines[run_id] = engine
        self.tasks[run_id] = asyncio.create_task(engine.execute())

    def request_pause(self, run_id: str) -> None:
        engine = self.engines.get(run_id)
        if engine:
            engine.pause_requested = True
        self.db.execute(
            "UPDATE runs SET status = 'PAUSED', updated_at = ? WHERE id = ? AND status = 'RUNNING'",
            (utc_now(), run_id),
        )
        self.events.publish(run_id, "run.status", {"status": "PAUSED", "phase": None})

    def cancel(self, run_id: str) -> None:
        engine = self.engines.get(run_id)
        if engine:
            engine.cancel_requested = True
        self.db.execute(
            "UPDATE runs SET status = 'CANCELLED', ended_at = ?, updated_at = ? WHERE id = ?",
            (utc_now(), utc_now(), run_id),
        )
        task = self.tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        self.events.publish(run_id, "run.status", {"status": "CANCELLED"})

    async def resume(self, run_id: str) -> bool:
        run = self.db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run or run["status"] not in {"PAUSED", "FAILED", "BUDGET_EXHAUSTED"}:
            return False
        active = self.tasks.get(run_id)
        if active and not active.done():
            engine = self.engines.get(run_id)
            if not engine:
                return False
            engine.pause_requested = False
            self.db.execute(
                "UPDATE runs SET status = 'RUNNING', error = NULL, ended_at = NULL, updated_at = ? WHERE id = ?",
                (utc_now(), run_id),
            )
            self.events.publish(run_id, "run.status", {"status": "RUNNING"})
            return True
        self.db.execute(
            "UPDATE runs SET status = 'RUNNING', error = NULL, ended_at = NULL, updated_at = ? WHERE id = ?",
            (utc_now(), run_id),
        )
        self.start(run_id)
        return True

    def recover_interrupted(self) -> int:
        self.db.execute(
            "UPDATE model_calls SET status = 'interrupted', error = ? WHERE status = 'requesting'",
            ("服务在请求完成前中断",),
        )
        count = self.db.execute(
            "UPDATE runs SET status = 'PAUSED', error = ?, updated_at = ? WHERE status = 'RUNNING'",
            ("服务重启后已暂停，可从检查点继续", utc_now()),
        )
        return count

    async def shutdown(self) -> None:
        for engine in self.engines.values():
            engine.shutting_down = True
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
