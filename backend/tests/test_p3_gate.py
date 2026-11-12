from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.config import ModelCallSettings
from app.db import Database, utc_now
from app.services import artifacts as artifacts_module
from app.services.artifacts import ArtifactStore
from app.services.engine import InvalidModelOutput, RunEngine
from app.services.events import EventBus
from app.services.model_gateway import BudgetExhausted, ModelCallError, ModelGateway, ModelResult

DOCUMENT = """# Test Idea

## Abstract
Text

## 1. Introduction
Text

## 2. Related Work
Text

## 3. Method
### 3.5 Implementation
Text

## 4. Experiment
Text

## 5. Expected Results
Text

## 6. Discussion and Limitations
Text

## 7. Reproducibility Plan
Text

## Appendix A. Literature
Text
"""


def _engine(tmp_path, monkeypatch) -> RunEngine:
    monkeypatch.setattr(artifacts_module, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(artifacts_module, "DATA_DIR", tmp_path)
    db = Database(tmp_path / "gate.db")
    now = utc_now()
    db.execute(
        "INSERT INTO projects (id, name, topic, venue, domain, constraints, language, skills_json, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("p1", "Project", "Topic", "ACL", "NLP", "", "zh-CN", "[]", now, now),
    )
    db.execute(
        "INSERT INTO runs (id, project_id, mode, status, phase, phase_label, budget_max, budget_used, "
        "candidate_count, idea_count, team_rounds, model, language, config_json, state_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "p1", "GENERATE", "RUNNING", "P3", "深化", 80, 0, 2, 1, 1, "test", "zh-CN", "{}", "{}", now, now),
    )
    engine = RunEngine(
        run_id="r1",
        database=db,
        settings_store=None,  # type: ignore[arg-type]
        artifacts=ArtifactStore(db),
        events=EventBus(db),
        gateway=None,  # type: ignore[arg-type]
    )
    engine.run = db.query_one("SELECT * FROM runs WHERE id = ?", ("r1",))
    engine.project = db.query_one("SELECT * FROM projects WHERE id = ?", ("p1",))
    engine.state = {
        "phase": "P3",
        "candidates": [{"id": "candidate_1", "title": "Idea A"}],
        "selected": [
            {
                "candidate_id": "candidate_1",
                "title": "Idea A",
                "one_sentence": "A",
                "consensus": {"thesis": "t", "core_mechanism": "m", "unresolved": []},
                "stage": "method",
            }
        ],
        "idea_next": 0,
        "review_notes": [],
        "objections": [],
        "recent_messages": [],
        "decision_digest": [],
        "context_summaries": {},
        "user_inputs": [],
        "evidence": [],
        "research_turns": [],
        "idea_states": {},
        "decision_log": [],
    }
    return engine


def test_method_revise_reruns_method_and_passes_on_second_gate(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    calls: list[str] = []
    gates = 0

    async def fake_call_json(**kwargs: Any) -> dict[str, Any]:
        nonlocal gates
        purpose = str(kwargs["purpose"])
        calls.append(purpose)
        if purpose.startswith("P3 idea1 方法机制"):
            return {"method_overview": "m", "speech": "method"}
        if purpose.startswith("P3 idea1 方法修订"):
            return {"method_overview": "m2", "speech": "method revise"}
        if purpose.startswith("P3 idea1 实验设计"):
            return {"claims": [], "speech": "experiment"}
        if purpose.startswith("P3 idea1 实验修订"):
            return {"claims": [], "speech": "experiment revise"}
        if purpose.startswith("P3 idea1 方法-实验门禁"):
            gates += 1
            if gates == 1:
                return {
                    "verdict": "method_revise",
                    "issues": [{"location": "method", "problem": "实验无法检验假设", "fix": "补对照"}],
                    "speech": "revise",
                }
            return {"verdict": "pass", "issues": [], "speech": "ok"}
        if purpose.startswith("P3 idea1 一致性检查"):
            return {"issues": [], "verdict": "pass", "speech": "ok"}
        raise AssertionError(f"unexpected purpose: {purpose}")

    async def fake_markdown(**kwargs: Any) -> str:
        calls.append(str(kwargs["purpose"]))
        return DOCUMENT

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def no_literature(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    engine._call_json = fake_call_json  # type: ignore[method-assign]
    engine._call_markdown = fake_markdown  # type: ignore[method-assign]
    engine._prepare_context = noop  # type: ignore[method-assign]
    engine._run_literature_queries = no_literature  # type: ignore[method-assign]
    engine._profile_ids_for = lambda role: []  # type: ignore[method-assign]

    asyncio.run(engine._phase_p3())

    assert gates == 2
    # 门禁回修是定向修订：第二轮方法/实验是单模型修订调用，不再重跑面板
    assert len([call for call in calls if call.endswith("方法机制")]) == 1
    assert len([call for call in calls if call.endswith("方法修订")]) == 1
    assert len([call for call in calls if call.endswith("实验设计")]) == 1
    assert len([call for call in calls if call.endswith("实验修订")]) == 1
    assert engine.state["selected"][0]["stage"] == "done"
    assert engine.state["selected"][0]["version"] == 1
    assert engine.state["idea_next"] == 1
    assert engine.state["phase"] == "P4"


def test_brief_is_saved_with_unconfirmed_gate_without_manuscript_calls(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    engine.config["deliverable"] = "idea_brief"
    calls: list[str] = []

    async def fake_json(**kwargs: Any) -> dict[str, Any]:
        purpose = kwargs["purpose"]
        calls.append(purpose)
        if "方法机制" in purpose:
            return {"method_overview": "机制描述"}
        if "实验设计" in purpose:
            return {"first_validation": {"hypothesis": "首个假设"}}
        return {"issues": []}

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def no_literature(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    engine._call_json = fake_json  # type: ignore[method-assign]
    engine._prepare_context = noop  # type: ignore[method-assign]
    engine._run_literature_queries = no_literature  # type: ignore[method-assign]
    engine._profile_ids_for = lambda role: []  # type: ignore[method-assign]

    asyncio.run(engine._phase_p3())

    selected = engine.state["selected"][0]
    assert len(calls) == 3
    assert selected["validation_card"]["status"] == "pending"
    assert "## 首轮验证" in selected["draft"]
    assert "一致性判定缺失" in selected["draft"]
    assert selected["version"] == 1

    engine.state.update({
        "revision_deliverable": "idea_brief",
        "revision_base_document": selected["draft"],
        "revision_idea": {"title": selected["title"], "summary": "原主张", "idea_id": selected["idea_id"]},
        "revision_plan": {"revised_thesis": "修订后主张"},
        "revision_experiment": {"first_validation": {"hypothesis": "修订后实验"}},
    })
    asyncio.run(engine._drive_revision_phase("R3"))
    assert engine.state["phase"] == "R6"
    assert "修订后主张" in engine.state["revision_document"]
    assert "修订后实验" in engine.state["revision_document"]


def test_gateway_reads_anthropic_stream_and_accounts_for_usage(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    gateway = ModelGateway(engine.db)
    settings = ModelCallSettings(
        id="sonnet", model="claude-sonnet-5-5", api_format="anthropic",
        base_url="https://provider.example/v1", api_key="test", reasoning_effort="high",
        input_price_per_million=2, output_price_per_million=10,
    )
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 20, "cache_read_input_tokens": 5}}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "plan"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "answer"}},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 7}},
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://provider.example/v1/messages"
        assert request.headers["x-api-key"] == "test"
        assert request.headers["anthropic-version"] == "2023-06-01"
        payload = json.loads(request.content)
        assert payload["system"] == "System rules"
        assert payload["messages"] == [{"role": "user", "content": "Question"}]
        assert payload["thinking"] == {"type": "adaptive"}
        assert payload["output_config"] == {"effort": "high"}
        assert "temperature" not in payload and "stream_options" not in payload
        body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    async def run() -> None:
        gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            deltas = []
            result = await gateway.complete(
                run_id="r1", role="writer", purpose="test", settings=settings,
                messages=[{"role": "system", "content": "System rules"}, {"role": "user", "content": "Question"}],
                on_delta=lambda kind, text: deltas.append((kind, text)),
            )
            assert result.content == "answer" and result.finish_reason == "length"
            assert result.prompt_tokens == 25 and result.completion_tokens == 7
            assert result.cost_usd == pytest.approx(0.00012)
            assert deltas == [("reasoning", "plan"), ("content", "answer")]
        finally:
            await gateway.close()

    asyncio.run(run())
    assert engine.db.query_one("SELECT budget_used FROM runs WHERE id='r1'")["budget_used"] == 1


def test_gateway_retries_429_and_charges_each_attempt(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    engine.db.execute("UPDATE runs SET budget_max = 2 WHERE id = 'r1'")
    gateway = ModelGateway(engine.db)
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    async def run() -> None:
        gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        settings = ModelCallSettings(id="test", api_key="test", base_url="https://provider.example/v1", model="test")
        call = {"run_id": "r1", "role": "pi", "purpose": "test", "messages": [], "settings": settings}
        try:
            result = await gateway.complete(**call)
            assert result.content == "answer"
            with pytest.raises(BudgetExhausted):
                await gateway.complete(**call)
        finally:
            await gateway.close()

    asyncio.run(run())
    assert requests == 2


def test_model_contract_validates_repair_before_accepting_proposal(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    component = '{"name":"Only a component","mechanism":"partial"}'
    blueprint = json.dumps({
        "problem_formulation": "task", "core_insight": "insight", "method_overview": "method",
        "components": [{"name": "part"}], "procedure": ["step"],
    })
    responses = iter([component, blueprint, component, component])
    purposes: list[str] = []

    async def fake_raw(**kwargs: Any) -> ModelResult:
        purposes.append(kwargs["purpose"])
        return ModelResult(next(responses), 0, 0, 0, 0)

    engine._call_raw = fake_raw  # type: ignore[method-assign]
    call = {"role": "method_architect", "purpose": "method", "messages": [], "contract": "method"}
    assert asyncio.run(engine._call_json(**call))["method_overview"] == "method"
    with pytest.raises(InvalidModelOutput, match="method_overview"):
        asyncio.run(engine._call_json(**call))
    assert purposes == ["method", "method / JSON 修复", "method", "method / JSON 修复"]


def test_panel_uses_only_valid_proposals_and_skips_single_proposal_synthesis(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    engine._profile_label = lambda profile_id: profile_id  # type: ignore[method-assign]
    calls = []

    async def fake_json(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["purpose"])
        if kwargs["profile_id"] == "partial":
            raise InvalidModelOutput("method_overview missing")
        return {"method_overview": "accepted proposal"}

    engine._call_json = fake_json  # type: ignore[method-assign]
    result = asyncio.run(engine._panel_json(
        role="method_architect", purpose="method panel", kind="method",
        idea={}, messages=[], profiles=["partial", "complete"], temperature=0.4, max_tokens=2000,
    ))
    assert result["method_overview"] == "accepted proposal"
    assert [item["profile_id"] for item in result["panel_proposals"]] == ["complete"]
    assert calls == ["method panel / partial", "method panel / complete"]


def test_resume_after_artifact_commit_keeps_one_version(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    engine.config["deliverable"] = "idea_brief"
    engine.state["selected"][0].update(stage="save", draft="# Saved document")
    engine._save_state()
    publish = engine.events.publish

    async def skip_context(*args: Any, **kwargs: Any) -> None:
        return None

    def fail_before_checkpoint(run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "artifact.updated":
            raise RuntimeError("interrupted after commit")
        publish(run_id, event_type, payload)

    engine._prepare_context = skip_context  # type: ignore[method-assign]
    engine.events.publish = fail_before_checkpoint  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="interrupted after commit"):
        asyncio.run(engine._phase_p3())
    engine.state = json.loads(engine._get_run()["state_json"])
    assert engine.state["selected"][0]["stage"] == "save"

    engine.events.publish = publish  # type: ignore[method-assign]
    asyncio.run(engine._phase_p3())
    assert engine.db.query_all("SELECT version FROM idea_versions") == [{"version": 1}]
    assert engine.state["selected"][0]["version"] == 1


def test_gateway_deadline_stops_stream_that_keeps_sending(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    gateway = ModelGateway(engine.db)
    closed = False
    chunks = 0

    class ContinuousStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal chunks
            while True:
                chunks += 1
                yield b'data: {"choices":[{"delta":{"content":"part"}}]}\n\n'
                await asyncio.sleep(0.01)

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    async def run() -> None:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ContinuousStream())
        )
        gateway._client = httpx.AsyncClient(transport=transport)
        settings = ModelCallSettings(id="test", api_key="test", base_url="https://provider.example/v1", model="test")
        settings = settings.model_copy(update={"request_timeout_seconds": 0.30})
        try:
            with pytest.raises(ModelCallError, match="总时限"):
                await gateway.complete(run_id="r1", role="pi", purpose="stream", messages=[], settings=settings)
        finally:
            await gateway.close()

    asyncio.run(run())
    assert chunks > 1 and closed
    assert engine.db.query_all("SELECT status FROM model_calls") == [{"status": "timed_out"}]
    assert engine._get_run()["budget_used"] == 1


def test_thesis_conflict_rolls_back_to_debate_once(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    gates = 0
    debates = 0

    async def fake_call_json(**kwargs: Any) -> dict[str, Any]:
        nonlocal gates, debates
        purpose = str(kwargs["purpose"])
        if purpose.startswith("P3 idea1 方法机制"):
            return {"method_overview": "m", "speech": "method"}
        if purpose.startswith("P3 idea1 实验设计"):
            return {"claims": [], "speech": "experiment"}
        if purpose.startswith("P3 idea1 方法-实验门禁"):
            gates += 1
            if gates == 1:
                return {
                    "verdict": "thesis_conflict",
                    "issues": [{"location": "thesis", "problem": "核心假设与实验冲突", "fix": "重设假设"}],
                    "speech": "conflict",
                }
            return {"verdict": "pass", "issues": [], "speech": "ok"}
        if purpose.startswith("P2.3 辩论"):
            debates += 1
            return {"stance": "pass", "information_gain": "none", "speech": "pass"}
        if purpose.startswith("P2.4 研究决策"):
            return {
                "thesis": "new thesis",
                "core_mechanism": "new mechanism",
                "unresolved": [],
                "speech": "decided",
            }
        if purpose.startswith("P3 idea1 一致性检查"):
            return {"issues": [], "verdict": "pass", "speech": "ok"}
        raise AssertionError(f"unexpected purpose: {purpose}")

    async def fake_markdown(**kwargs: Any) -> str:
        return DOCUMENT

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def no_literature(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    engine._call_json = fake_call_json  # type: ignore[method-assign]
    engine._call_markdown = fake_markdown  # type: ignore[method-assign]
    engine._prepare_context = noop  # type: ignore[method-assign]
    engine._run_literature_queries = no_literature  # type: ignore[method-assign]
    engine._profile_ids_for = (  # type: ignore[method-assign]
        lambda role: ["m1", "m2"] if role == "method_architect" else []
    )
    engine._profile_label = lambda profile_id: profile_id  # type: ignore[method-assign]

    asyncio.run(engine._phase_p3())

    assert gates == 2
    assert debates >= 1
    assert engine.state["selected"][0]["rollback_count"] == 1
    assert engine.state["selected"][0]["consensus"]["thesis"] == "new thesis"
    assert engine.state["selected"][0]["stage"] == "done"
    assert engine.state["phase"] == "P4"
