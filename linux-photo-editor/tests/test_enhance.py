"""Offline AI detail: installation safety, cancellation and real model results."""
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageFilter

from fieldroom import enhance
from fieldroom.enhance_models import DetailModel, EnhancementUnavailable, _download, locate_models, validate


class EnhancementSafetyTests(unittest.TestCase):
    def test_disabled_ai_needs_no_models_and_keeps_fractional_samples(self):
        original = np.random.default_rng(1).uniform(.1, .9, (7, 13, 3)).astype(np.float32)
        with patch.object(enhance, "_session", side_effect=AssertionError("Disabled AI must not load models")):
            result = enhance.apply_rgb(original)
        np.testing.assert_array_equal(original, result)
        self.assertIsNot(original, result)

    def test_bad_strength_or_pixels_fail_before_inference(self):
        good = np.full((3, 5, 3), .5, np.float32)
        for strength in (None, True, "20", -1, 101, float("nan")):
            with self.subTest(strength=strength), self.assertRaises(ValueError):
                enhance.apply_rgb(good, denoise=strength)
        for bad in (np.zeros((3, 5)), np.zeros((0, 5, 3)), np.full((3, 5, 3), np.nan), good * 3):
            with self.assertRaises(ValueError):
                enhance.apply_rgb(bad, denoise=50)

    def test_cancel_during_a_tile_does_not_return_partial_work_or_touch_input(self):
        original = np.full((17, 600, 3), .4, np.float32)
        before = original.copy()
        stop = [False]

        def predict(session, blob, expected):
            stop[0] = True
            return blob * .9

        with patch.object(enhance, "_session", return_value=object()), patch.object(enhance, "_predict", side_effect=predict):
            with self.assertRaises(enhance.EnhancementCancelled):
                enhance.apply_rgb(original, denoise=100, cancelled=lambda: stop[0])
        np.testing.assert_array_equal(original, before)

    def test_cancel_before_start_does_not_load_models(self):
        with patch.object(enhance, "_session", side_effect=AssertionError("Cancelled job loaded model")):
            with self.assertRaises(enhance.EnhancementCancelled):
                enhance.apply_rgb(np.zeros((1, 1, 3), np.float32), denoise=20, cancelled=lambda: True)

    def test_missing_models_are_reported_without_creating_or_downloading(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "absent"
            with patch("fieldroom.enhance_models.urlopen", side_effect=AssertionError("Read-only check downloaded")):
                with self.assertRaises(EnhancementUnavailable):
                    locate_models(folder)
            self.assertFalse(folder.exists())

    def test_modified_model_is_rejected_even_after_prior_verification(self):
        data = b"verified-neural-model"
        spec = DetailModel("test", "test.onnx", len(data), hashlib.sha256(data).hexdigest(), "https://example.invalid/model", "Test")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / spec.filename
            path.write_bytes(data)
            validate(path, spec)
            path.write_bytes(b"X" + data[1:])
            with self.assertRaises(EnhancementUnavailable):
                validate(path, spec)

    def test_failed_download_keeps_existing_model_and_removes_partial_file(self):
        data = b"verified-neural-model"
        spec = DetailModel("test", "test.onnx", len(data), hashlib.sha256(data).hexdigest(), "https://example.invalid/model", "Test")
        class Response(io.BytesIO):
            headers = {}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / spec.filename
            path.write_bytes(b"existing-file")
            with patch("fieldroom.enhance_models.urlopen", return_value=Response(b"X" * len(data))):
                with self.assertRaises(EnhancementUnavailable):
                    _download(path, spec)
            self.assertEqual(path.read_bytes(), b"existing-file")
            self.assertEqual([p.name for p in path.parent.iterdir()], [spec.filename])


class RealEnhancementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = enhance.status()
        if not state["available"]:
            raise unittest.SkipTest("Install the verified offline AI detail models to run actual inference tests: " + state["message"])

    def test_actual_denoising_is_offline_and_reduces_noise(self):
        image = np.clip(.5 + np.random.default_rng(5).normal(0, 20 / 255, (80, 96, 3)), 0, 1).astype(np.float32)
        before = image.copy()
        fractions = []
        with patch("fieldroom.enhance_models.urlopen", side_effect=AssertionError("Inference attempted a download")):
            result = enhance.apply_rgb(image, denoise=100, progress=lambda fraction, message: fractions.append(fraction))
        self.assertEqual(result.shape, image.shape)
        self.assertEqual(result.dtype, np.float32)
        self.assertLess(float(result.std()), float(image.std()) * .5)
        self.assertLess(abs(float(result.mean()) - .5), .02)
        self.assertEqual(fractions[-1], 1.0)
        self.assertEqual(fractions, sorted(fractions))
        np.testing.assert_array_equal(before, image)

    def test_tiled_denoise_matches_one_forward_across_the_internal_seam(self):
        image = np.random.default_rng(6).uniform(.2, .8, (45, 535, 3)).astype(np.float32)
        actual = enhance.apply_rgb(image, denoise=70)
        halo = enhance.DENOISE_HALO
        padded = np.pad(image, ((halo, halo), (halo, halo), (0, 0)), mode="reflect")
        with enhance._lock:
            network = enhance._session("denoise")
            blob = padded.transpose(2, 0, 1)[None]
            predicted = enhance._predict(network, blob, blob.shape)[0].transpose(1, 2, 0)
        predicted = predicted[halo:-halo, halo:-halo]
        expected = np.clip(image + (predicted - image) * .7, 0, 1)
        np.testing.assert_allclose(actual, expected, atol=2e-6, rtol=1e-5)

    def test_learned_detail_sharpens_a_blurred_edge_without_resizing(self):
        stripes = np.tile(np.where((np.arange(224) // 16) % 2, .8, .2), (128, 1))
        blurred = Image.fromarray(np.round(stripes * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.1))
        mono = np.asarray(blurred, dtype=np.float32) / 255
        image = np.repeat(mono[:, :, None], 3, axis=2)
        result = enhance.apply_rgb(image, sharpen=100)
        self.assertEqual(result.shape, image.shape)
        self.assertTrue(np.isfinite(result).all())
        original_gradient = np.abs(np.diff(image[20:-20, 20:-20, 0], axis=1)).max()
        result_gradient = np.abs(np.diff(result[20:-20, 20:-20, 0], axis=1)).max()
        self.assertGreater(float(result_gradient), float(original_gradient) * 1.02)
        np.testing.assert_allclose(result[:, :, 0], result[:, :, 1], atol=1e-6)

    def test_detail_tile_grid_does_not_change_interior_pixels(self):
        image = np.random.default_rng(7).uniform(.25, .75, (75, 440, 3)).astype(np.float32)
        whole = enhance.apply_rgb(image, sharpen=65)
        shifted = enhance.apply_rgb(image[:, 17:], sharpen=65)
        # Shift the tiling grid without changing these pixels or their context.
        np.testing.assert_allclose(whole[12:-12, 40:-20], shifted[12:-12, 23:-20], atol=2e-6, rtol=1e-5)

    def test_small_odd_and_single_pixel_images_keep_shape_and_finite_values(self):
        for height, width in ((1, 1), (3, 5), (17, 2)):
            with self.subTest(size=(width, height)):
                result = enhance.apply_rgb(np.full((height, width, 3), .5, np.float32), denoise=35, sharpen=30)
                self.assertEqual(result.shape, (height, width, 3))
                self.assertTrue(np.isfinite(result).all())
                self.assertGreaterEqual(float(result.min()), 0)
                self.assertLessEqual(float(result.max()), 1)


if __name__ == "__main__":
    unittest.main()
