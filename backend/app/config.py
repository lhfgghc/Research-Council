from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any, Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from .services.skills import DEFAULT_DISCUSSION_SKILLS, DEFAULT_WRITING_SKILLS

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
if not DATA_DIR.is_absolute():
    DATA_DIR = ROOT_DIR / DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

PROJECTS_DIR = DATA_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "app.db"
SETTINGS_PATH = DATA_DIR / "settings.json"


def _default_skill_dirs() -> list[str]:
    configured = os.getenv("CCFA_SKILLS_DIR", "").strip()
    if configured:
        return [part.strip() for part in configured.replace(";", ",").split(",") if part.strip()]
    local_skills = ROOT_DIR / "skills"
    return [str(local_skills)] if local_skills.is_dir() else []


class ModelProfile(BaseModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(default="", max_length=80)
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(default="", max_length=500)
    api_format: Literal["openai", "anthropic"] = "openai"
    api_key: str = Field(default="", repr=False)
    reasoning_effort: str = Field(default="low", pattern="^(off|low|medium|high|xhigh|max)$")
    context_window_tokens: int = Field(default=1_000_000, ge=8_000, le=2_000_000)
    max_input_tokens: int | None = Field(default=None, ge=4_000, le=2_000_000)
    input_price_per_million: float = Field(default=0.15, ge=0)
    output_price_per_million: float = Field(default=0.60, ge=0)
    strengths: list[str] = Field(default_factory=list)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value:
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username is not None or url.query or url.fragment:
                raise ValueError("Base URL 必须是 HTTP(S) 接口地址，不能包含凭据、查询参数或片段")
            _ = url.port  # Validate an explicitly supplied port.
        return value


MODEL_ROLES = (
    "pi", "domain_researcher_1", "domain_researcher_2", "domain_researcher_3",
    "method_architect", "experiment_designer", "literature_researcher", "writer", "coherence_editor",
)

LEGACY_BASE_URL = "https://api.commandcode.ai/provider/v1"


def _default_model_profiles() -> list[ModelProfile]:
    return [
        ModelProfile(id="deepseek", name="DeepSeek V4.1 Flash", model="deepseek/deepseek-v4.1-flash"),
        ModelProfile(id="mimo", name="MiMo V2.6 Flash", model="xiaomi/mimo-v2.6-flash"),
        ModelProfile(id="glm", name="GLM-5.3 Flash", model="z-ai/glm-5.3-flash"),
    ]


def _default_role_models() -> dict[str, list[str]]:
    return {
        **{role: ["deepseek"] for role in MODEL_ROLES},
        "method_architect": ["deepseek", "mimo", "glm"],
        "experiment_designer": ["deepseek", "mimo", "glm"],
    }


class ContextPolicy(BaseModel):
    context_compression_enabled: bool = True
    context_compression_trigger: float = Field(default=0.70, ge=0.30, le=0.95)
    context_compression_hard: float = Field(default=0.85, ge=0.40, le=0.98)


class ModelCallSettings(ModelProfile, ContextPolicy):
    request_timeout_seconds: int = 300


class AppSettings(ContextPolicy):
    skills_dirs: list[str] = Field(default_factory=_default_skill_dirs)
    default_skills: list[str] = Field(
        default_factory=lambda: list(DEFAULT_DISCUSSION_SKILLS)
    )
    writing_skills: list[str] = Field(
        default_factory=lambda: list(DEFAULT_WRITING_SKILLS)
    )
    request_timeout_seconds: int = 300
    model_profiles: list[ModelProfile] = Field(default_factory=_default_model_profiles)
    role_models: dict[str, list[str]] = Field(default_factory=_default_role_models)
    discussion_models: list[str] | None = None
    max_run_cost_usd: float = Field(default=0.0, ge=0)
    tavily_api_key: str = Field(default="", repr=False)

    def profile(self, profile_id: str) -> ModelProfile | None:
        return next((profile for profile in self.model_profiles if profile.id == profile_id), None)

    def find_model(self, value: str) -> ModelProfile | None:
        return self.profile(value) or next((profile for profile in self.model_profiles if profile.model == value), None)

    def call_settings(self, profile: ModelProfile) -> ModelCallSettings:
        return ModelCallSettings(
            **profile.model_dump(),
            request_timeout_seconds=self.request_timeout_seconds,
            context_compression_enabled=self.context_compression_enabled,
            context_compression_trigger=self.context_compression_trigger,
            context_compression_hard=self.context_compression_hard,
        )


def _normalize_roles(settings: AppSettings, unassigned_id: str = "") -> AppSettings:
    ids = {profile.id for profile in settings.model_profiles}
    bindings = {
        role: list(dict.fromkeys(profile_id for profile_id in profiles if profile_id in ids))
        for role, profiles in settings.role_models.items()
    }
    if unassigned_id:
        for role in MODEL_ROLES:
            if not bindings.get(role):
                bindings[role] = [unassigned_id]
    discussion_models = settings.discussion_models
    if discussion_models is not None:
        discussion_models = list(dict.fromkeys(profile_id for profile_id in discussion_models if profile_id in ids))
        bindings = {role: profiles[:1] for role, profiles in bindings.items()}
    return settings.model_copy(update={"role_models": bindings, "discussion_models": discussion_models})


class SettingsStore:
    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self.path = path
        self._write_lock = Lock()

    def load(self) -> AppSettings:
        values: dict[str, Any] = {}
        if self.path.exists():
            try:
                stored = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    values = stored
            except (json.JSONDecodeError, OSError):
                pass
        if not values:
            return AppSettings(model_profiles=[], role_models={})
        if values.get("model_settings_version") == 2:
            return _normalize_roles(AppSettings(**values))
        return self._migrate_legacy(values)

    @staticmethod
    def _migrate_legacy(values: dict[str, Any]) -> AppSettings:
        primary = ModelProfile(
            id="legacy_primary",
            model=values.get("model") or os.getenv("COMMAND_CODE_MODEL") or "deepseek/deepseek-v4.1-flash",
            base_url=values.get("base_url") or os.getenv("COMMAND_CODE_BASE_URL") or LEGACY_BASE_URL,
            api_key=values.get("api_key") or os.getenv("COMMAND_CODE_API_KEY", ""),
            reasoning_effort=values.get("reasoning_effort") or os.getenv("COMMAND_CODE_REASONING_EFFORT") or "low",
            **{field: values[field] for field in (
                "context_window_tokens", "max_input_tokens", "input_price_per_million", "output_price_per_million",
            ) if values.get(field) is not None},
        )
        settings = AppSettings(**values)
        profiles = []
        primary_id = ""
        for profile in settings.model_profiles:
            # A custom endpoint never receives a credential from a different host.
            base_url = profile.base_url or primary.base_url
            key = profile.api_key or (primary.api_key if base_url == primary.base_url else "")
            migrated = profile.model_copy(update={"base_url": base_url, "api_key": key})
            profiles.append(migrated)
            if not primary_id and (migrated.model, base_url, key) == (primary.model, primary.base_url, primary.api_key):
                primary_id = profile.id
        if not primary_id:
            primary_id = primary.id
            while any(profile.id == primary_id for profile in profiles):
                primary_id += "_"
            profiles.insert(0, primary.model_copy(update={"id": primary_id, "name": primary.model}))
        return _normalize_roles(settings.model_copy(update={"model_profiles": profiles}), primary_id)

    def save(self, updates: dict[str, Any]) -> AppSettings:
        with self._write_lock:
            return self._save(updates)

    def _save(self, updates: dict[str, Any]) -> AppSettings:
        current = self.load()
        merged = current.model_dump()
        for key, value in updates.items():
            if value is None:
                continue
            if key == "tavily_api_key" and value == "":
                continue
            if key == "model_profiles" and isinstance(value, list):
                existing = {item.get("id"): item for item in merged.get("model_profiles", [])}
                profiles: list[dict[str, Any]] = []
                for item in value:
                    profile = dict(item)
                    previous = existing.get(profile.get("id"), {})
                    if "api_key" not in profile and str(profile.get("base_url", "")).strip().rstrip("/") == previous.get("base_url"):
                        profile["api_key"] = previous.get("api_key", "")
                    profiles.append(profile)
                merged[key] = profiles
                continue
            merged[key] = value
        settings = _normalize_roles(AppSettings(**merged))
        ids = [profile.id for profile in settings.model_profiles]
        if len(set(ids)) != len(ids):
            raise ValueError("模型档案 ID 必须唯一")
        if settings.discussion_models == []:
            raise ValueError("请至少选择一个参与讨论的模型")
        payload = {**settings.model_dump(), "model_settings_version": 2}
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(payload, file, ensure_ascii=False, indent=2)
            temporary.chmod(0o600)
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return settings

    @staticmethod
    def public_view(settings: AppSettings) -> dict[str, Any]:
        payload = settings.model_dump()
        payload["tavily_api_key"] = ""
        payload["has_tavily_key"] = bool(settings.tavily_api_key)
        payload["model_profiles"] = [
            {
                **{key: value for key, value in profile.items() if key != "api_key"},
                "has_api_key": bool(profile.get("api_key")),
            }
            for profile in payload.get("model_profiles", [])
        ]
        return payload
