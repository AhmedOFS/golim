import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cterm.app_home import get_app_home, resolve_app_home


class AppHomeTests(unittest.TestCase):
    def test_resolve_app_home_binds_and_creates_home(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            resolved = resolve_app_home()

            self.assertEqual(resolved, Path(tmp) / ".cterm")
            self.assertEqual(get_app_home(), resolved)
            self.assertTrue(resolved.is_dir())


if __name__ == "__main__":
    unittest.main()
