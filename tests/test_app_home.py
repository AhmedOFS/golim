import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openterm.config import app_home
from openterm.config.app_home import get_app_home, resolve_app_home


class AppHomeTests(unittest.TestCase):
    def test_resolve_app_home_binds_and_creates_home(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}), \
             patch.object(app_home, "_app_home", None):
            resolved = resolve_app_home()

            self.assertEqual(resolved, Path(tmp) / ".openterm")
            self.assertEqual(get_app_home(), resolved)
            self.assertTrue(resolved.is_dir())

            os.environ["HOME"] = "/different-home"
            self.assertEqual(get_app_home(), resolved)


if __name__ == "__main__":
    unittest.main()
