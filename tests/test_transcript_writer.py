import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from golim.ui.tui.app.transcript_writer import TranscriptWriter, load_records


class TranscriptWriterTests(unittest.TestCase):
    def test_writer_creates_and_owns_the_transcript_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcripts" / "session.log"
            writer = TranscriptWriter(path=path)
            writer.write("hello")
            writer.close()

            self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")

    def test_writer_does_not_close_supplied_stream(self):
        from io import StringIO

        stream = StringIO()
        writer = TranscriptWriter(stream)
        writer.write("hello")
        writer.close()

        self.assertFalse(stream.closed)
        self.assertEqual(stream.getvalue(), "hello\n")

    def test_write_record_emits_a_single_json_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.log"
            writer = TranscriptWriter(path=path)
            writer.write_record(kind="markdown", text="# heading", ok=True)
            writer.close()

            line = path.read_text(encoding="utf-8").strip()
            self.assertEqual(json.loads(line), {"kind": "markdown", "text": "# heading", "ok": True})

    def test_write_record_round_trips_through_load_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.log"
            writer = TranscriptWriter(path=path)
            writer.write("prompt: list files")
            writer.write_record(kind="expandable", text="… 49 50", truncated=True)
            writer.write_record(kind="thinking", text="inspect the system")
            writer.write_record(kind="markdown", text="## Summary", ok=True)
            writer.close()

            records = load_records(path)
            self.assertEqual(
                records,
                [
                    {"kind": "text", "text": "prompt: list files"},
                    {"kind": "expandable", "text": "… 49 50", "truncated": True},
                    {"kind": "thinking", "text": "inspect the system"},
                    {"kind": "markdown", "text": "## Summary", "ok": True},
                ],
            )

    def test_load_records_falls_back_for_legacy_plain_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.log"
            path.write_text("prompt: hello\n\nresponse: ok\n", encoding="utf-8")
            self.assertEqual(
                load_records(path),
                [
                    {"kind": "text", "text": "prompt: hello"},
                    {"kind": "text", "text": "response: ok"},
                ],
            )

    def test_load_records_accepts_a_stream(self):
        stream = StringIO('{"kind": "thinking", "text": "abc"}\n')
        self.assertEqual(load_records(stream), [{"kind": "thinking", "text": "abc"}])


if __name__ == "__main__":
    unittest.main()
