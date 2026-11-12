from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT_DIR, AppSettings, ModelProfile, SettingsStore
from .db import Database, db, utc_now
from .schemas import (
    BudgetUpdate,
    FeedbackCreate,
    ProjectCreate,
    RunCreate,
    RunInput,
    SettingsUpdate,
)
from .services.artifacts import ArtifactStore, atomic_write, new_id
from .services.engine import RunManager
from .services.events import EventBus
from .services.live_feed import LiveFeed
from .services.model_gateway import ModelGateway, model_request, response_events
from .services.run_budget import budget_preview
from .services.skills import SkillRegistry


settings_store = SettingsStore()
artifacts = ArtifactStore(db)
event_bus = EventBus(db)
live_feed = LiveFeed()
model_gateway = ModelGateway(db)
run_manager = RunManager(db, settings_store, artifacts, event_bus, model_gateway, live_feed)
ADJUSTABLE_RUN_STATUSES = ("RUNNING", "PAUSED", "BUDGET_EXHAUSTED")


@asynccontextmanager
async def lifespan(_: FastAPI):
    run_manager.recover_interrupted()
    try:
        yield
    finally:
        try:
            await run_manager.shutdown()
        finally:
            await model_gateway.close()


app = FastAPI(title="Research Council", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def project_view(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["skills"] = json.loads(payload.pop("skills_json", "[]") or "[]")
    payload["archived"] = bool(payload.get("archived_at"))
    payload["idea_count_actual"] = db.query_one(
        "SELECT COUNT(*) AS count FROM ideas WHERE project_id = ?", (payload["id"],)
    )["count"]
    latest = db.query_one(
        "SELECT id, status, phase, phase_label, created_at FROM runs "
        "WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
        (payload["id"],),
    )
    payload["latest_run"] = latest
    return payload


def run_view(row: dict[str, Any], include_state: bool = False) -> dict[str, Any]:
    payload = dict(row)
    payload["config"] = json.loads(payload.pop("config_json", "{}") or "{}")
    if include_state:
        payload["state"] = json.loads(payload.pop("state_json", "{}") or "{}")
        payload["state"].pop("skill_content_snapshot", None)
        payload["state"].pop("panel_progress", None)
    else:
        payload.pop("state_json", None)
    idea_rows = db.query_all(
        "SELECT * FROM ideas WHERE run_id = ? ORDER BY created_at ASC", (payload["id"],)
    )
    if not idea_rows and payload.get("mode") == "REVISE" and payload.get("base_idea_id"):
        base_idea = db.query_one("SELECT * FROM ideas WHERE id = ?", (payload["base_idea_id"],))
        if base_idea:
            idea_rows = [base_idea]
    payload["ideas"] = [idea_view(item, include_document=False) for item in idea_rows]
    payload["call_count"] = db.query_one(
        "SELECT COUNT(*) AS count FROM model_calls WHERE run_id = ?", (payload["id"],)
    )["count"]
    return payload


def idea_view(row: dict[str, Any], include_document: bool = False) -> dict[str, Any]:
    payload = dict(row)
    versions = db.query_all(
        "SELECT id, version, path, revision_path, manifest_json, created_at "
        "FROM idea_versions WHERE idea_id = ? ORDER BY version ASC",
        (payload["id"],),
    )
    payload["versions"] = [
        {
            "id": version["id"],
            "version": version["version"],
            "path": version["path"],
            "revision_path": version["revision_path"],
            "created_at": version["created_at"],
            "manifest": json.loads(version["manifest_json"] or "{}"),
        }
        for version in versions
    ]
    if include_document and versions:
        latest = versions[-1]
        payload["document"] = artifacts.read_relative_text(latest["path"])
        payload["revision_document"] = artifacts.read_relative_text(latest["revision_path"])
        payload["current_path"] = latest["path"]
    return payload


@app.get("/api/health")
def health() -> dict[str, Any]:
    settings = settings_store.load()
    writable, database_error = db.writable_status()
    configured = [profile for profile in settings.model_profiles if profile.api_key and profile.base_url]
    return {
        "status": "ok" if writable else "degraded",
        "model": configured[0].model if configured else "",
        "api_configured": bool(configured),
        "configured_models": len(configured),
        "database": str(db.path),
        "database_writable": writable,
        "database_error": database_error,
        "journal_mode": db.journal_mode,
    }


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    settings = settings_store.load()
    return SettingsStore.public_view(settings)


@app.put("/api/settings")
def update_settings(payload: SettingsUpdate) -> dict[str, Any]:
    try:
        settings = settings_store.save(payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SettingsStore.public_view(settings)


@app.get("/api/skills")
def list_skills() -> dict[str, Any]:
    settings = settings_store.load()
    registry = SkillRegistry(settings.skills_dirs)
    skills = [item.__dict__ for item in registry.list_skills()]
    return {"skills": skills, "directories": settings.skills_dirs}


@app.get("/api/skills/{skill_id}")
def skill_preview(skill_id: str) -> dict[str, Any]:
    settings = settings_store.load()
    registry = SkillRegistry(settings.skills_dirs)
    for item in registry.list_skills():
        if item.id == skill_id:
            path = Path(item.path)
            return {
                **item.__dict__,
                "content": path.read_text(encoding="utf-8", errors="replace")[:20000],
            }
    raise HTTPException(status_code=404, detail="技能不存在")


def _get_model_profile(profile_id: str) -> ModelProfile:
    profile = settings_store.load().profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="模型档案不存在")
    return profile


def _run_model_profile(settings: AppSettings, requested: str | None) -> ModelProfile:
    if requested:
        profile = settings.find_model(requested)
        if profile is None:
            raise HTTPException(status_code=422, detail="指定模型不在模型团队中")
        return profile
    bindings = settings.role_models.get("pi", [])
    if not bindings or (profile := settings.profile(bindings[0])) is None:
        raise HTTPException(status_code=422, detail="请在模型团队中为负责人分配模型")
    return profile


@app.get("/api/settings/models/{profile_id}/key")
def reveal_model_key(profile_id: str, request: Request) -> JSONResponse:
    from ipaddress import ip_address

    try:
        is_local = request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        is_local = False
    if not is_local or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="请从本机地址打开工作台查看密钥")
    profile = _get_model_profile(profile_id)
    return JSONResponse({"api_key": profile.api_key}, headers={"Cache-Control": "no-store"})


@app.post("/api/settings/models/{profile_id}/test")
async def test_model_profile(profile_id: str) -> JSONResponse:
    import httpx

    settings = _get_model_profile(profile_id)
    if not settings.api_key or not settings.base_url:
        return JSONResponse(status_code=400, content={"ok": False, "error": "请先保存该模型的 Base URL 和 API Key"})
    try:
        url, headers, payload = model_request(
            settings, [{"role": "user", "content": "只回复 OK"}],
            max_tokens=128, stream=False, force_reasoning_off=True,
        )
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, headers=headers, json=payload)
        if not response.is_success:
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": f"模型接口返回 HTTP {response.status_code}"},
            )
        async for event in response_events(response, settings.api_format):
            if event.get("error"):
                break
            if any((item.get("delta") or item.get("message") or {}).get("content") for item in event.get("choices") or []):
                return JSONResponse({"ok": True, "model": settings.model})
        raise ValueError("模型响应中没有文本")
    except (httpx.HTTPError, ValueError):
        return JSONResponse(status_code=502, content={"ok": False, "error": "无法连接模型接口或解析响应"})


@app.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    rows = db.query_all(
        "SELECT * FROM projects "
        "ORDER BY (archived_at IS NOT NULL) ASC, updated_at DESC"
    )
    return [project_view(row) for row in rows]


@app.get("/api/overview")
def overview() -> dict[str, Any]:
    project_row = db.query_one(
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(CASE WHEN archived_at IS NULL THEN 1 ELSE 0 END), 0) AS active "
        "FROM projects"
    ) or {"total": 0, "active": 0}
    idea_row = db.query_one("SELECT COUNT(*) AS total FROM ideas") or {"total": 0}
    run_row = db.query_one(
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(CASE WHEN status = 'RUNNING' THEN 1 ELSE 0 END), 0) AS running, "
        "COALESCE(SUM(CASE WHEN status IN ('COMPLETED','COMPLETED_WITH_WARNINGS') THEN 1 ELSE 0 END), 0) AS completed, "
        "COALESCE(SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END), 0) AS failed "
        "FROM runs"
    ) or {"total": 0, "running": 0, "completed": 0, "failed": 0}
    call_row = db.query_one(
        "SELECT COUNT(*) AS total, COALESCE(SUM(cost_usd), 0) AS cost_usd, "
        "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens), 0) AS completion_tokens "
        "FROM model_calls WHERE status IN ('success', 'invalid_output')"
    ) or {"total": 0, "cost_usd": 0, "prompt_tokens": 0, "completion_tokens": 0}
    recent_rows = db.query_all(
        "SELECT * FROM projects WHERE archived_at IS NULL ORDER BY updated_at DESC LIMIT 6"
    )
    stored = settings_store.load()
    return {
        "projects": {
            "total": int(project_row["total"]),
            "active": int(project_row["active"]),
            "archived": int(project_row["total"]) - int(project_row["active"]),
        },
        "ideas": int(idea_row["total"]),
        "runs": {
            "total": int(run_row["total"]),
            "running": int(run_row["running"]),
            "completed": int(run_row["completed"]),
            "failed": int(run_row["failed"]),
        },
        "calls": {
            "total": int(call_row["total"]),
            "cost_usd": round(float(call_row["cost_usd"]), 4),
            "prompt_tokens": int(call_row["prompt_tokens"]),
            "completion_tokens": int(call_row["completion_tokens"]),
        },
        "recent_projects": [project_view(row) for row in recent_rows],
        "model_team": {
            "profiles": [
                {"id": profile.id, "name": profile.name or profile.id, "model": profile.model}
                for profile in stored.model_profiles
            ],
        },
    }


@app.post("/api/projects/{project_id}/archive")
def archive_project(project_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not row:
        raise HTTPException(status_code=404, detail="项目不存在")
    active = db.query_one(
        "SELECT id FROM runs WHERE project_id = ? AND status = 'RUNNING' LIMIT 1",
        (project_id,),
    )
    if active:
        raise HTTPException(
            status_code=409,
            detail="项目仍有进行中的运行，请先暂停或终止后再归档",
        )
    now = utc_now()
    db.execute(
        "UPDATE projects SET archived_at = ?, updated_at = ? WHERE id = ?",
        (now, now, project_id),
    )
    return project_view(db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,)))


@app.post("/api/projects/{project_id}/unarchive")
def unarchive_project(project_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not row:
        raise HTTPException(status_code=404, detail="项目不存在")
    db.execute(
        "UPDATE projects SET archived_at = NULL, updated_at = ? WHERE id = ?",
        (utc_now(), project_id),
    )
    return project_view(db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,)))


@app.post("/api/projects")
def create_project(payload: ProjectCreate) -> dict[str, Any]:
    project_id = new_id("proj")
    now = utc_now()
    db.execute(
        "INSERT INTO projects (id, name, topic, venue, domain, constraints, language, skills_json, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            payload.name,
            payload.topic,
            payload.venue,
            payload.domain,
            payload.constraints,
            payload.language,
            json.dumps(payload.skills, ensure_ascii=False),
            now,
            now,
        ),
    )
    project_path = artifacts.project_dir(project_id)
    project_path.mkdir(parents=True, exist_ok=True)
    atomic_write(
        project_path / "project.yaml",
        f"id: {project_id}\nname: {json.dumps(payload.name, ensure_ascii=False)}\n"
        f"topic: {json.dumps(payload.topic, ensure_ascii=False)}\nvenue: {json.dumps(payload.venue, ensure_ascii=False)}\n"
        f"created_at: {now}\n",
    )
    row = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    return project_view(row)


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not row:
        raise HTTPException(status_code=404, detail="项目不存在")
    result = project_view(row)
    result["runs"] = [
        run_view(item)
        for item in db.query_all(
            "SELECT * FROM runs WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
        )
    ]
    result["ideas"] = [
        idea_view(item)
        for item in db.query_all(
            "SELECT * FROM ideas WHERE project_id = ? ORDER BY created_at ASC", (project_id,)
        )
    ]
    return result


@app.delete("/api/projects/{project_id}")
def archive_project(project_id: str) -> dict[str, bool]:
    row = db.query_one("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not row:
        raise HTTPException(status_code=404, detail="项目不存在")
    # Soft removal from the project list is not implemented yet; keeping the endpoint explicit avoids data loss.
    raise HTTPException(status_code=409, detail="首版不支持删除项目，请直接保留历史记录")


@app.post("/api/runs/budget-preview")
def preview_run_budget(payload: RunCreate) -> dict[str, int | str]:
    return budget_preview(payload, settings_store.load())


@app.post("/api/projects/{project_id}/runs")
async def create_run(project_id: str, payload: RunCreate) -> dict[str, Any]:
    project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    active = db.query_one(
        "SELECT id, status FROM runs WHERE project_id = ? AND status IN ('RUNNING', 'PAUSED') "
        "ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    if active:
        raise HTTPException(status_code=409, detail=f"项目已有未完成运行 {active['id']}，请继续或终止后再启动")
    settings = settings_store.load()
    run_id = new_id("run")
    now = utc_now()
    project_skills = json.loads(project.get("skills_json") or "[]")
    preview = budget_preview(payload, settings)
    if preview["error"]:
        raise HTTPException(status_code=422, detail=preview["error"])
    skills = payload.skills if payload.skills is not None else project_skills or settings.default_skills
    language = payload.language or project["language"]
    profile = _run_model_profile(settings, payload.model)
    config = {
        "skills": skills,
        "writing_skills": payload.writing_skills if payload.writing_skills is not None else settings.writing_skills,
        "deliverable": payload.deliverable,
        "team_rounds": payload.team_rounds,
        "max_team_turns": payload.max_team_turns,
        "max_debate_turns": payload.max_debate_turns,
        "candidate_count": payload.candidate_count,
        "idea_count": payload.idea_count,
        "workflow": "claim_driven_generation_v1",
    }
    if settings.discussion_models is not None:
        config["discussion_models"] = settings.discussion_models
    if payload.model:
        config["pi_model_profile_id"] = profile.id
    if payload.context_window_tokens:
        config["context_window_tokens"] = payload.context_window_tokens
    if payload.max_input_tokens:
        config["max_input_tokens"] = payload.max_input_tokens
    initial_state = {
        "phase": "P0",
        "agenda": "",
        "candidates": [],
        "review_notes": [],
        "selected": [],
        "user_inputs": [],
        "recent_messages": [],
        "idea_next": 0,
        "skills_snapshot": [],
    }
    db.execute(
        "INSERT INTO runs (id, project_id, mode, status, phase, phase_label, budget_max, candidate_count, "
        "idea_count, team_rounds, model, language, config_json, state_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            project_id,
            "GENERATE",
            "RUNNING",
            "P0",
            "项目定义",
            payload.max_calls,
            payload.candidate_count,
            payload.idea_count,
            payload.team_rounds,
            profile.model,
            language,
            json.dumps(config, ensure_ascii=False),
            json.dumps(initial_state, ensure_ascii=False),
            now,
            now,
        ),
    )
    db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
    artifacts.initialize_run(project_id, run_id)
    event_bus.publish(run_id, "run.created", {"run_id": run_id, "project_id": project_id})
    run_manager.start(run_id)
    row = db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    return run_view(row, include_state=False)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not row:
        raise HTTPException(status_code=404, detail="运行不存在")
    project = db.query_one("SELECT * FROM projects WHERE id = ?", (row["project_id"],))
    result = run_view(row, include_state=True)
    result["project"] = project_view(project)
    result["messages"] = db.query_all(
        "SELECT id, role, role_label, message_type, content, structured_json, scope, created_at "
        "FROM messages WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    )
    for message in result["messages"]:
        message["structured"] = json.loads(message.pop("structured_json", "{}") or "{}")
    result["decisions"] = db.query_all(
        "SELECT id, title, content, rationale, created_at FROM decisions WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    )
    result["pending_inputs"] = db.query_all(
        "SELECT id, kind, content, created_at FROM run_inputs WHERE run_id = ? AND consumed = 0 ORDER BY id",
        (run_id,),
    )
    result["budget_changes"] = [
        {"id": item["id"], "created_at": item["created_at"], **json.loads(item["payload_json"])}
        for item in db.query_all(
            "SELECT id, created_at, payload_json FROM events "
            "WHERE run_id = ? AND event_type = 'budget.increased' ORDER BY id",
            (run_id,),
        )
    ]
    result["portfolio_path"] = result.get("state", {}).get("portfolio_path")
    result["portfolio_content"] = artifacts.read_relative_text(result.get("portfolio_path"))
    result["context"] = db.query_one(
        "SELECT id, purpose, estimated_tokens, budget_tokens, ratio, compressed, strategy, "
        "summary_id, actual_prompt_tokens, created_at FROM context_snapshots "
        "WHERE run_id = ? ORDER BY id DESC LIMIT 1",
        (run_id,),
    )
    result["context_stats"] = db.query_one(
        "SELECT COUNT(*) AS snapshots, "
        "SUM(CASE WHEN compressed = 1 THEN 1 ELSE 0 END) AS compressions, "
        "MAX(actual_prompt_tokens) AS peak_prompt_tokens "
        "FROM context_snapshots WHERE run_id = ?",
        (run_id,),
    )
    result["context_summaries"] = db.query_all(
        "SELECT id, scope, covered_start, covered_end, token_count, created_at "
        "FROM run_summaries WHERE run_id = ? ORDER BY id DESC LIMIT 20",
        (run_id,),
    )
    return result


@app.post("/api/runs/{run_id}/pause")
async def pause_run(run_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not row:
        raise HTTPException(status_code=404, detail="运行不存在")
    if row["status"] != "RUNNING":
        raise HTTPException(status_code=409, detail="只有进行中的运行可以暂停")
    run_manager.request_pause(run_id)
    return {"status": "PAUSED", "note": "正在等待当前模型调用返回"}


@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str) -> dict[str, Any]:
    if not await run_manager.resume(run_id):
        raise HTTPException(status_code=409, detail="当前状态不可继续")
    return {"status": "RUNNING"}


@app.post("/api/runs/{run_id}/budget")
async def update_run_budget(run_id: str, payload: BudgetUpdate) -> dict[str, Any]:
    with db.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status, budget_used, budget_max FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="运行不存在")
        if row["status"] not in ADJUSTABLE_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="当前运行状态无法增加预算")
        if payload.max_calls <= max(row["budget_used"], row["budget_max"]):
            raise HTTPException(status_code=409, detail=f"新上限必须大于当前上限 {row['budget_max']}")
        connection.execute(
            "UPDATE runs SET budget_max = ?, updated_at = ? WHERE id = ?",
            (payload.max_calls, utc_now(), run_id),
        )
        event = event_bus.record(connection, run_id, "budget.increased", {
            "previous_max": row["budget_max"], "max": payload.max_calls, "used": row["budget_used"],
        })
    event_bus.broadcast(run_id, event)
    resumed = row["status"] == "BUDGET_EXHAUSTED" and await run_manager.resume(run_id)
    current = db.query_one("SELECT status FROM runs WHERE id = ?", (run_id,))
    return {
        "budget_used": row["budget_used"],
        "budget_max": payload.max_calls,
        "resumed": resumed,
        "status": current["status"] if current else "BUDGET_EXHAUSTED",
    }


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not row:
        raise HTTPException(status_code=404, detail="运行不存在")
    if row["status"] in {"COMPLETED", "COMPLETED_WITH_WARNINGS", "CANCELLED"}:
        raise HTTPException(status_code=409, detail="运行已经结束")
    run_manager.cancel(run_id)
    return {"status": "CANCELLED"}


@app.post("/api/runs/{run_id}/inputs")
async def add_run_input(run_id: str, payload: RunInput) -> dict[str, Any]:
    with db.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="运行不存在")
        if row["status"] not in ADJUSTABLE_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="当前运行无法接收讨论输入")
        input_id = connection.execute(
            "INSERT INTO run_inputs (run_id, kind, content, consumed, created_at) VALUES (?, ?, ?, 0, ?)",
            (run_id, payload.kind, payload.content, utc_now()),
        ).lastrowid
        event = event_bus.record(connection, run_id, "input.queued", {"id": input_id, "kind": payload.kind})
    event_bus.broadcast(run_id, event)
    if payload.apply_immediately and row["status"] == "RUNNING":
        run_manager.request_pause(run_id)
        return {
            "id": input_id,
            "status": "queued",
            "note": "输入已保存并请求暂停；继续运行后会在下一安全点纳入议程。",
        }
    return {"id": input_id, "status": "queued"}


@app.get("/api/runs/{run_id}/events")
async def run_events(request: Request, run_id: str, after: int = Query(default=0)) -> StreamingResponse:
    if not db.query_one("SELECT id FROM runs WHERE id = ?", (run_id,)):
        raise HTTPException(status_code=404, detail="运行不存在")
    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))

    async def stream():
        queue = await event_bus.subscribe(run_id)
        try:
            for event in event_bus.replay(run_id, after):
                yield _sse(event.id, event.event_type, event.payload)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield _sse(event.id, event.event_type, event.payload)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            await event_bus.unsubscribe(run_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event_id: int, event_type: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n"


@app.get("/api/runs/{run_id}/live")
async def run_live_stream(run_id: str) -> StreamingResponse:
    """流式转发当前进行中模型调用的思考与正文增量；不落库，仅内存转发。"""
    if not db.query_one("SELECT id FROM runs WHERE id = ?", (run_id,)):
        raise HTTPException(status_code=404, detail="运行不存在")

    async def stream():
        queue = live_feed.subscribe(run_id)
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    data = json.dumps(payload, ensure_ascii=False)
                    yield f"event: {payload.get('type', 'live.delta')}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            live_feed.unsubscribe(run_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/runs/{run_id}/calls")
def run_calls(run_id: str) -> dict[str, Any]:
    calls = db.query_all(
        "SELECT id, role, purpose, model, prompt_tokens, completion_tokens, cost_usd, latency_ms, "
        "status, error, created_at FROM model_calls WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    )
    return {
        "calls": calls,
        "totals": {
            "calls": len(calls),
            "prompt_tokens": sum(item["prompt_tokens"] or 0 for item in calls),
            "completion_tokens": sum(item["completion_tokens"] or 0 for item in calls),
            "cost_usd": round(sum(item["cost_usd"] or 0 for item in calls), 6),
        },
    }


@app.get("/api/ideas/{idea_id}")
def get_idea(idea_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM ideas WHERE id = ?", (idea_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Idea 不存在")
    payload = idea_view(row, include_document=True)
    payload["portfolio"] = ""
    run_state = db.query_one("SELECT state_json FROM runs WHERE id = ?", (row["run_id"],))
    if run_state:
        portfolio_path = json.loads(run_state["state_json"] or "{}").get("portfolio_path")
        payload["portfolio"] = artifacts.read_relative_text(portfolio_path)
    return payload


@app.get("/api/ideas/{idea_id}/versions/{version}")
def get_idea_version(idea_id: str, version: int) -> dict[str, Any]:
    row = db.query_one(
        "SELECT * FROM idea_versions WHERE idea_id = ? AND version = ?", (idea_id, version)
    )
    if not row:
        raise HTTPException(status_code=404, detail="版本不存在")
    return {
        "version": version,
        "path": row["path"],
        "document": artifacts.read_relative_text(row["path"]),
        "revision_path": row["revision_path"],
        "revision_document": artifacts.read_relative_text(row["revision_path"]),
        "manifest": json.loads(row["manifest_json"] or "{}"),
    }


@app.post("/api/ideas/{idea_id}/feedback")
async def create_feedback(
    idea_id: str,
    instructions: str = Form(default=""),
    report_text: str = Form(default=""),
    max_calls: int = Form(default=20),
    model: str = Form(default=""),
    language: str = Form(default=""),
    base_version: int | None = Form(default=None, ge=1),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    idea = db.query_one("SELECT * FROM ideas WHERE id = ?", (idea_id,))
    if not idea:
        raise HTTPException(status_code=404, detail="Idea 不存在")
    version_row = db.query_one(
        "SELECT version FROM idea_versions WHERE idea_id = ? AND (? IS NULL OR version = ?) "
        "ORDER BY version DESC LIMIT 1",
        (idea_id, base_version, base_version),
    )
    if not version_row:
        raise HTTPException(status_code=409, detail="所选 Idea 的基础版本不可用")
    source_name = file.filename if file else None
    source_bytes = await file.read() if file else None
    combined = report_text.strip()
    if source_bytes and source_name:
        suffix = Path(source_name).suffix.lower()
        if suffix == ".pdf":
            try:
                from io import BytesIO

                from pypdf import PdfReader

                reader = PdfReader(BytesIO(source_bytes))
                extracted = "\n\n".join(page.extract_text() or "" for page in reader.pages)
                combined = (combined + "\n\n" + extracted).strip()
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"PDF 解析失败：{exc}") from exc
        elif suffix in {".md", ".txt", ".json", ".csv", ".yaml", ".yml"}:
            try:
                decoded = source_bytes.decode("utf-8")
            except UnicodeDecodeError:
                decoded = source_bytes.decode("gb18030", errors="replace")
            combined = (combined + "\n\n" + decoded).strip()
        else:
            raise HTTPException(status_code=400, detail="反馈文件暂支持 PDF、Markdown、文本、JSON、CSV、YAML")
    instructions = instructions.strip()
    if not combined and not instructions:
        raise HTTPException(status_code=400, detail="请填写修订要求、补充实验观察，或上传报告")
    settings = settings_store.load()
    profile = _run_model_profile(settings, model)
    feedback_id = new_id("fb")
    source_path = artifacts.save_feedback_source(
        idea["project_id"], feedback_id, combined, source_name, source_bytes
    )
    db.execute(
        "INSERT INTO feedback (id, project_id, idea_id, base_version, source_path, report_text, "
        "instructions, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            feedback_id,
            idea["project_id"],
            idea_id,
            version_row["version"],
            source_path,
            combined,
            instructions,
            utc_now(),
        ),
    )
    run_id = new_id("run")
    now = utc_now()
    project = db.query_one("SELECT * FROM projects WHERE id = ?", (idea["project_id"],))
    run_language = language or (project.get("language") if project else None) or "zh-CN"
    initial_state = {
        "phase": "R0",
        "agenda": "根据用户补充的信息和要求修订现有 Idea",
        "user_inputs": [],
        "recent_messages": [],
        "skills_snapshot": [],
    }
    db.execute(
        "INSERT INTO runs (id, project_id, mode, status, phase, phase_label, budget_max, candidate_count, "
        "idea_count, team_rounds, model, language, config_json, state_json, base_idea_id, feedback_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            idea["project_id"],
            "REVISE",
            "RUNNING",
            "R0",
            "用户反馈分析",
            max(6, min(max_calls, 80)),
            1,
            1,
            1,
            profile.model,
            run_language,
            json.dumps(
                {
                    "skills": json.loads(project.get("skills_json") or "[]") or settings.default_skills,
                    **({"pi_model_profile_id": profile.id} if model else {}),
                },
                ensure_ascii=False,
            ),
            json.dumps(initial_state, ensure_ascii=False),
            idea_id,
            feedback_id,
            now,
            now,
        ),
    )
    db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, idea["project_id"]))
    artifacts.initialize_run(idea["project_id"], run_id)
    event_bus.publish(run_id, "run.created", {"run_id": run_id, "mode": "REVISE"})
    run_manager.start(run_id)
    row = db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    return {"feedback_id": feedback_id, "run": run_view(row)}


@app.get("/api/projects/{project_id}/feedback")
def list_feedback(project_id: str) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT f.id, f.idea_id, f.base_version, f.instructions, f.created_at, i.title "
        "FROM feedback f JOIN ideas i ON i.id = f.idea_id WHERE f.project_id = ? ORDER BY f.created_at DESC",
        (project_id,),
    )


frontend_dist = ROOT_DIR / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        candidate = frontend_dist / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(frontend_dist / "index.html")
