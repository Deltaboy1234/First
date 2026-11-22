"""Fast, offline regressions for verified model installation."""
from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fieldroom import models


class ModelInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.payload = b"a small verified model fixture"
        self.spec = models.ModelSpec(
            "face", "fixture.onnx", len(self.payload),
            hashlib.sha256(self.payload).hexdigest(), "models/fixture",
        )
        self.path = self.folder / self.spec.filename
        replacement = patch.object(models, "MODELS", (self.spec,))
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_same_size_corruption_is_rejected(self):
        self.path.write_bytes(b"!" + self.payload[1:])
        with self.assertRaisesRegex(models.ModelUnavailable, "SHA-256"):
            models.locate_models(self.folder)

    def test_failed_transfer_preserves_destination_and_removes_partial(self):
        previous = b"keep the old installation"
        for bad in (b"truncated", b"x" * len(self.payload), self.payload + b"extra"):
            with self.subTest(bytes=len(bad)):
                self.path.write_bytes(previous)
                response = io.BytesIO(bad)
                response.headers = {}  # Exercise the stream limit without Content-Length.
                with patch.object(models, "urlopen", return_value=response), redirect_stdout(io.StringIO()):
                    with self.assertRaises(models.ModelUnavailable):
                        models.download_models(self.folder)
                self.assertEqual(self.path.read_bytes(), previous)
                self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_verified_existing_model_is_reused_without_network(self):
        self.path.write_bytes(self.payload)
        with patch.object(models, "urlopen", side_effect=AssertionError("unexpected network")) as network, \
             redirect_stdout(io.StringIO()):
            result = models.download_models(self.folder)
        network.assert_not_called()
        self.assertEqual(result, {"face": self.path})
        self.assertEqual(self.path.read_bytes(), self.payload)


if __name__ == "__main__":
    unittest.main()
