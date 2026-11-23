"""Color and full-source AI workflow regressions, independent of model downloads.

The trained networks have their own real-inference tests in test_enhance.py.
These cases replace just that expensive call to verify precision, coordinates,
cache invalidation and cancellation in the shared editing pipeline.
"""
import base64
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from fieldroom import enhance
from fieldroom import imaging as im
from fieldroom.server import EditorServer


def pattern(width=640, height=386):
    y, x = np.indices((height, width))
    return np.stack(((x * 3 + y) % 220 + 15,
                     (y * 2 + x // 3) % 210 + 20,
                     (x + y * 3) % 200 + 25), axis=2).astype(np.uint8)


class ColorWorkflowTests(unittest.TestCase):
    def test_old_recipes_keep_their_pixels_when_new_controls_default_to_zero(self):
        original = np.array([
            [[18, 27, 39], [92, 102, 112], [180, 172, 139], [245, 242, 236]],
            [[52, 61, 99], [80, 142, 97], [172, 83, 57], [226, 131, 167]],
        ], dtype=np.uint8)
        source = im.Source(Image.fromarray(original), (4, 2), {})
        settings = {"exposure": .35, "contrast": 14, "highlights": -28,
                    "shadows": 22, "whites": 9, "blacks": -4,
                    "warmth": 18, "saturation": 12}
        # Recorded from the previous released rendering pipeline, before Color
        # and AI controls were added. This also protects existing sidecars.
        expected = np.array([
            [[16, 27, 43], [103, 114, 125], [206, 191, 141], [255, 255, 255]],
            [[59, 70, 119], [78, 161, 96], [215, 88, 51], [255, 137, 182]],
        ], dtype=np.uint8)
        with patch.object(enhance, "apply_rgb", side_effect=AssertionError("A legacy recipe invoked AI")):
            np.testing.assert_array_equal(np.asarray(im.render(source, settings)), expected)
            np.testing.assert_array_equal(np.asarray(im.render(source, {})), original)
        self.assertEqual(im.recipe(settings)["tint"], 0)
        self.assertEqual(im.recipe(settings)["vibrance"], 0)
        self.assertEqual(im.recipe(settings)["ai_denoise"], 0)
        self.assertEqual(im.recipe(settings)["ai_sharpen"], 0)

    def test_tint_moves_green_in_both_directions_without_changing_red_or_blue(self):
        source = im.Source(Image.new("RGB", (5, 3), (120, 120, 120)), (5, 3), {})
        magenta = np.asarray(im.render(source, {"tint": 60}))[1, 2]
        green = np.asarray(im.render(source, {"tint": -60}))[1, 2]
        self.assertLess(int(magenta[1]), 120)
        self.assertGreater(int(green[1]), 120)
        np.testing.assert_array_equal(magenta[[0, 2]], [120, 120])
        np.testing.assert_array_equal(green[[0, 2]], [120, 120])

    def test_vibrance_favors_muted_colors_and_keeps_neutral_grey(self):
        # The muted cool and warm samples have equal original chroma. Strong
        # vibrance should protect warm colors and already saturated colors.
        original = np.array([[[100, 115, 130], [130, 115, 100],
                              [20, 100, 230], [110, 110, 110]]], dtype=np.uint8)
        source = im.Source(Image.fromarray(original), (4, 1), {})
        raised = np.asarray(im.render(source, {"vibrance": 100})).astype(int)[0]
        lowered = np.asarray(im.render(source, {"vibrance": -100})).astype(int)[0]
        chroma = lambda rgb: np.max(rgb, axis=-1) - np.min(rgb, axis=-1)
        old_chroma = chroma(original.astype(int)[0])
        new_chroma = chroma(raised)
        self.assertGreater(new_chroma[0] / old_chroma[0], 1.5)
        self.assertLess(new_chroma[2] / old_chroma[2], 1.2)
        self.assertGreater(new_chroma[0], new_chroma[1])
        self.assertLess(chroma(lowered)[0], old_chroma[0])
        np.testing.assert_array_equal(raised[3], original[0, 3])


class AIWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"FIELDROOM_AI_CACHE_DIR": str(self.folder / "ai-cache")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.clear_caches()
        self.addCleanup(self.clear_caches)

    @staticmethod
    def clear_caches():
        with im._AI_LOCK:
            im._AI_CACHE.clear()
            with im._AI_RESIZE_LOCK:
                im._AI_RESIZED.clear()
        im.preview_source.cache_clear()

    def evict_prepared_results(self):
        self.clear_caches()
        for path in (self.folder / "ai-cache").glob("fieldroom-ai-*.npy"):
            path.unlink()

    def photo(self, pixels=None, name="photo.png"):
        path = self.folder / name
        Image.fromarray(pattern() if pixels is None else pixels).save(path)
        return path

    @staticmethod
    def learned_result(rgb, denoise=0, sharpen=0, **kwargs):
        # Distinct strength-dependent results make reuse of a wrong setting or an
        # already enhanced image visible without requiring neural inference.
        gain = 1 - denoise / 250
        offset = np.array([sharpen / 700, sharpen / 1100, sharpen / 900], np.float32)
        return np.clip(rgb * gain + offset, 0, 1).astype(np.float32)

    def test_ai_receives_full_original_and_preview_export_share_it_for_each_strength(self):
        original = pattern()
        path = self.photo(original)
        revision = im.fingerprint(path)
        preview = im.preview_source(path, revision, 160)
        full = im.decode(path)
        outputs = []
        observed = []

        def learned(rgb, **kwargs):
            observed.append(rgb.copy())
            return self.learned_result(rgb, **kwargs)

        with patch.object(enhance, "apply_rgb", side_effect=learned) as model:
            for denoise, sharpen in ((20, 15), (70, 35)):
                settings = im.recipe({"ai_denoise": denoise, "ai_sharpen": sharpen})
                small = im.render(preview, settings)
                exported = im.render(full, settings)
                repeated = im.render(full, settings)
                self.assertEqual(small.size, preview.pixels.size)
                self.assertEqual(exported.size, full.size)
                np.testing.assert_array_equal(np.asarray(exported), np.asarray(repeated))
                self.assertEqual(model.call_count, len(outputs) + 1)
                outputs.append(np.asarray(exported))
            self.assertEqual(model.call_count, 2)
        for seen in observed:
            self.assertEqual(seen.shape, original.shape)
            np.testing.assert_array_equal(seen, original.astype(np.float32) / 255)
        self.assertFalse(np.array_equal(*outputs))
        np.testing.assert_array_equal(np.asarray(Image.open(path)), original)

    def test_raw_ai_prep_retains_16_bit_precision_without_8_bit_intermediate(self):
        path = self.folder / "precision.dng"
        path.write_bytes(b"RAW decoding is replaced only for this precision test")
        revision = im.fingerprint(path)
        ramp = np.arange(4096, dtype=np.uint16).reshape(32, 128) * 16
        raw = np.stack((ramp, np.flipud(ramp), np.fliplr(ramp)), axis=2)
        original = raw.copy()
        source = im.Source(raw, (128, 32), {"camera": "Precision fixture"}, b"metadata", path, revision)
        observed = []

        def identity(rgb, **kwargs):
            observed.append(rgb.copy())
            return rgb.copy()

        with patch.object(im, "decode", return_value=source), patch.object(enhance, "apply_rgb", side_effect=identity):
            result = im.prepare_ai(path, revision, 40, 30)
        self.assertEqual(result.pixels.dtype, np.uint16)
        np.testing.assert_allclose(result.pixels.astype(int), original.astype(int), atol=1, rtol=0)
        self.assertGreater(len(np.unique(result.pixels[:, :, 0])), 4000)
        fractional = np.abs(observed[0] * 255 - np.round(observed[0] * 255))
        self.assertGreater(float(np.median(fractional)), .1)
        self.assertEqual(result.metadata, source.metadata)
        self.assertEqual(result.exif, source.exif)
        self.assertEqual(result.enhancement, (40, 30))
        np.testing.assert_array_equal(raw, original)

    def test_accepted_crop_native_tiles_match_export_after_ai_color_and_detail(self):
        path = self.photo(pattern(1005, 721))
        source = im.decode(path)
        settings = im.recipe({"ai_denoise": 45, "ai_sharpen": 20,
                              "crop": [.059, .101, .947, .867],
                              "exposure": .2, "tint": -15, "vibrance": 38,
                              "luminance": 15, "color": 20, "sharpen": 60, "radius": 2.4})
        with patch.object(enhance, "apply_rgb", side_effect=self.learned_result) as model:
            exported = im.render(source, settings)
            left, top, right, bottom = im.pixel_box(source.size, settings["crop"])
            self.assertEqual(exported.size, (right - left, bottom - top))
            for column, row in ((0, 0), (1, 0), (0, 1), (1, 1)):
                tile = Image.open(io.BytesIO(im.detail_tile(source, settings, column, row, 1)))
                x0, y0 = max(left, column * im.TILE), max(top, row * im.TILE)
                x1, y1 = min(right, (column + 1) * im.TILE), min(bottom, (row + 1) * im.TILE)
                expected = exported.crop((x0 - left, y0 - top, x1 - left, y1 - top))
                np.testing.assert_array_equal(np.asarray(tile), np.asarray(expected))
            self.assertEqual(model.call_count, 1)

    def test_changed_original_rejects_old_revision_and_recomputes_new_source(self):
        path = self.photo(np.full((19, 31, 3), 75, np.uint8))
        old_revision = im.fingerprint(path)
        with patch.object(enhance, "apply_rgb", side_effect=self.learned_result) as model:
            first = im.prepare_ai(path, old_revision, 30, 10)
            stat = path.stat()
            Image.new("RGB", (31, 19), (160, 160, 160)).save(path)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            new_revision = im.fingerprint(path)
            self.assertNotEqual(old_revision, new_revision)
            with self.assertRaisesRegex(ValueError, "original changed"):
                im.prepare_ai(path, old_revision, 30, 10)
            self.assertEqual(model.call_count, 1)
            second = im.prepare_ai(path, new_revision, 30, 10)
            self.assertEqual(model.call_count, 2)
            self.assertEqual(second.fingerprint, new_revision)
            self.assertFalse(np.array_equal(first.pixels, second.pixels))
            np.testing.assert_array_equal(model.call_args.args[0], np.full((19, 31, 3), 160 / 255, np.float32))

    def test_original_changed_during_inference_is_not_cached(self):
        path = self.photo(np.full((17, 23, 3), 100, np.uint8))
        revision = im.fingerprint(path)

        def changed(rgb, **kwargs):
            stat = path.stat()
            Image.new("RGB", (23, 17), (180, 180, 180)).save(path)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            return rgb.copy()

        with patch.object(enhance, "apply_rgb", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "original changed during"):
                im.prepare_ai(path, revision, 25, 10)
        with patch.object(enhance, "apply_rgb", side_effect=self.learned_result) as retry:
            im.prepare_ai(path, im.fingerprint(path), 25, 10)
            self.assertEqual(retry.call_count, 1)

    def test_cancelled_result_is_not_cached_and_previous_edit_stays_available(self):
        path = self.photo(np.full((180, 32, 3), 125, np.uint8))
        revision = im.fingerprint(path)
        original_bytes = path.read_bytes()
        stop = [False]

        def learned(rgb, denoise=0, sharpen=0, **kwargs):
            if denoise == 70:
                stop[0] = True  # Cancel after the network, before a result is accepted.
            return self.learned_result(rgb, denoise, sharpen)

        with patch.object(enhance, "apply_rgb", side_effect=learned) as model:
            previous = im.prepare_ai(path, revision, 20, 10)
            with self.assertRaises(enhance.EnhancementCancelled):
                im.prepare_ai(path, revision, 70, 10, cancelled=lambda: stop[0])
            self.assertIs(im.prepare_ai(path, revision, 20, 10), previous)
            self.assertEqual(model.call_count, 2)
        with patch.object(enhance, "apply_rgb", side_effect=self.learned_result) as retry:
            accepted = im.prepare_ai(path, revision, 70, 10)
            repeated = im.prepare_ai(path, revision, 70, 10)
            self.assertIs(accepted, repeated)
            self.assertEqual(retry.call_count, 1)
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_interactive_cache_misses_never_start_neural_inference(self):
        path = self.photo(pattern(93, 65))
        source = im.decode(path)
        settings = im.recipe({"ai_denoise": 35, "ai_sharpen": 20, "tint": 15})
        fallback = {**settings, "ai_denoise": 0, "ai_sharpen": 0}
        expected_thumbnail = im.jpeg(im.render(im.decode(path, 270, thumbnail=True), fallback), 78)
        with EditorServer(state_directory=self.folder / "state") as server:
            identifier = server.session.browse(self.folder)["photos"][0]["id"]
            request = {"id": identifier, "recipe": settings, "column": 0, "row": 0, "step": 1}
            with patch.object(im, "prepare_ai", side_effect=AssertionError("An interactive cache miss started preparation")) as prepare, \
                    patch.object(enhance, "apply_rgb", side_effect=AssertionError("An interactive cache miss ran a model")) as model:
                self.assertIsNone(im.prepared_ai_source(source, settings))
                with self.assertRaises(im.AIPreviewNotPrepared) as missing:
                    im.prepared_ai_source(source, settings, required=True)
                self.assertEqual(missing.exception.code, "ai_not_prepared")
                for route in ("preview", "tile"):
                    with self.subTest(route=route), self.assertRaises(im.AIPreviewNotPrepared) as missing:
                        server.dispatch(route, request)
                    self.assertEqual(missing.exception.code, "ai_not_prepared")
                self.assertEqual(server.dispatch("thumbnail", request), expected_thumbnail)
                prepare.assert_not_called()
                model.assert_not_called()

    def test_memory_cache_hit_holds_prepared_source_after_eviction(self):
        path = self.photo(pattern(71, 47))
        source = im.decode(path)
        settings = im.recipe({"ai_denoise": 30, "ai_sharpen": 20, "vibrance": 15})
        with patch.object(enhance, "apply_rgb", side_effect=self.learned_result):
            prepared = im.prepare_ai(path, source.fingerprint, 30, 20)
        expected = np.asarray(im.render(prepared, settings))
        with patch.object(im, "prepare_ai", side_effect=AssertionError("Held result tried to prepare again")), \
                patch.object(enhance, "apply_rgb", side_effect=AssertionError("Held result ran a model")):
            held = im.prepared_ai_source(source, settings, required=True)
            self.assertIs(held, prepared)
            self.evict_prepared_results()
            self.assertIsNone(im.prepared_ai_source(source, settings))
            np.testing.assert_array_equal(np.asarray(im.render(held, settings)), expected)

    def test_disk_cache_hit_keeps_mapping_alive_after_its_file_is_evicted(self):
        path = self.photo(pattern(83, 59))
        source = im.decode(path)
        settings = im.recipe({"ai_denoise": 25, "ai_sharpen": 30, "exposure": .2})
        with patch.object(enhance, "apply_rgb", side_effect=self.learned_result):
            prepared = im.prepare_ai(path, source.fingerprint, 25, 30)
        expected = np.asarray(im.render(prepared, settings))
        self.clear_caches()
        with patch.object(im, "prepare_ai", side_effect=AssertionError("Mapped result tried to prepare again")), \
                patch.object(enhance, "apply_rgb", side_effect=AssertionError("Mapped result ran a model")):
            held = im.prepared_ai_source(source, settings, required=True)
            self.assertIsInstance(held.pixels, np.memmap)
            self.assertFalse(held.pixels.flags.writeable)
            np.testing.assert_array_equal(held.pixels, prepared.pixels)
            self.evict_prepared_results()
            self.assertIsNone(im.prepared_ai_source(source, settings))
            np.testing.assert_array_equal(np.asarray(im.render(held, settings)), expected)
            tile = Image.open(io.BytesIO(im.detail_tile(held, settings, 0, 0, 1)))
            np.testing.assert_array_equal(np.asarray(tile), expected)

    def test_interactive_hits_survive_eviction_between_lookup_and_render(self):
        path = self.photo(pattern(640, 386))
        source = im.decode(path)
        settings = im.recipe({"ai_denoise": 40, "ai_sharpen": 15,
                              "crop": [.03, .07, .95, .94], "tint": -12})
        lookup = im.prepared_ai_source
        with EditorServer(state_directory=self.folder / "state") as server:
            identifier = server.session.browse(self.folder)["photos"][0]["id"]
            request = {"id": identifier, "recipe": settings, "column": 0, "row": 0, "step": 1}
            for route in ("preview", "tile", "thumbnail"):
                with self.subTest(route=route):
                    with patch.object(enhance, "apply_rgb", side_effect=self.learned_result):
                        prepared = im.prepare_ai(path, source.fingerprint, 40, 15)
                    full = im.render(prepared, settings)
                    if route == "preview":
                        expected = base64.b64encode(im.jpeg(full, 90)).decode("ascii")
                    elif route == "tile":
                        expected = im.detail_tile(prepared, settings, 0, 0, 1)
                    else:
                        reduced = lookup(im.decode(path, 270, thumbnail=True), settings, required=True)
                        expected = im.jpeg(im.render(reduced, settings), 78)

                    def held_then_evicted(original, recipe, **kwargs):
                        held = lookup(original, recipe, **kwargs)
                        self.assertIsNotNone(held)
                        self.evict_prepared_results()
                        return held

                    with patch.object(im, "prepared_ai_source", side_effect=held_then_evicted), \
                            patch.object(im, "prepare_ai", side_effect=AssertionError("Eviction restarted preparation")), \
                            patch.object(enhance, "apply_rgb", side_effect=AssertionError("Eviction restarted neural inference")):
                        actual = server.dispatch(route, request)
                    self.assertEqual(actual["image"] if route == "preview" else actual, expected)


if __name__ == "__main__":
    unittest.main()
