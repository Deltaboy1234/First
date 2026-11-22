"""Opt-in real-model checks on public upstream photographs, with network blocked.

Set FIELDROOM_SMARTCROP_FIXTURES to a folder containing the three exact files
below and FIELDROOM_MODEL_DIR to the verified model directory. This module never
fetches assets. Ordinary unit-test runs skip it when the fixture folder is unset.

Fixture sources (SHA-256 verification makes changes explicit):
https://raw.githubusercontent.com/opencv/opencv/master/samples/data/basketball1.png
https://raw.githubusercontent.com/opencv/opencv/master/samples/data/messi5.jpg
https://raw.githubusercontent.com/Megvii-BaseDetection/YOLOX/main/assets/dog.jpg
"""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fieldroom import smartcrop as sc


FIXTURES = {
    "basketball1.png": "ba06f6701f7260998b430c39b6557f775497e6ce7b1a74f0b7ea6af371bf54a6",
    "messi5.jpg": "1d570e49654e84c7a943918537bd9e5e1ef82920152e147c834006e235be97c9",
    "dog.jpg": "5a9522051c3cec2bbd2f6323fccba32e8fbf3ddcc2b3e2fd46b04c720bc6f866",
}


@unittest.skipUnless(os.environ.get("FIELDROOM_SMARTCROP_FIXTURES"), "public model fixtures not configured")
class SmartCropModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(os.environ["FIELDROOM_SMARTCROP_FIXTURES"])
        for name, expected in FIXTURES.items():
            actual = hashlib.sha256((cls.folder / name).read_bytes()).hexdigest()
            if actual != expected:
                raise AssertionError(f"Public Smart Crop fixture changed: {name}")

    def setUp(self):
        # Clear analysis so each test really performs inference, not a stale mock.
        with sc._ANALYSIS_LOCK:
            sc._CACHE.clear()
        self.network = ExitStack()
        self.addCleanup(self.network.close)
        for target in ("socket.create_connection", "socket.socket.connect",
                       "socket.socket.connect_ex", "socket.getaddrinfo",
                       "fieldroom.models.urlopen"):
            self.network.enter_context(patch(target, side_effect=AssertionError("inference attempted network access")))

    def assert_contains_point(self, crop, x, y):
        self.assertLessEqual(crop[0], x)
        self.assertLessEqual(crop[1], y)
        self.assertGreaterEqual(crop[2], x)
        self.assertGreaterEqual(crop[3], y)

    def test_off_center_portrait_and_multiple_person_selection(self):
        path = self.folder / "basketball1.png"
        portrait = sc.suggest(path, "tight")
        self.assertEqual(portrait["status"], "ready")
        self.assertEqual(portrait["method"], "face")
        self.assert_contains_point(portrait["crop"], 88 / 640, 112 / 480)
        self.assertLess(portrait["crop"][2], .35)  # The face is at the far left.

        people = sorted(portrait["subjects"], key=lambda person: person["box"][0])
        self.assertEqual(len(people), 2)
        left = sc.suggest(path, "full-body", subject_id=people[0]["id"])
        right = sc.suggest(path, "full-body", subject_id=people[1]["id"])
        self.assertEqual(left["status"], "ready")
        self.assertEqual(right["status"], "ready")
        self.assertEqual(right["selected_subject"], people[1]["id"])
        self.assertLess(left["crop"][2], .35)
        self.assertGreater(right["crop"][0], .60)
        self.assert_contains_point(left["crop"], 100 / 640, 460 / 480)
        self.assert_contains_point(right["crop"], 560 / 640, 440 / 480)

    def test_full_body_keeps_action_pose_extremities(self):
        result = sc.suggest(self.folder / "messi5.jpg", "full-body", aspect="4:5")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["method"], "person")
        # Hand, hair, extended boot and bent-leg boot, observed in the fixture.
        for x, y in ((70, 90), (240, 70), (440, 270), (208, 329)):
            with self.subTest(point=(x, y)):
                self.assert_contains_point(result["crop"], x / 548, y / 342)

    def test_non_person_scene_never_proposes_center_crop(self):
        path = self.folder / "dog.jpg"
        for preset in ("tight", "full-body"):
            with self.subTest(preset=preset):
                result = sc.suggest(path, preset)
                self.assertEqual(result["status"], "no_subject")
                self.assertIsNone(result["crop"])
                self.assertEqual(result["subjects"], [])


if __name__ == "__main__":
    unittest.main()
