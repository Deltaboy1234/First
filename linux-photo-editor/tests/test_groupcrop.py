"""Regression cases for cropping all or selected detected people together."""
import unittest
from unittest.mock import patch

from fieldroom import imaging as im
from fieldroom import smartcrop as sc


class GroupCropTests(unittest.TestCase):
    def analysis(self, *detections, size=(6000, 4000)):
        return sc.Analysis(size=size, detections=tuple(
            sc.Detection(kind=kind, box=box, confidence=confidence)
            for kind, box, confidence in detections
        ))

    def two_people(self):
        return self.analysis(
            ("person", (.06, .08, .31, .94), .96),
            ("person", (.67, .16, .94, .92), .95),
            ("face", (.12, .18, .22, .34), .97),
            ("face", (.74, .25, .84, .42), .96),
        )

    def three_people(self):
        return self.analysis(
            ("person", (.05, .10, .22, .91), .96),
            ("person", (.35, .12, .52, .91), .95),
            ("person", (.79, .10, .96, .92), .95),
            ("face", (.09, .18, .18, .34), .97),
            ("face", (.40, .22, .47, .37), .96),
            ("face", (.84, .19, .92, .34), .96),
        )

    def individuals(self, result):
        return [s for s in result["subjects"] if s["id"] != "all"]

    def assert_contains(self, crop, box, tolerance=2e-6):
        self.assertIsNotNone(crop)
        im.recipe({"crop": crop})
        self.assertLessEqual(crop[0], box[0] + tolerance)
        self.assertLessEqual(crop[1], box[1] + tolerance)
        self.assertGreaterEqual(crop[2], box[2] - tolerance)
        self.assertGreaterEqual(crop[3], box[3] - tolerance)

    def test_group_is_offered_without_changing_individual_selection(self):
        analysis = self.two_people()
        initial = sc.suggest_from_analysis(analysis)
        group = [s for s in initial["subjects"] if s["id"] == "all"]
        self.assertEqual(len(group), 1)
        self.assertEqual(group[0]["kind"], "group")
        self.assertIn("All people", group[0]["label"])
        individual = self.individuals(initial)
        self.assertEqual([s["id"] for s in individual], ["person-1", "person-2"])
        self.assertNotEqual(initial["selected_subject"], "all")
        for person in individual:
            result = sc.suggest_from_analysis(analysis, subject_id=person["id"])
            self.assertEqual(result["selected_subject"], person["id"])
            self.assertLess(result["crop"][2] - result["crop"][0], .35)

    def test_group_encloses_both_individual_framings_for_every_preset(self):
        analysis = self.two_people()
        members = self.individuals(sc.suggest_from_analysis(analysis))
        for preset in sc.PRESETS:
            with self.subTest(preset=preset):
                group = sc.suggest_from_analysis(analysis, preset, subject_id="all")
                self.assertEqual(group["status"], "ready")
                self.assertEqual(group["selected_subject"], "all")
                for member in members:
                    single = sc.suggest_from_analysis(analysis, preset, subject_id=member["id"])
                    self.assert_contains(group["crop"], single["crop"])

    def test_full_body_keeps_edge_people_when_aspect_cannot_fit(self):
        left, right = (0., .03, .30, .97), (.70, .01, 1., 1.)
        analysis = self.analysis(("person", left, .95), ("person", right, .94))
        for aspect in ("Free", "Original", "1:1", "4:5", "3:2", "16:9"):
            with self.subTest(aspect=aspect):
                group = sc.suggest_from_analysis(analysis, "full-body", subject_id="all", aspect=aspect)
                self.assertEqual(group["status"], "ready")
                self.assert_contains(group["crop"], left)
                self.assert_contains(group["crop"], right)
                if aspect == "4:5":
                    self.assertEqual(group["aspect"], "Free")
                    self.assertTrue(group["warning"])

    def test_portrait_group_includes_person_with_hidden_face_and_explains_fallback(self):
        left_face, right_body = (.12, .18, .22, .34), (.67, .16, .94, .92)
        analysis = self.analysis(
            ("person", (.06, .08, .31, .94), .96),
            ("person", right_body, .95),
            ("face", left_face, .97),
        )
        for preset in ("very-tight", "tight", "normal", "loose"):
            with self.subTest(preset=preset):
                result = sc.suggest_from_analysis(analysis, preset, subject_id="all")
                self.assertEqual(result["status"], "ready")
                self.assert_contains(result["crop"], left_face)
                self.assert_contains(result["crop"], right_body)
                self.assertTrue(result["warning"])
                self.assertIn("face", result["warning"].lower())

    def test_full_body_group_does_not_invent_outline_for_face_only_child(self):
        analysis = self.analysis(
            ("person", (.08, .08, .90, .98), .97),
            ("face", (.20, .15, .40, .35), .97),
            ("face", (.60, .38, .72, .52), .96),
        )
        portrait = sc.suggest_from_analysis(analysis, subject_id="all")
        self.assertEqual(portrait["status"], "ready")
        self.assertEqual(len(self.individuals(portrait)), 2)
        self.assert_contains(portrait["crop"], (.20, .15, .40, .35))
        self.assert_contains(portrait["crop"], (.60, .38, .72, .52))
        body = sc.suggest_from_analysis(analysis, "full-body", subject_id="all")
        self.assertEqual(body["status"], "person_not_detected")
        self.assertIsNone(body["crop"])
        self.assertEqual(body["selected_subject"], "all")
        self.assertTrue(body["warning"])

    def test_group_requires_two_reliable_people_and_ignores_weak_detections(self):
        one = self.analysis(
            ("person", (.08, .10, .40, .94), .95),
            ("face", (.18, .20, .29, .38), .96),
            ("person", (.68, .06, .96, .97), .60),
            ("face", (.74, .12, .82, .24), .80),
        )
        for analysis in (self.analysis(), one):
            with self.subTest(count=len(analysis.detections)):
                result = sc.suggest_from_analysis(analysis)
                self.assertNotIn("all", [s["id"] for s in result["subjects"]])
                with self.assertRaises(ValueError):
                    sc.suggest_from_analysis(analysis, subject_id="all")

    def test_group_respects_requested_aspect_when_everyone_fits(self):
        left, right = (.25, .25, .37, .65), (.58, .25, .70, .65)
        analysis = self.analysis(("person", left, .95), ("person", right, .94))
        result = sc.suggest_from_analysis(analysis, "full-body", subject_id="all", aspect="4:5")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["aspect"], "4:5")
        self.assert_contains(result["crop"], left)
        self.assert_contains(result["crop"], right)
        l, t, r, b = result["crop"]
        self.assertAlmostEqual((r-l)*6000 / ((b-t)*4000), 4/5, places=4)

    def test_selected_pair_excludes_third_person_and_keeps_all_choices(self):
        analysis = self.three_people()
        initial = sc.suggest_from_analysis(analysis)
        expected_choices = [s["id"] for s in initial["subjects"]]
        for preset in sc.PRESETS:
            with self.subTest(preset=preset):
                result = sc.suggest_from_analysis(
                    analysis, preset, subject_ids=["person-2", "person-1"])
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["method"], "group")
                self.assertEqual(result["selected_subject"], "group")
                self.assertEqual(result["selected_subjects"], ["person-1", "person-2"])
                self.assertEqual([s["id"] for s in result["subjects"]], expected_choices)
                self.assertLess(result["crop"][2], .79)
                for subject_id in ("person-1", "person-2"):
                    single = sc.suggest_from_analysis(analysis, preset, subject_id=subject_id)
                    self.assert_contains(result["crop"], single["crop"])
        # Exercise the public file API too: selected IDs must reach framing intact.
        with patch.object(sc, "analyze", return_value=analysis):
            result = sc.suggest("unused.jpg", subject_ids=["person-2", "person-1"])
        self.assertEqual(result["selected_subjects"], ["person-1", "person-2"])
        self.assertLess(result["crop"][2], .79)

    def test_unselected_face_only_person_cannot_block_full_body_subset(self):
        analysis = self.analysis(
            ("person", (.05, .10, .22, .91), .96),
            ("person", (.35, .12, .52, .91), .95),
            ("face", (.09, .18, .18, .34), .97),
            ("face", (.40, .22, .47, .37), .96),
            ("face", (.84, .19, .92, .34), .96),
        )
        all_people = sc.suggest_from_analysis(analysis, "full-body", subject_id="all")
        self.assertEqual(all_people["status"], "person_not_detected")
        result = sc.suggest_from_analysis(
            analysis, "full-body", subject_ids=["person-1", "person-2"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["selected_subject"], "group")
        self.assertEqual(result["selected_subjects"], ["person-1", "person-2"])
        self.assert_contains(result["crop"], (.05, .10, .22, .91))
        self.assert_contains(result["crop"], (.35, .12, .52, .91))
        self.assertLess(result["crop"][2], .79)
        self.assertIn("face-1", [s["id"] for s in result["subjects"]])

    def test_single_selected_id_matches_legacy_individual_behavior(self):
        analysis = self.three_people()
        for preset in sc.PRESETS:
            with self.subTest(preset=preset):
                legacy = sc.suggest_from_analysis(analysis, preset, subject_id="person-2")
                selected = sc.suggest_from_analysis(analysis, preset, subject_ids=["person-2"])
                self.assertEqual(selected["status"], legacy["status"])
                self.assertEqual(selected["crop"], legacy["crop"])
                self.assertEqual(selected["method"], legacy["method"])
                self.assertEqual(selected["selected_subject"], "person-2")
                self.assertEqual(selected["selected_subjects"], ["person-2"])
                self.assertEqual(legacy["selected_subjects"], ["person-2"])

    def test_selecting_all_ids_matches_all_people_and_canonicalizes_order(self):
        analysis = self.three_people()
        expected = ["person-1", "person-2", "person-3"]
        legacy = sc.suggest_from_analysis(analysis, "full-body", subject_id="all")
        result = sc.suggest_from_analysis(
            analysis, "full-body", subject_ids=list(reversed(expected)))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["crop"], legacy["crop"])
        self.assertEqual(result["selected_subject"], "all")
        self.assertEqual(result["selected_subjects"], expected)
        self.assertEqual(legacy["selected_subjects"], expected)

    def test_invalid_subsets_are_rejected_without_silently_dropping_members(self):
        analysis = self.three_people()
        invalid = ([], ["person-1", "person-1"], ["not-a-subject"],
                   ["person-1", "not-a-subject"], ["all"], ["group"],
                   ["person-1", "all"], [None], [3], [""], "person-1", {})
        for ids in invalid:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                sc.suggest_from_analysis(analysis, subject_ids=ids)
        for legacy in ("person-1", "all"):
            with self.subTest(legacy=legacy), self.assertRaises(ValueError):
                sc.suggest_from_analysis(
                    analysis, subject_id=legacy, subject_ids=["person-1", "person-2"])


if __name__ == "__main__":
    unittest.main()
