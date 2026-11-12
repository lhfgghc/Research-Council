from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import httpx

from app.config import AppSettings, ModelProfile, SettingsStore
from app.db import Database, utc_now
from app.services import artifacts as artifacts_module
from app.services.artifacts import ArtifactStore
from app.services.engine import RunEngine
from app.services.events import EventBus
from app.services.context import estimate_messages
from app.services.model_gateway import ModelGateway, ModelResult
from app.services.orchestrator import Action, Participant


def _engine(tmp_path, settings: AppSettings) -> RunEngine:
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save(settings.model_dump())
    db = Database(tmp_path / "cap.db")
    now = utc_now()
    db.execute(
        "INSERT INTO projects (id, name, topic, venue, domain, constraints, language, skills_json, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("p1", "P", "T", "ACL", "D", "", "zh-CN", "[]", now, now),
    )
    db.execute(
        "INSERT INTO runs (id, project_id, mode, status, phase, phase_label, budget_max, budget_used, "
        "candidate_count, idea_count, team_rounds, model, language, config_json, state_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "p1", "GENERATE", "RUNNING", "P0", "", 100, 0, 2, 1, 1, "test", "zh-CN", "{}", "{}", now, now),
    )
    engine = RunEngine(
        run_id="r1",
        database=db,
        settings_store=store,
        artifacts=ArtifactStore(db),
        events=EventBus(db),
        gateway=None,  # type: ignore[arg-type]
    )
    engine.run = db.query_one("SELECT * FROM runs WHERE id = ?", ("r1",))
    engine.project = db.query_one("SELECT * FROM projects WHERE id = ?", ("p1",))
    engine.state = {"phase": "P0", "recent_messages": [], "user_inputs": []}
    return engine


def test_capability_routing_orders_bound_profiles(tmp_path) -> None:
    settings = AppSettings(
        model_profiles=[
            ModelProfile(id="a", model="m-a", strengths=["academic_writing"]),
            ModelProfile(id="b", model="m-b", strengths=["method_design", "reasoning"]),
            ModelProfile(id="c", model="m-c"),
        ],
        role_models={
            "coherence_editor": ["a", "b", "c"],
            "method_architect": ["a", "b", "c"],
        },
    )
    engine = _engine(tmp_path, settings)
    # 匹配标签更多的模型排前面
    assert engine._profile_ids_for("method_architect") == ["b", "a", "c"]
    # 只有部分模型带匹配标签：a 匹配 academic_writing，b/c 无匹配则按绑定顺序
    assert engine._profile_ids_for("coherence_editor") == ["a", "b", "c"]
    # Legacy temporary failures do not permanently remove a configured model.
    engine.state["degraded_profiles"] = ["b"]
    assert engine._profile_ids_for("method_architect") == ["b", "a", "c"]


def test_dispatch_snapshot_tracks_active_speaker_and_saves_history(tmp_path) -> None:
    engine = _engine(tmp_path, AppSettings())
    first = Participant("a", "domain_researcher_1", "领域研究者 A")
    second = Participant("b", "experiment_designer", "实验设计者")
    dispatch = engine._begin_dispatch(
        stage="P2.1", purpose="P2.1 团队讨论", role=second.role,
        action=Action("RESPOND", target=second, issue_id="obj_1", meta={"source_key": "a", "request": True}),
        participants=[first, second], turn=1,
    )

    def snapshot():
        return json.loads(engine._get_run()["state_json"])["dispatch"]

    async def complete(**kwargs):
        assert snapshot()["active"]["status"] == "waiting"
        kwargs["on_delta"]("reasoning", "thinking")
        assert snapshot()["active"]["status"] == "thinking"
        kwargs["on_delta"]("content", "answer")
        assert snapshot()["active"]["status"] == "streaming"
        return ModelResult("answer", 0, 0, 0.0, 0)

    engine.gateway = SimpleNamespace(complete=complete)  # type: ignore[assignment]
    assert snapshot()["active"]["status"] == "scheduled"
    asyncio.run(engine._call_raw(role=second.role, purpose=dispatch["purpose"], messages=[], temperature=0.1, max_tokens=100))
    assert snapshot()["active"]["status"] == "processing"
    engine._finish_dispatch(message_id=17, outcome="解决 1 条异议")
    saved = snapshot()
    assert saved["active"] is None
    assert saved["history"] == [saved["last"]]
    assert saved["last"]["source_key"] == "a"
    assert saved["last"]["target_key"] == "b"
    assert saved["last"]["message_id"] == 17


def test_role_calls_use_the_bound_models_connection_and_context(tmp_path) -> None:
    engine = _engine(tmp_path, AppSettings(
        model_profiles=[
            ModelProfile(id="lead", model="same-model-id", base_url="https://lead.example/v1", api_key="lead-key"),
            ModelProfile(id="writer", model="same-model-id", base_url="https://writer.example/v1", api_key="writer-key", context_window_tokens=8000, max_input_tokens=4000),
        ],
        role_models={"pi": ["lead"], "writer": ["writer"]},
        context_compression_enabled=False,
    ))
    gateway = ModelGateway(engine.db)
    engine.gateway = gateway

    def provider(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://writer.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer writer-key"
        payload = json.loads(request.content)
        estimated = estimate_messages(payload["messages"])
        assert estimated <= 4000
        assert payload["max_tokens"] + estimated <= 8000
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    async def run() -> None:
        gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        try:
            result = await engine._call_raw(
                role="writer", purpose="test writer", temperature=0.1, max_tokens=18000,
                messages=[{"role": "user", "content": "Detailed research discussion. " * 3000}],
            )
            assert result.content == "answer"
        finally:
            await gateway.close()

    asyncio.run(run())


def test_cost_cap_pauses_the_run(tmp_path) -> None:
    settings = AppSettings(max_run_cost_usd=0.5)
    engine = _engine(tmp_path, settings)
    engine.db.execute(
        "INSERT INTO model_calls (run_id, role, purpose, model, cost_usd, status, created_at) "
        "VALUES ('r1', 'pi', 'test', 'm', 0.75, 'ok', ?)",
        (utc_now(),),
    )
    from app.services.model_gateway import BudgetExhausted

    with pytest.raises(BudgetExhausted) as excinfo:
        engine._guard()
    assert "$0.7500" in str(excinfo.value)
    assert engine.state["cost_used"] == pytest.approx(0.75)

    # 上限内不触发
    engine.state.pop("cost_used")
    engine.db.execute("DELETE FROM model_calls")
    engine.db.execute(
        "INSERT INTO model_calls (run_id, role, purpose, model, cost_usd, status, created_at) "
        "VALUES ('r1', 'pi', 'test', 'm', 0.1, 'ok', ?)",
        (utc_now(),),
    )
    engine._guard()

    # 未配置上限（0）时即使费用高也不触发
    sub = tmp_path / "nolimit"
    sub.mkdir()
    engine2 = _engine(sub, AppSettings(max_run_cost_usd=0.0))
    engine2.db.execute(
        "INSERT INTO model_calls (run_id, role, purpose, model, cost_usd, status, created_at) "
        "VALUES ('r1', 'pi', 'test', 'm', 99.0, 'ok', ?)",
        (utc_now(),),
    )
    engine2._guard()
