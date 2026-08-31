import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openterm.toolset.tools import finder


class FinderToolTests(unittest.TestCase):
    def test_pattern_defaults_to_wildcard(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Resume.pdf").write_text("resume")
            Path(tmp, "notes.txt").write_text("notes")

            result = finder(tmp)

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["matches"],
            [
                str(Path(tmp, "Resume.pdf")).replace("\\", "/"),
                str(Path(tmp, "notes.txt")).replace("\\", "/"),
            ],
        )

    def test_matching_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Resume.PDF").write_text("resume")
            Path(tmp, "Inside").mkdir()

            result = finder(tmp, pattern="resume.pdf")
            result_dirs = finder(tmp, pattern="INSIDE")

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["matches"],
            [str(Path(tmp, "Resume.PDF")).replace("\\", "/")],
        )
        self.assertEqual(
            result_dirs["matches"],
            [str(Path(tmp, "Inside")).replace("\\", "/")],
        )

    def test_pattern_is_wildcarded_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Ahmed_CV.pdf").write_text("resume")
            Path(tmp, "my_cool_video.txt").write_text("video")
            Path(tmp, "notes.txt").write_text("notes")

            bare = finder(tmp, pattern="cv", type_filter="file")
            both_sides = finder(tmp, pattern="*CV*", type_filter="file")
            partial = finder(tmp, pattern="c*v", type_filter="file")
            trailing = finder(tmp, pattern="cv*", type_filter="file")

        self.assertTrue(bare["ok"])
        self.assertEqual(
            bare["matches"],
            [str(Path(tmp, "Ahmed_CV.pdf")).replace("\\", "/")],
        )
        self.assertEqual(bare["matches"], both_sides["matches"])
        self.assertEqual(bare["matches"], trailing["matches"])
        self.assertEqual(
            partial["matches"],
            [
                str(Path(tmp, "Ahmed_CV.pdf")).replace("\\", "/"),
                str(Path(tmp, "my_cool_video.txt")).replace("\\", "/"),
            ],
        )
        self.assertNotIn(
            str(Path(tmp, "notes.txt")),
            bare["matches"],
        )

    def test_empty_pattern_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = finder(tmp, pattern="")

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
        self.assertEqual(
            result["matches"],
            [str(Path(tmp, "Resume.pdf")).replace("\\", "/")],
        )

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
        self.assertEqual(
            result["matches"],
            [str(Path(tmp, "Ahmed_CV.docx")).replace("\\", "/")],
        )

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

    def test_swallows_permission_error_raised_during_scandir_iteration(self):
        # /proc-style pseudo filesystems: scandir() opens the directory, but
        # the kernel fails readdir with EACCES, so PermissionError is raised
        # lazily on the first iteration step instead of at scandir() call time.
        import os as real_os

        original_scandir = real_os.scandir

        class DeferredPermissionDir:
            def __init__(self, path):
                self._path = path

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                raise PermissionError(13, "Permission denied", self._path)

        def fake_scandir(path):
            if str(path).endswith("protected"):
                return DeferredPermissionDir(path)
            return original_scandir(path)

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "protected").mkdir()
            Path(tmp, "Resume.pdf").write_text("resume")

            with mock.patch("os.scandir", side_effect=fake_scandir):
                result = finder(tmp, pattern="*.pdf")

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["matches"],
            [str(Path(tmp, "Resume.pdf")).replace("\\", "/")],
        )


if __name__ == "__main__":
    unittest.main()
