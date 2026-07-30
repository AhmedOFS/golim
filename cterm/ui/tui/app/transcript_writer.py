from datetime import datetime
from pathlib import Path


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
        return Path.home() / "cterm" / "transcripts" / f"cterm_{timestamp}.log"

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
