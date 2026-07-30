import logging
import json
from pathlib import Path

LOG_FORMAT = '%(levelname)s: %(message)s'
DIAGNOSTIC_LOGGER = logging.getLogger("cterm.diagnostic")
DIAGNOSTIC_LOGGER.propagate = False


class RunLogging:
    def __init__(self, transcript_path: str | Path):
        transcript_path = Path(transcript_path)
        self.path = transcript_path.parent.parent / "logs" / transcript_path.name
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.handler = logging.FileHandler(self.path, mode="w", encoding="utf-8")
        self.handler._cterm_run_log = True
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
        )

        self.root = logging.getLogger()
        self.root.addHandler(self.handler)
        self.previous_diagnostic_handlers = list(DIAGNOSTIC_LOGGER.handlers)
        self.previous_diagnostic_level = DIAGNOSTIC_LOGGER.level
        self.previous_diagnostic_propagate = DIAGNOSTIC_LOGGER.propagate
        for previous in self.previous_diagnostic_handlers:
            DIAGNOSTIC_LOGGER.removeHandler(previous)
        DIAGNOSTIC_LOGGER.addHandler(self.handler)
        DIAGNOSTIC_LOGGER.setLevel(logging.DEBUG)
        DIAGNOSTIC_LOGGER.propagate = False

    def close(self) -> None:
        self.root.removeHandler(self.handler)
        DIAGNOSTIC_LOGGER.removeHandler(self.handler)
        for previous in self.previous_diagnostic_handlers:
            DIAGNOSTIC_LOGGER.addHandler(previous)
        DIAGNOSTIC_LOGGER.setLevel(self.previous_diagnostic_level)
        DIAGNOSTIC_LOGGER.propagate = self.previous_diagnostic_propagate
        self.handler.close()


def start_run_logging(transcript_path: str | Path) -> RunLogging:
    return RunLogging(transcript_path)


def log_diagnostic_section(title: str, payload) -> None:
    if isinstance(payload, str):
        body = payload
    else:
        try:
            body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        except TypeError:
            body = repr(payload)
    DIAGNOSTIC_LOGGER.info("## %s\n%s", title, body)


def setup_root_logger(debug: bool = False) -> None:
    logging.basicConfig(level=logging.DEBUG, format=LOG_FORMAT, force=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in root.handlers:
        handler.setLevel(logging.DEBUG if debug else logging.WARNING)
