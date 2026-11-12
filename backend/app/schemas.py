from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from .config import ModelProfile

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    topic: str = Field(min_length=3, max_length=4000)
    venue: str = Field(default="ACL", max_length=120)
    domain: str = Field(default="NLP / LLM", max_length=120)
    constraints: str = Field(default="", max_length=8000)
    language: Literal["zh-CN", "en-US"] = "zh-CN"
    skills: list[str] = Field(default_factory=list)


class RunCreate(BaseModel):
    idea_count: int = Field(default=3, ge=1, le=6)
    candidate_count: int = Field(default=6, ge=2, le=12)
    team_rounds: int = Field(default=2, ge=1)
    max_team_turns: int = Field(default=20, ge=0, le=200)
    max_debate_turns: int = Field(default=15, ge=0, le=100)
    max_calls: int = Field(default=50, ge=8, le=200)
    model: str | None = None
    language: Literal["zh-CN", "en-US"] | None = None
    skills: list[str] | None = None
    writing_skills: list[str] | None = None
    deliverable: Literal["idea_brief", "manuscript"] = "manuscript"
    context_window_tokens: int | None = Field(default=None, ge=8_000, le=2_000_000)
    max_input_tokens: int | None = Field(default=None, ge=4_000, le=2_000_000)


class BudgetUpdate(BaseModel):
    max_calls: int = Field(ge=8, le=300)


class RunInput(BaseModel):
    kind: Literal["GUIDANCE", "CONSTRAINT", "EVIDENCE", "DECISION"] = "GUIDANCE"
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]
    apply_immediately: bool = False


class SettingsUpdate(BaseModel):
    skills_dirs: list[str] | None = None
    default_skills: list[str] | None = None
    writing_skills: list[str] | None = None
    request_timeout_seconds: int | None = Field(default=None, ge=30, le=900)
    max_run_cost_usd: float | None = Field(default=None, ge=0)
    context_compression_enabled: bool | None = None
    context_compression_trigger: float | None = Field(default=None, ge=0.30, le=0.95)
    context_compression_hard: float | None = Field(default=None, ge=0.40, le=0.98)
    model_profiles: list[ModelProfile] | None = None
    role_models: dict[str, list[str]] | None = None
    discussion_models: list[str] | None = Field(default=None, min_length=1)
    tavily_api_key: str | None = Field(default=None, max_length=200)


class FeedbackCreate(BaseModel):
    instructions: str = Field(default="", max_length=12000)
    report_text: str = Field(default="", max_length=200000)
    max_calls: int = Field(default=20, ge=6, le=80)
    model: str | None = None
    language: Literal["zh-CN", "en-US"] | None = None


