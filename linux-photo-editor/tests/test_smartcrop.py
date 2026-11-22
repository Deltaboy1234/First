"""Smart Crop's subject framing and failure behavior, without model downloads.

These cases use explicit detections so they exercise framing decisions separately
from model accuracy. Real model smoke tests are documented in the release checks.
"""
import math
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from fieldroom import imaging as im
from fieldroom import smartcrop as sc


class SmartCropTests(unittest.TestCase):
    def analysis(self, *detections, size=(6000, 4000)):
        return sc.Analysis(size=size, detections=tuple(
            sc.Detection(kind=kind, box=box, confidence=confidence)
            for kind, box, confidence in detections
        ))

    def assert_valid_crop(self, crop):
        self.assertEqual(len(crop), 4)
        self.assertTrue(all(math.isfinite(v) for v in crop))
        self.assertTrue(0 <= crop[0] < crop[2] <= 1)
        self.assertTrue(0 <= crop[1] < crop[3] <= 1)
        im.recipe({"crop": crop})

    def assert_contains(self, crop, box, tolerance=1e-5):
        self.assert_valid_crop(crop)
        self.assertLessEqual(crop[0], box[0] + tolerance)
        self.assertLessEqual(crop[1], box[1] + tolerance)
        self.assertGreaterEqual(crop[2], box[2] - tolerance)
        self.assertGreaterEqual(crop[3], box[3] - tolerance)

    def test_no_subject_returns_no_crop_for_every_preset(self):
        for preset in ("very-tight", "tight", "normal", "loose", "full-body"):
            with self.subTest(preset=preset):
                result = sc.suggest_from_analysis(self.analysis(), preset)
                self.assertIsNone(result["crop"])
                self.assertEqual(result["status"], "no_subject")
                self.assertEqual(result["subjects"], [])
                self.assertTrue(result["warning"])

    def test_tight_crop_follows_off_center_face_and_keeps_headroom(self):
        face = (.10, .22, .20, .39)
        person = (.05, .12, .30, .91)
        result = sc.suggest_from_analysis(self.analysis(
            ("person", person, .92), ("face", face, .97)))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["method"], "face")
        self.assert_contains(result["crop"], face)
        self.assertLess(result["crop"][1], face[1])
        self.assertLess((result["crop"][0] + result["crop"][2]) / 2, .35)

    def test_portrait_presets_increase_context_without_losing_face(self):
        face = (.42, .30, .54, .47)
        analysis = self.analysis(("person", (.25, .13, .72, .94), .93),
                                 ("face", face, .98))
        areas = []
        for preset in ("very-tight", "tight", "normal", "loose"):
            with self.subTest(preset=preset):
                result = sc.suggest_from_analysis(analysis, preset)
                self.assertEqual(result["status"], "ready")
                crop = result["crop"]
                self.assert_contains(crop, face)
                areas.append((crop[2] - crop[0]) * (crop[3] - crop[1]))
        self.assertEqual(areas, sorted(areas))
        self.assertGreater(areas[-1], areas[0])

    def test_body_only_never_guesses_a_portrait(self):
        analysis = self.analysis(("person", (.15, .10, .65, .90), .96))
        for preset in ("very-tight", "tight", "normal", "loose"):
            with self.subTest(preset=preset):
                result = sc.suggest_from_analysis(analysis, preset)
                self.assertIsNone(result["crop"])
                self.assertEqual(result["status"], "face_not_detected")
                self.assertTrue(result["warning"])
                self.assertEqual(len(result["subjects"]), 1)
        body = sc.suggest_from_analysis(analysis, "full-body")
        self.assertEqual(body["status"], "ready")
        self.assert_contains(body["crop"], (.15, .10, .65, .90))

    def test_face_only_never_invents_complete_body(self):
        analysis = self.analysis(("face", (.42, .20, .55, .41), .97))
        self.assertEqual(sc.suggest_from_analysis(analysis)["status"], "ready")
        result = sc.suggest_from_analysis(analysis, "full-body")
        self.assertIsNone(result["crop"])
        self.assertEqual(result["status"], "person_not_detected")
        self.assertTrue(result["warning"])

    def test_weak_standalone_false_face_cannot_create_crop(self):
        result = sc.suggest_from_analysis(self.analysis(
            ("face", (.45, .52, .53, .64), .88)))
        self.assertIsNone(result["crop"])
        self.assertEqual(result["status"], "no_subject")

    def test_downward_face_can_use_matching_person_confidence(self):
        face = (.40, .18, .53, .36)
        result = sc.suggest_from_analysis(self.analysis(
            ("person", (.24, .08, .67, .85), .852),
            ("face", face, .878)))
        self.assertEqual(result["status"], "ready")
        self.assert_contains(result["crop"], face)

    def test_low_torso_face_is_not_corroborated_by_body(self):
        result = sc.suggest_from_analysis(self.analysis(
            ("person", (.20, .08, .70, .93), .93),
            ("face", (.39, .64, .49, .78), .88)))
        self.assertIsNone(result["crop"])
        self.assertEqual(result["status"], "face_not_detected")

    def test_full_body_preserves_extremities_at_image_edges(self):
        for box in ((.0, .01, .43, .99), (.63, .0, 1., 1.),
                    (.02, .22, .98, .71), (.30, .08, .62, .90)):
            for aspect in ("Free", "Original", "1:1", "4:5", "3:2", "16:9"):
                with self.subTest(box=box, aspect=aspect):
                    result = sc.suggest_from_analysis(
                        self.analysis(("person", box, .95)), "full-body", aspect=aspect)
                    self.assertEqual(result["status"], "ready")
                    self.assert_contains(result["crop"], box)

    def test_requested_aspect_is_respected_when_subject_fits(self):
        box = (.42, .25, .57, .68)
        result = sc.suggest_from_analysis(self.analysis(("person", box, .95)),
                                          "full-body", aspect="4:5")
        self.assert_contains(result["crop"], box)
        l, t, r, b = result["crop"]
        self.assertAlmostEqual((r-l)*6000 / ((b-t)*4000), 4/5, places=4)
        self.assertEqual(result["aspect"], "4:5")

    def test_impossible_aspect_warns_without_cutting_the_person(self):
        box = (.02, .05, .98, .95)
        result = sc.suggest_from_analysis(self.analysis(("person", box, .95)),
                                          "full-body", aspect="4:5")
        self.assert_contains(result["crop"], box)
        self.assertTrue(result["warning"])
        self.assertEqual(result["aspect"], "Free")

    def test_multiple_subjects_can_select_person_away_from_center(self):
        left = (.03, .10, .27, .90)
        right = (.61, .16, .94, .92)
        analysis = self.analysis(("person", left, .94), ("person", right, .96),
                                 ("face", (.09, .17, .18, .32), .97),
                                 ("face", (.72, .24, .83, .40), .98))
        initial = sc.suggest_from_analysis(analysis)
        self.assertEqual(len(initial["subjects"]), 2)
        target = min(initial["subjects"], key=lambda s: s["box"][0])
        selected = sc.suggest_from_analysis(analysis, subject_id=target["id"])
        self.assertEqual(selected["selected_subject"], target["id"])
        self.assertLess((selected["crop"][0] + selected["crop"][2]) / 2, .30)
        again = sc.suggest_from_analysis(analysis)
        self.assertEqual(initial["subjects"], again["subjects"])

    def test_two_faces_in_one_person_outline_remain_selectable(self):
        analysis = self.analysis(
            ("person", (.10, .08, .85, .95), .96),
            ("face", (.30, .16, .48, .36), .97),
            ("face", (.58, .38, .69, .50), .95))
        initial = sc.suggest_from_analysis(analysis)
        self.assertEqual(len(initial["subjects"]), 2)
        second = next(s for s in initial["subjects"] if s["kind"] == "face")
        selected = sc.suggest_from_analysis(analysis, subject_id=second["id"])
        self.assert_contains(selected["crop"], (.58, .38, .69, .50))
        self.assertGreater((selected["crop"][0]+selected["crop"][2])/2, .60)
        body = sc.suggest_from_analysis(analysis, "full-body", subject_id=second["id"])
        self.assertIsNone(body["crop"])
        self.assertEqual(body["status"], "person_not_detected")

    def test_invalid_controls_are_rejected(self):
        analysis = self.analysis(("person", (.1, .1, .6, .9), .95))
        for options in ({"preset": "mystery"}, {"aspect": "0:0"},
                        {"aspect": "nan"}, {"subject_id": "not-a-subject"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                sc.suggest_from_analysis(analysis, **options)

    def test_suggest_is_nondestructive_and_uses_oriented_original_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rotated.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (1200, 800), (130, 150, 170)).save(path, exif=exif)
            before = path.read_bytes()
            analysis = self.analysis(("person", (.10, .08, .65, .94), .95),
                                     ("face", (.22, .18, .40, .31), .97),
                                     size=(800, 1200))
            with patch.object(sc, "analyze", return_value=analysis):
                result = sc.suggest(path, "tight", aspect="4:5")
            self.assertEqual(path.read_bytes(), before)
            self.assert_valid_crop(result["crop"])
            source = im.decode(path)
            self.assertEqual(source.size, analysis.size)
            cropped = im.render(source, im.recipe({"crop": result["crop"]}))
            self.assertAlmostEqual(cropped.width / cropped.height, 4/5, delta=.01)

    def test_missing_models_keep_crop_empty_without_a_network_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photo.png"
            Image.new("RGB", (80, 60)).save(path)
            before = path.read_bytes()
            with patch.dict(os.environ, {"FIELDROOM_MODEL_DIR": str(Path(tmp) / "missing")}), \
                 patch("fieldroom.models.urlopen") as network:
                result = sc.suggest(path)
            self.assertIsNone(result["crop"])
            self.assertEqual(result["status"], "model_unavailable")
            self.assertTrue(result["warning"])
            network.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((Path(tmp) / "missing").exists())

    def test_analysis_is_single_flight_and_cached_across_presets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oriented.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (96, 72)).save(path, exif=exif)
            calls = []
            def detect(rgb):
                calls.append(rgb.shape)
                time.sleep(.02)  # Give concurrent callers a chance to enter analyze.
                return (sc.Detection("person", (.1, .1, .8, .9), .95),)
            with sc._ANALYSIS_LOCK:
                sc._CACHE.clear()
            try:
                with patch.object(sc, "detect_rgb", side_effect=detect), \
                     patch.object(sc.im, "decode", wraps=im.decode) as decode:
                    with ThreadPoolExecutor(max_workers=6) as pool:
                        results = list(pool.map(lambda _: sc.analyze(path), range(6)))
                    self.assertEqual(decode.call_count, 1)
                    self.assertEqual(calls, [(96, 72, 3)])
                    self.assertTrue(all(a is results[0] for a in results))
                    self.assertEqual(results[0].size, (72, 96))
                    sc.suggest(path, "tight")
                    sc.suggest(path, "full-body", aspect="1:1")
                    self.assertEqual(decode.call_count, 1)
                    previous = path.stat()
                    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns+1))
                    self.assertIsNot(sc.analyze(path), results[0])
                    self.assertEqual(decode.call_count, 2)
            finally:
                with sc._ANALYSIS_LOCK:
                    sc._CACHE.clear()

    def test_different_photos_serialize_analysis_and_bound_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for index in range(8):
                path = Path(tmp) / f"{index}.png"
                Image.new("RGB", (48, 36), (index, 0, 0)).save(path)
                paths.append(path)
            counts = {"active": 0, "peak": 0}
            guard = threading.Lock()
            def detect(_):
                with guard:
                    counts["active"] += 1
                    counts["peak"] = max(counts["peak"], counts["active"])
                time.sleep(.01)
                with guard:
                    counts["active"] -= 1
                return ()
            with sc._ANALYSIS_LOCK:
                sc._CACHE.clear()
            try:
                with patch.object(sc, "detect_rgb", side_effect=detect):
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        list(pool.map(sc.analyze, paths))
                self.assertEqual(counts["peak"], 1)
                self.assertLessEqual(len(sc._CACHE), 6)
            finally:
                with sc._ANALYSIS_LOCK:
                    sc._CACHE.clear()


if __name__ == "__main__":
    unittest.main()
