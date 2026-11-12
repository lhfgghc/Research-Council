from __future__ import annotations

import re
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_DISCUSSION_SKILLS = [
    "ccf-idea-optimizer",
    "ccf-idea-reviewer",
    "ccf-experiment-designer",
    "ccf-literature-searcher",
]
DEFAULT_WRITING_SKILLS = [
    "ccf-paper-writer",
    "ccf-paper-reviewer",
    "ccf-humanization",
    "ccf-integrity-auditor",
]
WRITING_STAGES = {
    "gate",
    "draft_a",
    "draft_b",
    "draft_c",
    "coherence",
    "revise_a",
    "revise_b",
    "revise_c",
    "save",
}


def skill_kind_for(phase: str, stage: str = "") -> str:
    """Choose which skill bundle should be active for a phase/step."""
    if phase.startswith("R") or phase == "P4":
        return "writing"
    if phase == "P3" and stage in WRITING_STAGES:
        return "writing"
    return "discussion"


@dataclass
class SkillInfo:
    id: str
    name: str
    description: str
    path: str


class SkillRegistry:
    def __init__(self, directories: list[str]) -> None:
        self.directories = [Path(item) for item in directories if item.strip()]

    def list_skills(self) -> list[SkillInfo]:
        found: dict[str, SkillInfo] = {}
        for root in self.directories:
            if not root.exists():
                continue
            candidates = list(root.glob("*/SKILL.md")) + list(root.glob("*/*/SKILL.md"))
            for entry in candidates:
                skill_id = entry.parent.name
                if skill_id in found:
                    continue
                text = entry.read_text(encoding="utf-8", errors="replace")[:20000]
                name, description = self._metadata(text, skill_id)
                found[skill_id] = SkillInfo(skill_id, name, description, str(entry))
        return sorted(found.values(), key=lambda item: item.id.lower())

    def load_bundle(
        self,
        names: list[str],
        max_chars: int = 36000,
        per_skill_chars: int = 9000,
    ) -> tuple[str, list[dict[str, str]]]:
        if not names:
            return "", []
        available = {item.id: item for item in self.list_skills()}
        sections: list[str] = []
        loaded: list[dict[str, str]] = []
        remaining = max_chars
        for name in names:
            info = available.get(name)
            if not info or remaining <= 0:
                continue
            text = Path(info.path).read_text(encoding="utf-8", errors="replace")
            text = self._strip_frontmatter(text)
            excerpt = text[: min(remaining, per_skill_chars)]
            sections.append(f"### Skill: {info.name} ({info.id})\n{excerpt}")
            loaded.append({
                "id": info.id, "name": info.name, "path": info.path,
                "sha256": sha256(excerpt.encode("utf-8")).hexdigest(),
            })
            remaining -= len(excerpt)
        return "\n\n".join(sections), loaded

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end >= 0:
                return text[end + 4 :].lstrip()
        return text

    @staticmethod
    def _metadata(text: str, fallback: str) -> tuple[str, str]:
        name_match = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
        desc_match = re.search(r"^(?:description|summary):\s*(.+)$", text, re.MULTILINE)
        heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        name = (name_match.group(1) if name_match else (heading.group(1) if heading else fallback)).strip(' "\'')
        description = desc_match.group(1).strip(' "\'') if desc_match else "本地研究技能"
        return name[:120], description[:400]
