import json
import os
from pathlib import Path
import tempfile
import unittest

from cterm.mcp.config import get_config, init_config


class McpConfigSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        self.tmp.cleanup()

    def test_reader_returns_the_nested_schema_without_flattening(self):
        path = Path(self.tmp.name) / ".cterm" / "config" / "config.json"
        path.parent.mkdir(parents=True)
        schema = {
            "providers": {"ollama": {"ollama_host": "http://localhost:11434"}},
            "attributes": {"bash_unrestricted": True, "exa_api_key": "key"},
        }
        path.write_text(json.dumps(schema))

        self.assertEqual(get_config().read(), schema)
        self.assertNotIn("bash_unrestricted", get_config().read().keys())

    def test_reader_rejects_flat_config(self):
        path = Path(self.tmp.name) / ".cterm" / "config" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"bash_unrestricted": True}))

        self.assertEqual(get_config().read(), {})

    def test_init_config_binds_the_new_app_home(self):
        config = init_config()

        self.assertEqual(config.config_path, Path(self.tmp.name) / ".cterm" / "config" / "config.json")
        self.assertEqual(config.whitelist_path, Path(self.tmp.name) / ".cterm" / "privileged_whitelist")


if __name__ == "__main__":
    unittest.main()
