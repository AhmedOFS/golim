import logging
import tempfile
import unittest
from pathlib import Path

from golim.logger import (
    NOISY_THIRD_PARTY_LOGGERS,
    RunLogging,
    THIRD_PARTY_LOG_LEVEL,
    setup_root_logger,
)


class SetupRootLoggerTests(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.previous_root_level = self.root.level
        self.previous_root_handlers = list(self.root.handlers)
        self.saved_levels = {
            name: logging.getLogger(name).level
            for name in NOISY_THIRD_PARTY_LOGGERS
        }
        self.addCleanup(self._restore)

    def _restore(self):
        self.root.setLevel(self.previous_root_level)
        for handler in list(self.root.handlers):
            if handler not in self.previous_root_handlers:
                self.root.removeHandler(handler)
        for name, level in self.saved_levels.items():
            logging.getLogger(name).setLevel(level)

    def test_setup_root_logger_caps_noisy_third_party_loggers(self):
        for name in NOISY_THIRD_PARTY_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)

        setup_root_logger()

        for name in NOISY_THIRD_PARTY_LOGGERS:
            self.assertEqual(
                logging.getLogger(name).level,
                THIRD_PARTY_LOG_LEVEL,
                f"logger {name!r} should be capped at WARNING",
            )

    def test_setup_root_logger_keeps_golim_diagnostics_at_debug(self):
        setup_root_logger()

        self.assertEqual(self.root.level, logging.DEBUG)
        golim_logger = logging.getLogger("golim.core.agent")
        self.assertEqual(golim_logger.getEffectiveLevel(), logging.DEBUG)


class RunLoggingTests(unittest.TestCase):
    def test_run_log_captures_golim_debug_but_not_capped_third_party_debug(self):
        setup_root_logger()
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / "transcripts" / "session.log"
            run_logging = RunLogging(transcript_path)
            try:
                logging.getLogger("golim.core.agent").debug("golim diagnostic line")
                logging.getLogger("mistune").debug("entering list: StateBlock(...)")
                logging.getLogger("mistune").warning("mistune warning line")
            finally:
                run_logging.close()

            text = transcript_path.parent.parent / "logs" / transcript_path.name
            content = text.read_text(encoding="utf-8")
            self.assertIn("golim diagnostic line", content)
            self.assertNotIn("entering list", content)
            self.assertIn("mistune warning line", content)


if __name__ == "__main__":
    unittest.main()
