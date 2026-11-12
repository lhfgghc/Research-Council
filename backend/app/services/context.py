from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..config import ModelCallSettings
from ..db import Database, utc_now

SUMMARY_SYSTEM = """你是科研讨论的上下文压缩器。
只依据给定的历史消息生成摘要，不添加新事实，不评价研究质量，不模拟审稿人。
必须保留：已决定事项及理由、被否决路线及理由、未决问题、证据状态、用户约束。
输出必须是合法 JSON，不要代码围栏，不要解释。
""".strip()

SUMMARY_REQUEST = """请把下面的讨论压缩成可供后续研究会议使用的结构化摘要。

现有摘要：
<EXISTING>
{existing}
</EXISTING>

需要压缩的消息：
<MESSAGES>
{messages}
</MESSAGES>

返回 JSON：
{{
  "summary": "300~600 字的连贯纪要，说明讨论进展、关键取舍和当前状态",
  "decisions": ["已决定事项，附简短理由"],
  "rejected": ["被否决或收窄的路线，附理由"],
  "open_questions": ["仍未解决的问题"],
  "evidence": ["用户报告或已核实的事实；没有则空数组"],
  "next_actions": ["下一步应完成的具体动作"]
}}

要求：
- 不得把模型推测写成事实。
- 不得遗漏用户明确提出的约束。
- 同一事项只保留一条，合并重复表述。
- 如果现有摘要已经包含某事项，不要重复展开。
- summary 不超过 350 字；每个数组最多 6 项。
- 只输出 JSON 对象，第一个字符必须是 {{，最后一个字符必须是 }}。
- 不要输出 Markdown、标题、解释或任何前后缀。
""".strip()


def estimate_tokens(text: str) -> int:
    """Conservative mixed Chinese/English estimate, avoiding a tokenizer dependency."""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    other = max(0, len(text) - cjk)
    return max(1, int(cjk / 1.35 + other / 3.6) + 1)


def estimate_messages(messages: list[dict[str, str]]) -> int:
    return sum(estimate_tokens(str(item.get("content") or "")) + 8 for item in messages)


def mask_messages(messages: list[dict[str, Any]], max_chars: int = 1200) -> list[dict[str, Any]]:
    """Deterministic, loss-minimizing pass: drop noise and duplicates before any LLM summary."""
    seen: set[str] = set()
    masked: list[dict[str, Any]] = []
    for item in messages:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        message_type = str(item.get("type") or "speech")
        if message_type == "speech" and len(content) < 60:
            continue
        normalized = re.sub(r"\s+", "", content)[:240]
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        masked.append(
            {
                "id": item.get("id"),
                "role": item.get("role") or "",
                "type": message_type,
                "content": content[:max_chars],
            }
        )
    return masked


def extractive_summary(existing: str, messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in messages[-16:]:
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if not content:
            continue
        first = re.split(r"(?<=[。！？.!?])\s*", content)[0][:180]
        lines.append(f"- [{item.get('role') or '成员'}] {first}")
    body = "历史讨论要点（抽取式，未经模型改写）：\n" + ("\n".join(lines) or "- 无")
    return f"{existing.rstrip()}\n\n{body}".strip() if existing else body


def _as_text_list(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:400])
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content") or item.get("claim") or ""
            if text:
                result.append(str(text).strip()[:400])
    return result


def render_summary(data: dict[str, Any], fallback: str) -> str:
    summary = str(data.get("summary") or "").strip()
    parts = [summary or fallback]
    for key, label in (
        ("decisions", "已决定"),
        ("rejected", "已否决或收窄"),
        ("open_questions", "未决问题"),
        ("evidence", "证据状态"),
        ("next_actions", "下一步"),
    ):
        items = _as_text_list(data.get(key))
        if items:
            parts.append(f"{label}：\n" + "\n".join(f"- {item}" for item in items))
    return "\n\n".join(part for part in parts if part).strip()


def current_scope(state: dict[str, Any]) -> str:
    phase = str(state.get("phase") or "P0")
    idea_id = state.get("current_idea_id")
    if phase.startswith("P3") and idea_id:
        return f"P3:{idea_id}"
    if phase == "P2" and idea_id:
        return f"P2:{idea_id}"
    if phase.startswith("R") and idea_id:
        return f"R:{idea_id}"
    return "RUN"


@dataclass
class ContextSnapshot:
    id: int
    estimated_tokens: int
    budget_tokens: int
    ratio: float
    compressed: bool
    strategy: str
    summary_id: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


class ContextManager:
    """Assembles a bounded working set and compacts old discussion into traceable summaries."""

    def __init__(
        self,
        db: Database,
        run_id: str,
        state: dict[str, Any],
        settings: ModelCallSettings,
    ) -> None:
        self.db = db
        self.run_id = run_id
        self.state = state
        self.settings = settings

    @property
    def scope(self) -> str:
        return current_scope(self.state)

    def budget_tokens(self) -> int:
        window = max(4_000, int(self.settings.context_window_tokens))
        if self.settings.max_input_tokens:
            return min(int(self.settings.max_input_tokens), int(window * 0.90))
        return max(4_000, int(window * 0.70))

    def summary_text(self) -> str:
        summaries = self.state.get("context_summaries") or {}
        parts: list[str] = []
        run_item = summaries.get("RUN") or {}
        if run_item.get("text"):
            parts.append(f"项目阶段摘要：\n{run_item['text']}")
        if self.scope != "RUN":
            item = summaries.get(self.scope) or {}
            if item.get("text"):
                parts.append(f"当前 Idea 摘要：\n{item['text']}")
        return "\n\n".join(parts)

    def scope_summary_text(self) -> str:
        summaries = self.state.get("context_summaries") or {}
        item = summaries.get(self.scope) or {}
        return str(item.get("text") or "")

    def covered_ids(self) -> set[int]:
        rows = self.db.query_all(
            "SELECT covered_start, covered_end, covered_ids_json FROM run_summaries "
            "WHERE run_id = ? AND scope = ?",
            (self.run_id, self.scope),
        )
        covered: set[int] = set()
        for row in rows:
            try:
                ids = json.loads(row.get("covered_ids_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                ids = []
            if ids:
                covered.update(int(value) for value in ids if value is not None)
                continue
            start = int(row.get("covered_start") or 0)
            end = int(row.get("covered_end") or 0)
            if start and end:
                covered.update(range(start, end + 1))
        return covered

    def scoped_messages(self) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT id, role, role_label, message_type AS type, content, created_at "
            "FROM messages WHERE run_id = ? AND (scope = ? OR scope = '') AND message_type != 'thinking' "
            "ORDER BY id DESC LIMIT 80",
            (self.run_id, self.scope),
        )
        if rows:
            return list(reversed(rows))
        messages = list(self.state.get("recent_messages") or [])
        scoped = [
            item
            for item in messages
            if not item.get("scope") or str(item.get("scope")) == self.scope
        ]
        return scoped

    def uncovered_messages(self) -> list[dict[str, Any]]:
        covered = self.covered_ids()
        if not covered:
            return self.scoped_messages()
        return [
            item
            for item in self.scoped_messages()
            if int(item.get("id") or 0) not in covered
        ]

    def estimate(self, base_tokens: int = 0) -> int:
        parts = [
            str(self.state.get("agenda") or ""),
            str(self.state.get("problem_frame") or ""),
            self.summary_text(),
            "\n".join(self.state.get("decision_digest") or []),
            "\n".join(str(item) for item in (self.state.get("objections") or [])[-8:]),
            "\n".join(str(item) for item in (self.state.get("recent_literature") or [])[-10:]),
            str(self.state.get("revision_feedback") or "")[:24000],
        ]
        for item in self.uncovered_messages()[-10:]:
            parts.append(str(item.get("content") or "")[:1600])
        return base_tokens + estimate_tokens("\n".join(parts))

    def _remaining_budget(self) -> int:
        run = self.db.query_one(
            "SELECT budget_used, budget_max FROM runs WHERE id = ?", (self.run_id,)
        )
        if not run:
            return 0
        return max(0, int(run["budget_max"]) - int(run["budget_used"]))

    def _save_summary(
        self,
        *,
        text: str,
        schema: dict[str, Any],
        covered: list[dict[str, Any]],
        model: str,
    ) -> int:
        ids = [int(item["id"]) for item in covered if item.get("id") is not None]
        previous = self.covered_ids()
        all_ids = sorted(previous | set(ids))
        created = utc_now()
        summary_id = self.db.insert(
            "INSERT INTO run_summaries "
            "(run_id, scope, covered_start, covered_end, covered_ids_json, summary_text, "
            "schema_json, token_count, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id,
                self.scope,
                min(ids) if ids else 0,
                max(ids) if ids else 0,
                json.dumps(all_ids, ensure_ascii=False),
                text,
                json.dumps(schema, ensure_ascii=False),
                estimate_tokens(text),
                model,
                created,
            ),
        )
        summaries = self.state.setdefault("context_summaries", {})
        summaries[self.scope] = {
            "text": text,
            "summary_id": summary_id,
            "covered_ids": all_ids,
            "created_at": created,
        }
        covered_ids = set(all_ids)
        self.state["recent_messages"] = [
            item
            for item in (self.state.get("recent_messages") or [])
            if item.get("id") not in covered_ids
        ]
        return summary_id

    def _insert_snapshot(
        self,
        *,
        purpose: str,
        estimated: int,
        budget: int,
        compressed: bool,
        strategy: str,
        summary_id: int | None,
        details: dict[str, Any],
    ) -> int:
        ratio = round(estimated / budget, 4) if budget else 0.0
        return self.db.insert(
            "INSERT INTO context_snapshots "
            "(run_id, phase, purpose, estimated_tokens, budget_tokens, ratio, compressed, strategy, "
            "summary_id, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id,
                str(self.state.get("phase") or ""),
                purpose,
                estimated,
                budget,
                ratio,
                1 if compressed else 0,
                strategy,
                summary_id,
                json.dumps(details, ensure_ascii=False),
                utc_now(),
            ),
        )

    async def prepare(
        self,
        *,
        purpose: str,
        base_tokens: int,
        summarize: Callable[[str, list[dict[str, Any]]], Awaitable[dict[str, Any]]] | None,
        force: bool = False,
    ) -> ContextSnapshot:
        budget = self.budget_tokens()
        estimated = self.estimate(base_tokens)
        trigger = budget * float(self.settings.context_compression_trigger)
        hard = budget * float(self.settings.context_compression_hard)

        if not self.settings.context_compression_enabled or (
            not force and estimated <= trigger
        ):
            snapshot_id = self._insert_snapshot(
                purpose=purpose,
                estimated=estimated,
                budget=budget,
                compressed=False,
                strategy="disabled" if not self.settings.context_compression_enabled else "none",
                summary_id=None,
                details={"scope": self.scope, "messages": len(self.scoped_messages())},
            )
            return ContextSnapshot(
                snapshot_id, estimated, budget, estimated / budget if budget else 0, False,
                "disabled" if not self.settings.context_compression_enabled else "none",
            )

        messages = self.uncovered_messages()
        keep_tail = 4
        candidates = [
            item
            for item in messages[:-keep_tail]
            if str(item.get("type") or "speech") in {"speech", "panel"}
        ]
        masked = mask_messages(candidates)
        strategy = "mask"
        summary_id: int | None = None
        summary_data: dict[str, Any] = {}
        summary_text = self.scope_summary_text()

        if len(masked) >= 2:
            if summarize is not None and self._remaining_budget() >= 3:
                try:
                    summary_data = await summarize(summary_text, masked)
                    strategy = "llm_summary"
                except Exception as exc:  # compression must never break the run
                    summary_data = {}
                    strategy = "extractive_fallback"
                    self.state["last_compression_error"] = str(exc)[:500]
            fallback = extractive_summary(summary_text, masked)
            text = render_summary(summary_data, fallback) if summary_data else fallback
            summary_id = self._save_summary(
                text=text,
                schema=summary_data,
                covered=masked,
                model=self.settings.model,
            )
            estimated = self.estimate(base_tokens)

        if estimated > hard:
            # Last deterministic resort: keep only the newest two messages plus pinned records.
            pinned_types = {"artifact", "user_input", "decision"}
            recent = list(self.state.get("recent_messages") or [])
            pinned = [item for item in recent if str(item.get("type") or "") in pinned_types]
            tail = recent[-2:]
            merged: list[dict[str, Any]] = []
            seen_ids: set[Any] = set()
            for item in pinned + tail:
                key = item.get("id")
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                merged.append(item)
            self.state["recent_messages"] = merged
            strategy = f"{strategy}+hard_trim"
            estimated = self.estimate(base_tokens)

        compressed = summary_id is not None or strategy.endswith("hard_trim")
        snapshot_id = self._insert_snapshot(
            purpose=purpose,
            estimated=estimated,
            budget=budget,
            compressed=compressed,
            strategy=strategy,
            summary_id=summary_id,
            details={
                "scope": self.scope,
                "messages": len(messages),
                "candidates": len(candidates),
                "masked": len(masked),
                "summary_id": summary_id,
            },
        )
        return ContextSnapshot(
            snapshot_id,
            estimated,
            budget,
            estimated / budget if budget else 0,
            compressed,
            strategy,
            summary_id,
        )


def hard_trim_messages(
    messages: list[dict[str, str]], budget_tokens: int
) -> list[dict[str, str]]:
    """Trim an already-built prompt without dropping the system message or newest user turn."""
    if len(messages) <= 2 or estimate_messages(messages) <= budget_tokens:
        return messages
    system = [item for item in messages if item.get("role") == "system"]
    rest = [item for item in messages if item.get("role") != "system"]
    if not rest:
        return messages

    def rebuild(keep_last: int) -> list[dict[str, str]]:
        kept = rest[-keep_last:] if keep_last else []
        return system + kept

    for keep_last in (2, 1):
        candidate = rebuild(keep_last)
        if estimate_messages(candidate) <= budget_tokens:
            return candidate
    return rebuild(1)


def fit_messages_to_budget(
    messages: list[dict[str, str]], budget_tokens: int
) -> list[dict[str, str]]:
    """Last-resort guard for a single oversized task payload.

    The newest user message is preserved at both ends; only its middle is removed.
    This keeps the task statement and the output schema while avoiding a hard
    context-window failure on smaller models.
    """
    if not messages or estimate_messages(messages) <= budget_tokens:
        return messages
    system = [item for item in messages if item.get("role") == "system"]
    rest = [item for item in messages if item.get("role") != "system"]
    if not rest:
        return messages
    system_tokens = estimate_messages(system)
    remaining = max(800, budget_tokens - system_tokens - 120)
    last = rest[-1]
    text = str(last.get("content") or "")
    max_chars = max(1600, int(remaining * 1.25))
    if len(text) > max_chars:
        head = int(max_chars * 0.45)
        tail = max(400, max_chars - head - 40)
        text = (
            text[:head]
            + "\n\n[中间内容因单次输入预算被截断；任务开头与输出格式保留]\n\n"
            + text[-tail:]
        )
    fitted = system + [{**last, "content": text}]
    return fitted
