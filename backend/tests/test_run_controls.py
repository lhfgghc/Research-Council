from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import main
from app.config import ModelProfile, SettingsStore
from app.db import Database, utc_now
from app.services import artifacts as artifacts_module
from app.services.artifacts import ArtifactStore
from app.services.engine import RunEngine, RunManager
from app.services.events import EventBus
from app.services.model_gateway import ModelGateway


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    database = Database(tmp_path / "app.db")
    store = SettingsStore(tmp_path / "settings.json")
    store.save({
        "model_profiles": [
            ModelProfile(id="test", model="test", base_url="https://model.example/v1", api_key="mock").model_dump()
        ],
        "role_models": {"pi": ["test"]},
        "skills_dirs": [],
    })
    monkeypatch.setattr(artifacts_module, "PROJECTS_DIR", tmp_path / "projects")
    artifacts = ArtifactStore(database)
    events = EventBus(database)
    gateway = ModelGateway(database)
    manager = RunManager(database, store, artifacts, events, gateway)
    for name, value in (
        ("db", database), ("artifacts", artifacts), ("event_bus", events), ("run_manager", manager),
        ("settings_store", store),
    ):
        monkeypatch.setattr(main, name, value)
    now = utc_now()
    database.execute(
        "INSERT INTO projects (id, name, topic, venue, created_at, updated_at) VALUES ('p1', 'P', 'Topic', 'ACL', ?, ?)",
        (now, now),
    )
    database.execute(
        "INSERT INTO runs (id, project_id, mode, status, phase, phase_label, budget_max, budget_used, "
        "candidate_count, idea_count, team_rounds, model, language, config_json, state_json, created_at, updated_at) "
        "VALUES ('r1', 'p1', 'GENERATE', 'RUNNING', 'P2', '', 8, 7, 2, 1, 1, 'test', 'zh-CN', '{}', '{}', ?, ?)",
        (now, now),
    )
    return database, manager, gateway


async def model_turn(engine: RunEngine) -> None:
    await engine._call_raw(
        role="pi", purpose="P2 discussion", temperature=0.1, max_tokens=100,
        messages=[{"role": "user", "content": "Discuss the proposal."}],
    )


@pytest.mark.asyncio
async def test_input_during_model_call_and_budget_increase_reach_next_call(runtime, monkeypatch):
    database, manager, gateway = runtime
    first_started, second_started = asyncio.Event(), asyncio.Event()
    first_reply, second_reply = asyncio.Event(), asyncio.Event()
    prompts = []

    async def drive(engine):
        await model_turn(engine)
        await model_turn(engine)
        engine._complete_generation()

    async def provider(request):
        prompts.append(json.loads(request.content)["messages"][-1]["content"])
        started, reply = (first_started, first_reply) if len(prompts) == 1 else (second_started, second_reply)
        started.set()
        await reply.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    monkeypatch.setattr(RunEngine, "_drive", drive)
    gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    manager.start("r1")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            await asyncio.wait_for(first_started.wait(), timeout=5)
            blank = await client.post("/api/runs/r1/inputs", json={"content": "  \n "})
            assert blank.status_code == 422
            note = "Compare a same-budget retrieval baseline."
            queued = await client.post("/api/runs/r1/inputs", json={"kind": "CONSTRAINT", "content": f" {note} "})
            assert queued.status_code == 200
            pending = (await client.get("/api/runs/r1")).json()["pending_inputs"]
            assert [(item["id"], item["content"]) for item in pending] == [(queued.json()["id"], note)]

            increase = await client.post("/api/runs/r1/budget", json={"max_calls": 12})
            assert increase.json() == {"budget_used": 8, "budget_max": 12, "status": "RUNNING", "resumed": False}
            stale = await client.post("/api/runs/r1/budget", json={"max_calls": 11})
            assert stale.status_code == 409

            first_reply.set()
            await asyncio.wait_for(second_started.wait(), timeout=5)
            assert note not in prompts[0]
            assert prompts[1].count(note) == 1 and "[CONSTRAINT]" in prompts[1]
            during = (await client.get("/api/runs/r1")).json()
            assert during["pending_inputs"] == []
            assert [item["structured"]["input_id"] for item in during["messages"] if item["role"] == "user"] == [
                queued.json()["id"]
            ]
            assert len(during["budget_changes"]) == 1
            change = during["budget_changes"][0]
            assert (change["previous_max"], change["max"], change["used"]) == (8, 12, 8)

            late = await client.post("/api/runs/r1/inputs", json={"content": "Sent after the final call started."})
            assert late.status_code == 200
            second_reply.set()
            await asyncio.wait_for(manager.tasks["r1"], timeout=5)
            finished = (await client.get("/api/runs/r1")).json()
            assert (finished["status"], finished["phase"], finished["error"]) == ("COMPLETED", "DONE", None)
            assert [item["id"] for item in finished["pending_inputs"]] == [late.json()["id"]]
            assert database.query_one("SELECT budget_used FROM runs WHERE id = 'r1'")["budget_used"] == 9
            assert (await client.post("/api/runs/r1/inputs", json={"content": note})).status_code == 409
            assert (await client.post("/api/runs/r1/budget", json={"max_calls": 20})).status_code == 409
    finally:
        await manager.shutdown()
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["PAUSED", "BUDGET_EXHAUSTED"])
async def test_budget_extension_only_auto_resumes_exhausted_run(runtime, monkeypatch, initial_status):
    database, manager, gateway = runtime
    seeded_status = "PAUSED" if initial_status == "PAUSED" else "RUNNING"
    database.execute("UPDATE runs SET status = ?, budget_used = 8 WHERE id = 'r1'", (seeded_status,))
    prompts = []

    async def drive(engine):
        await model_turn(engine)
        engine._complete_revision(2)

    def provider(request):
        prompts.append(json.loads(request.content)["messages"][-1]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    monkeypatch.setattr(RunEngine, "_drive", drive)
    gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            queued = await client.post("/api/runs/r1/inputs", json={"content": "Keep the original research question."})
            assert queued.status_code == 200
            if initial_status == "BUDGET_EXHAUSTED":
                manager.start("r1")
                await asyncio.wait_for(manager.tasks["r1"], timeout=5)
                exhausted = (await client.get("/api/runs/r1")).json()
                assert exhausted["status"] == "BUDGET_EXHAUSTED"
                assert [item["id"] for item in exhausted["pending_inputs"]] == [queued.json()["id"]]
            update = (await client.post("/api/runs/r1/budget", json={"max_calls": 10})).json()
            if initial_status == "PAUSED":
                assert update["status"] == "PAUSED" and not update["resumed"]
                assert "r1" not in manager.tasks
                assert (await client.post("/api/runs/r1/resume")).status_code == 200
            else:
                assert update["status"] == "RUNNING" and update["resumed"]
            await asyncio.wait_for(manager.tasks["r1"], timeout=5)
            assert len(prompts) == 1 and "Keep the original research question." in prompts[0]
            finished = (await client.get("/api/runs/r1")).json()
            assert (finished["status"], finished["pending_inputs"], finished["error"]) == ("COMPLETED", [], None)
            paths = (await client.get("/openapi.json")).json()["paths"]
            assert "/api/ideas/{idea_id}/literature/check" not in paths
    finally:
        await manager.shutdown()
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("report", ["", "The held-out tasks showed no gain."])
async def test_completed_idea_accepts_follow_up_and_preserves_its_base_version(runtime, monkeypatch, report):
    database, manager, gateway = runtime
    database.execute("UPDATE runs SET status = 'COMPLETED', phase = 'DONE' WHERE id = 'r1'")
    idea = manager.artifacts.create_idea(project_id="p1", run_id="r1", title="Idea", summary="Summary", index=0)
    original = "# Base proposal"
    manager.artifacts.save_version(
        idea=idea, project_id="p1", document=original, manifest={"deliverable": "idea_brief"},
    )

    async def drive(engine):
        engine._load_revision_base()
        assert engine.state["revision_feedback"] == report
        assert engine.state["revision_instructions"] == "Strengthen the baseline."
        assert engine.state["revision_base_document"].strip() == original
        engine.state["revision_document"] = "# Revised proposal"
        engine._save_revision()

    monkeypatch.setattr(RunEngine, "_drive", drive)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            url = f"/api/ideas/{idea['id']}/feedback"
            assert (await client.post(url, data={"instructions": " "})).status_code == 400
            assert (await client.post(url, data={"base_version": 9, "instructions": "Edit."})).status_code == 409
            result = await client.post(url, data={
                "base_version": 1, "instructions": "Strengthen the baseline.", "report_text": report,
            })
            assert result.status_code == 200
            revision = result.json()["run"]
            assert revision["mode"] == "REVISE" and revision["id"] != "r1"
            await asyncio.wait_for(manager.tasks[revision["id"]], timeout=5)

            finished = (await client.get(f"/api/runs/{revision['id']}")).json()
            assert finished["status"] == "COMPLETED"
            opening = finished["messages"][0]
            assert opening["role"] == "user" and opening["structured"]["base_version"] == 1
            assert "Strengthen the baseline." in opening["content"]
            if report:
                assert report in opening["content"]
            assert database.query_one("SELECT status FROM runs WHERE id = 'r1'")["status"] == "COMPLETED"
            before = (await client.get(f"/api/ideas/{idea['id']}/versions/1")).json()
            after = (await client.get(f"/api/ideas/{idea['id']}/versions/2")).json()
            assert before["document"].strip() == original
            assert after["document"].strip() == "# Revised proposal"
    finally:
        await manager.shutdown()
        await gateway.close()
