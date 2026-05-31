import tempfile
import unittest
from pathlib import Path

from cterm.tools_mcp import finder


class FinderToolTests(unittest.TestCase):
    def test_requires_explicit_pattern(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Resume.pdf").write_text("resume")

            result = finder(tmp)

        self.assertFalse(result["ok"])
        self.assertIn("requires an explicit pattern", result["error"])

    def test_accepts_include_as_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Resume.pdf").write_text("resume")
            Path(tmp, "notes.txt").write_text("notes")

            result = finder(
                tmp,
                pattern="__no_direct_match__",
                include=["*Resume*", "*.pdf"],
                type_filter="file",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"], ["Resume.pdf"])

    def test_accepts_include_as_json_list_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Ahmed_CV.docx").write_text("resume")
            Path(tmp, "notes.txt").write_text("notes")

            result = finder(
                tmp,
                pattern="__no_direct_match__",
                include='["*CV*", "*.docx"]',
                type_filter="file",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"], ["Ahmed_CV.docx"])

    def test_rejects_invalid_include_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = finder(
                tmp,
                pattern="*",
                include='["*CV*"',
                type_filter="file",
            )

        self.assertFalse(result["ok"])
        self.assertIn("include must be", result["error"])


if __name__ == "__main__":
    unittest.main()
