"""Load markdown skills and select relevant ones for a user prompt."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Callable


@dataclass(frozen=True)
class Skill:
    name: str
    path: Path
    when_to_use: str
    content: str


class SkillsLoader:
    """Loads skills from markdown files and asks a small model to select matches."""

    DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
    WHEN_HEADINGS = {"when to use", "when to use this skill", "usage"}
    CONTENT_HEADINGS = {"skill", "skill content", "content", "instructions"}

    def __init__(
        self,
        skills_dir: str | Path | None = None,
        debug: bool = False,
    ):
        self.skills_dir = Path(skills_dir) if skills_dir else self.DEFAULT_SKILLS_DIR
        self.debug = debug

    def load(self) -> list[Skill]:
        if not self.skills_dir.exists():
            return []

        skills = []
        for path in sorted(self.skills_dir.glob("*.md")):
            if path.name.lower() == "readme.md":
                continue
            skill = self._load_skill(path)
            if skill:
                skills.append(skill)
        return skills

    def select(
        self,
        user_message: str,
        model: str,
        chat: Callable,
        binary: str = "ollama",
    ) -> list[Skill]:
        skills = self.load()
        if not skills:
            return []

        selection_messages = [
            {
                "role": "system",
                "content": (
                    "You select which skills are relevant to a user request.\n"
                    "Only select a skill when its when-to-use guidance very strictly matches.\n"
                    "Respond ONLY with valid JSON in this exact shape:\n"
                    '{ "skills": ["skill-name"] }\n'
                    "Use an empty list when no skills match.\n"
                    "Always prefer using a sigle skill"
                ),
            },
            {
                "role": "user",
                "content": self._build_selection_prompt(user_message, skills),
            },
        ]

        response = chat(
            model,
            selection_messages,
            tools=None,
            binary=binary,
            response_format="json",
        )
        content = response.get("message", {}).get("content") or ""
        selected_names = self._parse_selected_names(content)
        if not selected_names:
            return []

        by_name = {skill.name: skill for skill in skills}
        return [by_name[name] for name in selected_names if name in by_name]

    def render_for_system_prompt(self, skills: list[Skill]) -> str:
        if not skills:
            return ""

        blocks = [
            "Additional task-specific skills follow. Apply them when completing the user's request."
        ]
        for skill in skills:
            blocks.append(f"## {skill.name}\n{skill.content.strip()}")
        return "\n\n".join(blocks)

    def _load_skill(self, path: Path) -> Skill | None:
        text = path.read_text(encoding="utf-8")
        sections = self._parse_sections(text)
        when_to_use = self._first_section(sections, self.WHEN_HEADINGS)
        content = self._first_section(sections, self.CONTENT_HEADINGS)
        if not when_to_use or not content:
            return None
        return Skill(
            name=path.stem,
            path=path,
            when_to_use=when_to_use.strip(),
            content=content.strip(),
        )

    def _parse_sections(self, text: str) -> dict[str, str]:
        sections: dict[str, list[str]] = {}
        current = None

        for line in text.splitlines():
            match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
            if match:
                current = self._normalise_heading(match.group(1))
                sections.setdefault(current, [])
                continue
            if current:
                sections[current].append(line)

        return {heading: "\n".join(lines).strip() for heading, lines in sections.items()}

    def _first_section(self, sections: dict[str, str], headings: set[str]) -> str:
        for heading in headings:
            if sections.get(heading):
                return sections[heading]
        return ""

    def _build_selection_prompt(self, user_message: str, skills: list[Skill]) -> str:
        skill_blocks = []
        for skill in skills:
            skill_blocks.append(
                f"Skill name: {skill.name}\n"
                f"When to use:\n{skill.when_to_use}"
            )
        available_skills = "\n\n".join(skill_blocks)

        return (
            f"User request:\n{user_message}\n\n"
            f"Available skills:\n\n"
            f"{available_skills}"
        )

    def _parse_selected_names(self, content: str) -> list[str]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = self._parse_json_object(content)

        names = parsed.get("skills", []) if isinstance(parsed, dict) else []
        if not isinstance(names, list):
            return []

        selected = []
        for name in names:
            if isinstance(name, str) and name not in selected:
                selected.append(name)
        return selected

    def _parse_json_object(self, content: str) -> dict:
        decoder = json.JSONDecoder()
        for idx, char in enumerate(content):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(content[idx:])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
        return {}

    def _normalise_heading(self, heading: str) -> str:
        return re.sub(r"\s+", " ", heading.strip().lower())
