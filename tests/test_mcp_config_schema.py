import json
import os
from pathlib import Path
import tempfile
import unittest

from openterm import config
from openterm.config.app_home import resolve_app_home
from openterm.mcp.config import ServerConfig, get_config, init_config


class McpConfigSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name
        resolve_app_home()

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        self.tmp.cleanup()

    def test_reader_returns_the_nested_schema_without_flattening(self):
        path = Path(self.tmp.name) / ".openterm" / "config" / "config.json"
        path.parent.mkdir(parents=True)
        schema = {
            config.Config.PROVIDERS: {
                config.Config.OLLAMA: {
                    config.Config.OLLAMA_SERVER_URL: "http://localhost:11434",
                },
            },
            config.Config.ATTRIBUTES: {
                config.Config.UNRESTRICTED_MODE: True,
                config.Config.EXA_API_KEY: "key",
            },
        }
        path.write_text(json.dumps(schema))

        self.assertEqual(get_config().read(), schema)
        self.assertNotIn(config.Config.UNRESTRICTED_MODE, get_config().read().keys())

    def test_reader_uses_the_client_schema_keys_and_values(self):
        path = Path(self.tmp.name) / ".openterm" / "config" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            config.Config.PROVIDERS: {},
            config.Config.ATTRIBUTES: {
                config.Config.UNRESTRICTED_MODE: True,
                config.Config.WEBSEARCH_PROVIDER: config.Config.PARALLEL,
            },
        }))

        server_config = get_config()

        self.assertTrue(server_config.unrestricted_mode)
        self.assertEqual(server_config.websearch_provider, config.Config.PARALLEL)

    def test_reader_rejects_flat_config(self):
        path = Path(self.tmp.name) / ".openterm" / "config" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({config.Config.UNRESTRICTED_MODE: True}))

        self.assertEqual(get_config().read(), {})

    def test_init_config_binds_the_new_app_home(self):
        server_config = init_config()

        self.assertIsInstance(server_config, ServerConfig)
        self.assertEqual(server_config.config_path, Path(self.tmp.name) / ".openterm" / "config" / "config.json")
        self.assertEqual(server_config.whitelist_path, Path(self.tmp.name) / ".openterm" / "privileged_whitelist")


if __name__ == "__main__":
    unittest.main()
