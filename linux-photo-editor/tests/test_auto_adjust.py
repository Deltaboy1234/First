"""Subject-aware automatic controls, independent of neural-model accuracy."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from fieldroom import auto_adjust as aa
from fieldroom import smartcrop as sc
from fieldroom.models import ModelUnavailable


class AutoAdjustTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"photo.png"
        self.size = (320, 240)
        self.left_body = (.04, .05, .46, .95)
        self.left_face = (.15, .13, .28, .32)
        self.right_body = (.54, .05, .97, .95)
        self.right_face = (.67, .13, .84, .34)

    def analysis(self, people=1, faces=True):
        detections = [sc.Detection("person", self.left_body, .96)]
        if faces:
            detections.append(sc.Detection("face", self.left_face, .98))
        if people == 2:
            detections.append(sc.Detection("person", self.right_body, .96))
            if faces:
                detections.append(sc.Detection("face", self.right_face, .98))
        return sc.Analysis(self.size, tuple(detections) if people else ())

    def image(self, background=(100, 110, 120), left=(32, 32, 32), right=(225, 225, 225)):
        rgb = np.empty((self.size[1], self.size[0], 3), dtype=np.uint8)
        rgb[:] = background
        self.fill(rgb, self.left_face, left)
        self.fill(rgb, self.right_face, right)
        return rgb

    def fill(self, rgb, box, value):
        l, t, r, b = box
        height, width = rgb.shape[:2]
        rgb[round(t*height):round(b*height), round(l*width):round(r*width)] = value

    def suggest(self, rgb, *, analysis=None, **options):
        Image.fromarray(rgb).save(self.path)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch.object(sc, "analyze", return_value=analysis or self.analysis()):
            result = aa.suggest(self.path, **options)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertTrue(all(np.isfinite(value) for value in result["recipe"].values()))
        return result

    def test_background_does_not_drive_selected_face_exposure(self):
        first = self.suggest(self.image(background=(60, 80, 95)), subject_ids=["person-1"])
        second = self.suggest(self.image(background=(155, 165, 175)), subject_ids=["person-1"])
        self.assertEqual(first["method"], "subject-face")
        self.assertEqual(first["recipe"], second["recipe"])
        self.assertGreater(first["recipe"]["exposure"], .15)

    def test_selection_changes_focus_and_group_weights_people_equally(self):
        rgb = self.image()
        analysis = self.analysis(people=2)
        left = self.suggest(rgb, analysis=analysis, subject_ids=["person-1"])
        right = self.suggest(rgb, analysis=analysis, subject_ids=["person-2"])
        group = self.suggest(rgb, analysis=analysis, subject_ids=["person-2", "person-1"])
        self.assertGreater(left["recipe"]["exposure"], right["recipe"]["exposure"]+.15)
        self.assertLess(group["recipe"]["exposure"], left["recipe"]["exposure"])
        self.assertGreater(group["recipe"]["exposure"], right["recipe"]["exposure"])
        self.assertEqual(group["selected_subjects"], ["person-1", "person-2"])
        self.assertEqual(len(group["subjects"]), 3)
        prominent = self.suggest(rgb, analysis=analysis)
        self.assertEqual(prominent["selected_subjects"], ["person-2"])

    def test_naturally_dark_and_light_faces_are_not_normalized_to_one_target(self):
        dark = self.suggest(self.image(left=(85, 60, 45)), subject_ids=["person-1"])
        light = self.suggest(self.image(left=(195, 155, 132)), subject_ids=["person-1"])
        self.assertEqual(dark["recipe"]["exposure"], 0)
        self.assertEqual(light["recipe"]["exposure"], 0)

    def test_whole_frame_highlights_guard_positive_face_exposure(self):
        result = self.suggest(self.image(background=(252, 252, 252)), subject_ids=["person-1"])
        self.assertLessEqual(result["recipe"]["exposure"], .03)
        self.assertGreater(result["recipe"]["shadows"], 0)
        self.assertLess(result["recipe"]["highlights"], 0)
        self.assertIn("limited", result["warning"])

    def test_body_only_adjustment_is_explicit_and_conservative(self):
        rgb = self.image(background=(20, 20, 20), left=(20, 20, 20), right=(20, 20, 20))
        result = self.suggest(rgb, analysis=self.analysis(faces=False))
        self.assertEqual(result["method"], "subject-person")
        self.assertLessEqual(result["recipe"]["exposure"], .35)
        self.assertIn("body regions", result["warning"])

    def test_no_person_fallback_is_explicit(self):
        result = self.suggest(self.image(background=(25, 25, 25)), analysis=self.analysis(people=0))
        self.assertEqual(result["method"], "whole-image-fallback")
        self.assertEqual(result["selected_subjects"], [])
        self.assertIn("whole-image fallback", result["message"])
        self.assertLessEqual(abs(result["recipe"]["exposure"]), .35)

    def test_crop_limits_default_selection_and_rejects_explicit_outside_subject(self):
        rgb = self.image()
        result = self.suggest(rgb, analysis=self.analysis(people=2), crop=[.5, 0, 1, 1])
        self.assertEqual(result["selected_subjects"], ["person-2"])
        with self.assertRaisesRegex(ValueError, "outside the current crop"):
            self.suggest(rgb, analysis=self.analysis(people=2), subject_ids=["person-1"], crop=[.5, 0, 1, 1])

    def test_color_does_not_force_skin_to_neutral(self):
        rgb = self.image(background=(45, 65, 150))
        self.fill(rgb, self.left_body, (145, 95, 75))
        result = self.suggest(rgb, kind="color", subject_ids=["person-1"])
        self.assertNotIn("warmth", result["recipe"])
        self.assertNotIn("tint", result["recipe"])
        self.assertIn("White balance is unchanged", result["message"])

    def test_color_does_not_neutralize_colored_clothes(self):
        for color in ((190, 45, 45), (35, 170, 110), (65, 110, 220), (210, 175, 180)):
            with self.subTest(color=color):
                rgb = self.image(background=(80, 80, 80))
                self.fill(rgb, self.left_body, color)
                self.fill(rgb, self.left_face, (190, 135, 110))
                result = self.suggest(rgb, kind="color", subject_ids=["person-1"])
                self.assertNotIn("warmth", result["recipe"])
                self.assertNotIn("tint", result["recipe"])

    def neutral_scene(self, cast):
        rgb = self.image(background=(105, 30, 160))
        l, t, r, b = self.left_body
        for index, brightness in enumerate((.35, .53, .74)):
            box = (l, t+(b-t)*index/3, r, t+(b-t)*(index+1)/3)
            self.fill(rgb, box, tuple(round(brightness*gain*255) for gain in cast))
        self.fill(rgb, self.left_face, (190, 135, 110))
        return rgb

    def test_consistent_neutral_cast_gets_small_bounded_correction(self):
        for cast, key, sign in (((.96, 1, 1.03), "warmth", 1),
                                ((1.03, 1, .97), "warmth", -1),
                                ((1, 1.04, 1), "tint", 1)):
            with self.subTest(cast=cast):
                result = self.suggest(self.neutral_scene(cast), kind="color", subject_ids=["person-1"])
                self.assertGreater(result["recipe"][key]*sign, 0)
                self.assertTrue(all(abs(value) <= 10 for value in result["recipe"].values()))
                self.assertIn("near-neutral", result["message"])

    def test_one_flat_pale_garment_is_not_sufficient_neutral_evidence(self):
        rgb = self.image(background=(105, 30, 160))
        self.fill(rgb, self.left_body, (172, 180, 185))
        self.fill(rgb, self.left_face, (190, 135, 110))
        result = self.suggest(rgb, kind="color", subject_ids=["person-1"])
        self.assertEqual(result["recipe"], {})

    def test_color_measurement_follows_selected_people_not_other_neutrals(self):
        rgb = self.neutral_scene((.96, 1, 1.03))
        self.fill(rgb, self.right_body, (45, 140, 210))
        self.fill(rgb, self.right_face, (180, 125, 95))
        left = self.suggest(rgb, analysis=self.analysis(people=2), kind="color", subject_ids=["person-1"])
        right = self.suggest(rgb, analysis=self.analysis(people=2), kind="color", subject_ids=["person-2"])
        self.assertGreater(left["recipe"]["warmth"], 0)
        self.assertEqual(right["recipe"], {})

    def test_missing_models_do_not_silently_use_a_whole_image_fallback(self):
        Image.fromarray(self.image()).save(self.path)
        with patch.object(sc, "analyze", side_effect=ModelUnavailable("Offline models need installation.")):
            with self.assertRaisesRegex(ModelUnavailable, "models"):
                aa.suggest(self.path)

    def test_invalid_input_never_changes_subject_or_silently_falls_back(self):
        for options in ({"kind":"unknown"}, {"subject_ids":[]}, {"subject_ids":"person-1"},
                        {"subject_ids":[1]}, {"subject_ids":["person-1","person-1"]},
                        {"subject_ids":["missing"]}, {"crop":[0,0,float("nan"),1]},
                        {"crop":[False,0,1,1]}, {"crop":[0,0,2,1]}, {"crop":"0,0,1,1"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.suggest(self.image(), **options)
        with self.assertRaises(ValueError):
            self.suggest(self.image(), analysis=self.analysis(people=0), subject_ids=["person-1"])

    def test_changed_original_during_detection_is_rejected(self):
        Image.fromarray(self.image()).save(self.path)
        def replace(_):
            Image.new("RGB", self.size, (1, 2, 3)).save(self.path)
            return self.analysis()
        with patch.object(sc, "analyze", side_effect=replace), self.assertRaisesRegex(ValueError, "original changed"):
            aa.suggest(self.path)


if __name__ == "__main__":
    unittest.main()
