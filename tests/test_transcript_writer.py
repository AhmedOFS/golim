import tempfile
import unittest
from pathlib import Path

from openterm.ui.tui.app.transcript_writer import TranscriptWriter


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


if __name__ == "__main__":
    unittest.main()
