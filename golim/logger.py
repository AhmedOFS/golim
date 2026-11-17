import logging
import json
from pathlib import Path
from .version import __version__

LOG_FORMAT = '%(levelname)s: %(message)s'
DIAGNOSTIC_LOGGER = logging.getLogger("golim.diagnostic")
DIAGNOSTIC_LOGGER.propagate = False

# Third-party loggers captured at DEBUG flood the per-run diagnostic file
# (mistune parser traces alone produced ~8k lines per rendered answer).
# Golim's own loggers stay at DEBUG; these are capped so run logs keep only
# golim signal plus third-party warnings.
NOISY_THIRD_PARTY_LOGGERS = (
    "mistune",
    "urllib3",
    "asyncio",
    "textual",
    "httpx",
    "httpcore",
)
THIRD_PARTY_LOG_LEVEL = logging.WARNING


class RunLogging:
    def __init__(self, transcript_path: str | Path):
        transcript_path = Path(transcript_path)
        self.path = transcript_path.parent.parent / "logs" / transcript_path.name
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.handler = logging.FileHandler(self.path, mode="w", encoding="utf-8")
        self.handler._golim_run_log = True
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
        DIAGNOSTIC_LOGGER.info(
            "## session_start\n%s",
            json.dumps({"version": __version__}, ensure_ascii=False, indent=2),
        )

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


def setup_root_logger() -> None:
    logging.basicConfig(level=logging.DEBUG, format=LOG_FORMAT, force=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in root.handlers:
        handler.setLevel(logging.CRITICAL + 1)
    for name in NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(THIRD_PARTY_LOG_LEVEL)
