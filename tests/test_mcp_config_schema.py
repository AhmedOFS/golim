import json
import os
from pathlib import Path
import tempfile
import unittest

from cterm.mcp.config import get_config


class McpConfigSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name

    def tearDown(self):
        if self.old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.old_xdg
        self.tmp.cleanup()

    def test_reader_returns_the_nested_schema_without_flattening(self):
        path = Path(self.tmp.name) / "cterm" / "config.json"
        path.parent.mkdir()
        schema = {
            "providers": {"ollama": {"ollama_host": "http://localhost:11434"}},
            "attributes": {"bash_unrestricted": True, "exa_api_key": "key"},
        }
        path.write_text(json.dumps(schema))

        self.assertEqual(get_config().read(), schema)
        self.assertNotIn("bash_unrestricted", get_config().read().keys())

    def test_reader_rejects_flat_config(self):
        path = Path(self.tmp.name) / "cterm" / "config.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"bash_unrestricted": True}))

        self.assertEqual(get_config().read(), {})


if __name__ == "__main__":
    unittest.main()
