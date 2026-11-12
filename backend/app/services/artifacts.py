from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from ..config import DATA_DIR, PROJECTS_DIR
from ..db import Database, utc_now


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def safe_slug(title: str) -> str:
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    return (ascii_part[:48] or "idea").strip("-")


class ArtifactStore:
    def __init__(self, database: Database) -> None:
        self.db = database

    def project_dir(self, project_id: str) -> Path:
        return PROJECTS_DIR / project_id

    def run_dir(self, project_id: str, run_id: str) -> Path:
        return self.project_dir(project_id) / "runs" / run_id

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(DATA_DIR)).replace("\\", "/")
        except ValueError:
            return str(path)

    def resolve(self, relative_path: str) -> Path:
        path = Path(relative_path)
        return path if path.is_absolute() else DATA_DIR / path

    def initialize_run(self, project_id: str, run_id: str) -> None:
        run_dir = self.run_dir(project_id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "messages.jsonl").touch(exist_ok=True)
        (run_dir / "decisions.jsonl").touch(exist_ok=True)

    def append_message(self, project_id: str, run_id: str, payload: dict[str, Any]) -> None:
        append_jsonl(self.run_dir(project_id, run_id) / "messages.jsonl", payload)

    def append_decision(self, project_id: str, run_id: str, payload: dict[str, Any]) -> None:
        append_jsonl(self.run_dir(project_id, run_id) / "decisions.jsonl", payload)

    def create_idea(
        self,
        *,
        project_id: str,
        run_id: str,
        title: str,
        summary: str,
        index: int,
    ) -> dict[str, Any]:
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id, slug FROM ideas WHERE run_id = ? AND creation_index = ?", (run_id, index)
            ).fetchone()
            if existing:
                idea_id, slug = existing["id"], existing["slug"]
                idea_dir = self.project_dir(project_id) / "ideas" / slug
            else:
                idea_id = new_id("idea")
                slug = f"idea-{index + 1:02d}-{safe_slug(title)}-{idea_id}"
                now = utc_now()
                connection.execute(
                    "INSERT INTO ideas (id, project_id, run_id, title, slug, summary, status, "
                    "current_version, created_at, updated_at, creation_index) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'DRAFT', 0, ?, ?, ?)",
                    (idea_id, project_id, run_id, title, slug, summary, now, now, index),
                )
                idea_dir = self.project_dir(project_id) / "ideas" / slug
                atomic_write(
                    idea_dir / "idea.yaml",
                    f"id: {idea_id}\ntitle: {json.dumps(title, ensure_ascii=False)}\nslug: {slug}\n"
                    f"run_id: {run_id}\ncreated_at: {now}\n",
                )
        return {"id": idea_id, "slug": slug, "dir": str(idea_dir), "index": index}

    def save_version(
        self,
        *,
        idea: dict[str, Any],
        project_id: str,
        document: str,
        manifest: dict[str, Any],
        revision_markdown: str | None = None,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id, version, path, revision_path FROM idea_versions "
                "WHERE idea_id = ? AND operation_key = ?", (idea["id"], operation_key),
            ).fetchone()
            if existing:
                return {
                    "version_id": existing["id"], "version": existing["version"],
                    "path": existing["path"], "revision_path": existing["revision_path"],
                    "absolute_path": str(self.resolve(existing["path"])),
                }
            current = connection.execute(
                "SELECT slug FROM ideas WHERE id = ? AND project_id = ?", (idea["id"], project_id)
            ).fetchone()
            if current is None:
                raise ValueError(f"Idea 不存在：{idea['id']}")
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM idea_versions WHERE idea_id = ?", (idea["id"],)
            ).fetchone()[0]
            # New versions of legacy Ideas also get a private directory.
            slug = current["slug"]
            if not slug.endswith(f"-{idea['id']}"):
                slug = f"{slug}-{idea['id']}"
            version_dir = self.project_dir(project_id) / "ideas" / slug / f"v{version:03d}"
            idea_path = version_dir / "idea.md"
            path = self.relative(idea_path)
            atomic_write(idea_path, document.rstrip() + "\n")
            revision_path = None
            if revision_markdown:
                revision_file = version_dir / "revision.md"
                atomic_write(revision_file, revision_markdown.rstrip() + "\n")
                revision_path = self.relative(revision_file)
            now = utc_now()
            manifest_payload = {**manifest, "idea_id": idea["id"], "version": version, "saved_at": now}
            atomic_write(version_dir / "manifest.json", json.dumps(manifest_payload, ensure_ascii=False, indent=2))
            version_id = new_id("ver")
            connection.execute(
                "INSERT INTO idea_versions (id, idea_id, version, path, revision_path, manifest_json, "
                "created_at, operation_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (version_id, idea["id"], version, path, revision_path,
                 json.dumps(manifest_payload, ensure_ascii=False), now, operation_key),
            )
            connection.execute(
                "UPDATE ideas SET current_version = ?, status = ?, updated_at = ? WHERE id = ?",
                (version, manifest.get("status", "DRAFT"), now, idea["id"]),
            )
        return {
            "version_id": version_id,
            "version": version,
            "path": path,
            "revision_path": revision_path,
            "absolute_path": str(idea_path),
        }

    def save_portfolio(self, project_id: str, run_id: str, content: str) -> str:
        path = self.project_dir(project_id) / "runs" / run_id / "portfolio.md"
        atomic_write(path, content.rstrip() + "\n")
        return self.relative(path)

    def save_feedback_source(
        self,
        project_id: str,
        feedback_id: str,
        report_text: str,
        source_name: str | None,
        source_bytes: bytes | None,
    ) -> str:
        folder = self.project_dir(project_id) / "feedback" / feedback_id
        folder.mkdir(parents=True, exist_ok=True)
        if source_bytes and source_name:
            safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(source_name).name)
            attachment = folder / "attachments" / safe_name
            attachment.parent.mkdir(parents=True, exist_ok=True)
            attachment.write_bytes(source_bytes)
        atomic_write(folder / "source.md", report_text or "（用户未提供文字报告）")
        return self.relative(folder / "source.md")

    def read_relative_text(self, path: str | None) -> str:
        if not path:
            return ""
        resolved = self.resolve(path)
        if not resolved.exists():
            return ""
        return resolved.read_text(encoding="utf-8", errors="replace")
