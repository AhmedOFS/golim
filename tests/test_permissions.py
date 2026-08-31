import unittest
from unittest.mock import patch

from openterm.core.agent import ToolAgent
from openterm.core.permissions import Permissions
from openterm.core.runtime import Runtime
from openterm.core.utils import _PYTHON_DENIED_RESULT


class FakeUI:
    def __init__(self, approved=True):
        self.approved = approved
        self.approval_prompts = []
        self.python_shown = []
        self.tool_results = []

    def tool_call(self, tool_name, args):
        pass

    def tool_output(self, fd=None, line="", end="\n", result=None):
        if result is not None:
            self.tool_results.append(result)

    def python_code(self, code):
        self.python_shown.append(code)

    def request_binary_approval(self, binary):
        self.approval_prompts.append(binary)
        return self.approved

    def request_sudo_password(self):
        return "session-password"

    def request_python_approval(self, code):
        self.approval_prompts.append(("python", code))
        return self.approved

    def request_write_approval(self, path, content, mode):
        self.approval_prompts.append(("write", path, content, mode))
        return self.approved


class PermissionsTests(unittest.TestCase):
    def test_plain_bash_needs_no_approval_and_skips_config(self):
        ui = FakeUI(approved=False)
        permissions = Permissions(ui)
        with patch("openterm.core.permissions.get_config") as get_config:
            denial = permissions.is_approved("bash", {"command": "ls -la"})

        self.assertIsNone(denial)
        get_config.assert_not_called()
        self.assertEqual(ui.approval_prompts, [])

    def test_whole_command_python_is_treated_as_plain_bash(self):
        permissions = Permissions(FakeUI(approved=False))
        with patch("openterm.core.permissions.get_config"):
            denial = permissions.is_approved(
                "bash", {"command": 'python3 -c "print(1)"'}
            )
        self.assertIsNone(denial)

    def test_embedded_python_requires_approval_in_restricted_mode(self):
        ui = FakeUI(approved=True)
        permissions = Permissions(ui)
        args = {"command": "echo hi && python3 -c \"print('x')\""}
        with patch("openterm.core.permissions.get_config") as get_config:
            get_config.return_value.unrestricted_mode = False
            denial = permissions.is_approved("bash", args)

        self.assertIsNone(denial)
        self.assertEqual(ui.approval_prompts, [("python", "print('x')")])

    def test_embedded_python_denial_returns_denied_result(self):
        ui = FakeUI(approved=False)
        permissions = Permissions(ui)
        args = {"command": "echo hi && python3 -c \"print('x')\""}
        with patch("openterm.core.permissions.get_config") as get_config:
            get_config.return_value.unrestricted_mode = False
            denial = permissions.is_approved("bash", args)

        self.assertEqual(denial, dict(_PYTHON_DENIED_RESULT))
        self.assertEqual(ui.tool_results, [dict(_PYTHON_DENIED_RESULT)])
        self.assertFalse(denial["ok"])

    def test_embedded_python_unrestricted_mode_displays_without_prompting(self):
        ui = FakeUI(approved=False)
        permissions = Permissions(ui)
        args = {"command": "echo hi && python3 -c \"print('x')\""}
        with patch("openterm.core.permissions.get_config") as get_config:
            get_config.return_value.unrestricted_mode = True
            denial = permissions.is_approved("bash", args)

        self.assertIsNone(denial)
        self.assertEqual(ui.python_shown, ["print('x')"])
        self.assertEqual(ui.approval_prompts, [])

    def test_exec_tool_denied_in_restricted_mode(self):
        ui = FakeUI(approved=False)
        permissions = Permissions(ui)
        with patch("openterm.core.permissions.get_config") as get_config:
            get_config.return_value.unrestricted_mode = False
            denial = permissions.is_approved(
                "exec", {"code": "print(1)"}
            )

        self.assertEqual(denial, dict(_PYTHON_DENIED_RESULT))
        self.assertEqual(ui.approval_prompts, [("python", "print(1)")])
        self.assertEqual(ui.tool_results, [dict(_PYTHON_DENIED_RESULT)])

    def test_exec_tool_accepts_script_and_source_aliases(self):
        ui = FakeUI(approved=True)
        permissions = Permissions(ui)
        with patch("openterm.core.permissions.get_config") as get_config:
            get_config.return_value.unrestricted_mode = False
            self.assertIsNone(permissions.is_approved("exec", {"script": "a=1"}))
            self.assertIsNone(permissions.is_approved("exec_python", {"source": "b=2"}))

        self.assertEqual(
            ui.approval_prompts,
            [("python", "a=1"), ("python", "b=2")],
        )

    def test_other_tools_are_not_gated_in_restricted_mode(self):
        ui = FakeUI(approved=False)
        permissions = Permissions(ui)
        with patch("openterm.core.permissions.get_config") as get_config:
            get_config.return_value.unrestricted_mode = False
            denial = permissions.is_approved("finder", {"pattern": "*"})

        self.assertIsNone(denial)
        self.assertEqual(ui.approval_prompts, [])

    def test_write_file_approval_uses_path_content_mode(self):
        ui = FakeUI(approved=True)
        permissions = Permissions(ui)
        with patch("openterm.core.permissions.get_config") as get_config:
            get_config.return_value.unrestricted_mode = False
            denial = permissions.is_approved("write_file", {
                "path": "/tmp/out.txt",
                "content": "hello",
                "mode": "append",
            })

        self.assertIsNone(denial)
        self.assertEqual(
            ui.approval_prompts,
            [("write", "/tmp/out.txt", "hello", "append")],
        )

    def test_approve_binary_delegates_to_ui_with_binary_key(self):
        ui = FakeUI(approved=True)
        permissions = Permissions(ui)

        self.assertTrue(permissions.approve_binary({
            "approval_id": "1:0",
            "binary": "/usr/bin/chmod",
        }))
        self.assertEqual(ui.approval_prompts, ["/usr/bin/chmod"])

        ui.approved = False
        self.assertFalse(permissions.approve_binary({"binary": "/usr/bin/ls"}))
        self.assertEqual(ui.approval_prompts, ["/usr/bin/chmod", "/usr/bin/ls"])

    def test_runtime_passes_permissions_to_agent_and_client(self):
        ui = FakeUI(approved=False)
        runtime = Runtime(model="main")
        runtime.bind_ui(ui)

        client = runtime.create_mcp_client("/tmp/openterm-permissions-test.sock")
        agent = ToolAgent(
            "main",
            ui=ui,
            mcp_client=client,
            permissions=runtime.permissions,
        )

        self.assertIsInstance(runtime.permissions, Permissions)
        self.assertIs(agent.permissions, runtime.permissions)
        wired = client.on_approval_request
        self.assertEqual(wired.__self__, runtime.permissions)
        self.assertEqual(wired.__func__, Permissions.approve_binary)
        approval = {"approval_id": "1:0", "binary": "/usr/bin/apt"}
        self.assertFalse(client.on_approval_request(approval))
        self.assertEqual(ui.approval_prompts, ["/usr/bin/apt"])

    def test_runtime_wires_auth_session_callback_with_transport_request(self):
        ui = FakeUI()
        runtime = Runtime(model="main")
        runtime.bind_ui(ui)

        client = runtime.create_mcp_client("/tmp/openterm-auth-test.sock")

        self.assertEqual(
            client.on_auth_request({"auth_id": "1:auth:0", "kind": "sudo_password"}),
            "session-password",
        )

    def test_rebinding_ui_refreshes_permissions_handler(self):
        ui_a = FakeUI(approved=True)
        ui_b = FakeUI(approved=False)
        runtime = Runtime(model="main")
        runtime.bind_ui(ui_a)
        first_permissions = runtime.permissions

        runtime.bind_ui(ui_b)

        self.assertIsNot(runtime.permissions, first_permissions)
        self.assertIs(runtime.permissions.ui, ui_b)
        self.assertFalse(runtime.permissions.approve_binary({"binary": "/bin/x"}))
        self.assertEqual(ui_b.approval_prompts, ["/bin/x"])
        self.assertEqual(ui_a.approval_prompts, [])


if __name__ == "__main__":
    unittest.main()
