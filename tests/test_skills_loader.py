import json
import tempfile
from pathlib import Path
import unittest

from cterm.skills_loader import SkillsLoader


class SkillsLoaderTests(unittest.TestCase):
    def test_loads_skill_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            skills_dir.mkdir()
            (skills_dir / "git.md").write_text(
                "# Git\n\n"
                "## When to use\n\n"
                "Use for git tasks.\n\n"
                "## Skill\n\n"
                "Always inspect status before changing branches.\n",
                encoding="utf-8",
            )
            (skills_dir / "README.md").write_text(
                "## When to use\n\nDocumentation.\n\n## Skill\n\nDo not load this.",
                encoding="utf-8",
            )

            skills = SkillsLoader(skills_dir).load()

        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "git")
        self.assertEqual(skills[0].when_to_use, "Use for git tasks.")
        self.assertEqual(
            skills[0].content,
            "Always inspect status before changing branches.",
        )

    def test_selects_skills_with_small_model_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            skills_dir.mkdir()
            (skills_dir / "git.md").write_text(
                "## When to use\n\nUse for git requests.\n\n"
                "## Skill\n\nUse git carefully.\n",
                encoding="utf-8",
            )

            def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
                self.assertEqual(model, "small")
                self.assertEqual(response_format, "json")
                self.assertIn("Skill name: git", messages[1]["content"])
                return {"message": {"content": json.dumps({"skills": ["git"]})}}

            selected = SkillsLoader(skills_dir).select(
                "update this branch from master",
                "small",
                fake_chat,
            )

        self.assertEqual([skill.name for skill in selected], ["git"])

    def test_renders_selected_skill_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            skills_dir.mkdir()
            (skills_dir / "git.md").write_text(
                "## When to use\n\nUse for git requests.\n\n"
                "## Skill\n\nUse git carefully.\n",
                encoding="utf-8",
            )

            loader = SkillsLoader(skills_dir)
            rendered = loader.render_for_system_prompt(loader.load())

        self.assertIn("## git", rendered)
        self.assertIn("Use git carefully.", rendered)


if __name__ == "__main__":
    unittest.main()
