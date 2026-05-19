import unittest
from unittest.mock import patch

from cterm.llm import ToolAgent


class VerifierRetryTests(unittest.TestCase):
    def test_rejected_final_answer_is_kept_before_retry(self):
        captured_messages = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            captured_messages.append([message.copy() for message in messages])
            if len(captured_messages) == 1:
                return {"message": {"content": "I found the files but did not copy them."}}
            return {"message": {"content": "I copied the files."}}

        agent = ToolAgent("main")
        agent.tools = []

        with patch.object(agent, "_select_skills_prompt", return_value=""), \
             patch.object(agent, "_verify_history", side_effect=[(False, "Copy step missing."), (True, "")]), \
             patch("cterm.llm.chat_with_model_api", side_effect=fake_chat):
            result = agent._run_with_native_tools("Find CVs and copy them.")

        self.assertEqual(result, "I copied the files.")
        retry_messages = captured_messages[1]
        self.assertEqual(retry_messages[-2]["role"], "assistant")
        self.assertEqual(
            retry_messages[-2]["content"],
            "I found the files but did not copy them.",
        )
        self.assertEqual(retry_messages[-1]["role"], "user")
        self.assertIn("Copy step missing.", retry_messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
