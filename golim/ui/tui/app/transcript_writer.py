import json
from datetime import datetime
from pathlib import Path

from golim.config.app_home import get_app_home


class TranscriptWriter:
    """Write the user-visible transcript for one run.

    When no stream is supplied, the writer creates and owns a transcript file.
    Supplying a stream remains useful for tests and callers that manage their
    own output destination.
    """

    def __init__(self, file=None, path: str | Path | None = None):
        if file is not None and path is not None:
            raise ValueError("provide either file or path, not both")

        self._owns_file = file is None
        if file is None:
            transcript_path = Path(path) if path is not None else self._default_path()
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(transcript_path, "w", encoding="utf-8")
            self.path = transcript_path
        else:
            self._file = file
            self.path = Path(path) if path is not None else None

    @staticmethod
    def _default_path() -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return get_app_home() / "transcripts" / f"golim_{timestamp}.log"

    def write(self, text: str, end: str = "\n") -> None:
        if self._file is None:
            return
        try:
            suffix = end if end in ("\n", "") else "\n"
            self._file.write(f"{text}{suffix}")
            self._file.flush()
        except Exception:
            pass

    def close(self) -> None:
        if self._file is None:
            return
        try:
            self._file.flush()
            if self._owns_file:
                self._file.close()
        except Exception:
            pass

    def flush(self) -> None:
        if self._file is None:
            return
        try:
            self._file.flush()
        except Exception:
            pass

    def write_record(self, *, kind: str, text: str, **extra) -> None:
        """Append one structured JSON-lines record.

        ``kind`` describes how the entry should be reconstructed (for
        example ``"markdown"``, ``"thinking"``, or ``"expandable"``) and any
        extra keyword fields are preserved verbatim on the record. Records
        are written as a single JSON object per line alongside the plain
        ``write`` output, so the file stays human-readable while remaining
        reconstructable via :func:`load_records`.
        """
        if self._file is None:
            return
        try:
            payload = {"kind": kind, "text": text, **extra}
            self._file.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._file.flush()
        except Exception:
            pass


def load_records(path) -> list[dict]:
    """Reconstruct structured records from a transcript file or stream.

    Returns a list of dicts. JSON-lines records carrying a ``kind`` key are
    returned unchanged; any other non-empty line is surfaced as a plain
    ``{"kind": "text", "text": ...}`` record so legacy transcript files stay
    readable.
    """
    if hasattr(path, "read"):
        lines = path.read().splitlines()
    else:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    records: list[dict] = []
    for line in lines:
        if not line:
            continue
        try:
            payload = json.loads(line)
            if isinstance(payload, dict) and "kind" in payload:
                records.append(payload)
                continue
        except json.JSONDecodeError:
            pass
        records.append({"kind": "text", "text": line})
    return records
