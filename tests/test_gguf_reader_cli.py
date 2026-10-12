"""Regression test for the standalone GGUF reader CLI (issue #4)."""

import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest


READER = pathlib.Path(__file__).resolve().parents[1] / "opt" / "ai" / "gguf_reader.py"


def small_gguf():
    """Construct a tiny GGUF v3 file with one metadata field and F32 tensor."""
    def gguf_string(value):
        raw = value.encode("utf-8")
        return struct.pack("<Q", len(raw)) + raw

    data = b"GGUF" + struct.pack("<IQQ", 3, 1, 1)
    data += gguf_string("general.name") + struct.pack("<I", 8)
    data += gguf_string("regression")
    data += gguf_string("test.weight") + struct.pack("<I", 1)
    data += struct.pack("<QIQ", 2, 0, 0)
    data += b"\x00" * (-len(data) % 32)
    return data + struct.pack("<ff", 1.25, -2.5)


class GGUFReaderCLITest(unittest.TestCase):
    def test_standalone_reader_prints_metadata_and_tensor(self):
        with tempfile.TemporaryDirectory() as directory:
            model = pathlib.Path(directory) / "tiny.gguf"
            model.write_bytes(small_gguf())
            result = subprocess.run(
                [sys.executable, str(READER), str(model)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--- meta ---", result.stdout)
        self.assertIn("general.name: regression", result.stdout)
        self.assertIn("--- tensors ---", result.stdout)
        self.assertIn("test.weight", result.stdout)
        self.assertIn("(2,)", result.stdout)
        self.assertIn("float32", result.stdout)


if __name__ == "__main__":
    unittest.main()
