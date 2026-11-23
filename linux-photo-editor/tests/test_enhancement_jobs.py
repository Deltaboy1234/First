"""Background AI work stays bounded, cancellable, and separate from recipes."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

from fieldroom.enhancement_jobs import EnhancementJobs
from fieldroom.library import Session


class EnhancementJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "Original.jpg"
        Image.new("RGB", (40, 30), "orange").save(self.path)
        session = Session()
        identifier = session.browse(self.path.parent)["photos"][0]["id"]
        self.photo = session.get(identifier)
        self.original = self.path.read_bytes()

    def terminal(self, jobs, identifier):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = jobs.progress(identifier)
            if result["state"] in ("ready", "cancelled", "error"):
                return result
            time.sleep(.005)
        self.fail("The AI worker did not reach a terminal state.")

    def test_one_worker_progress_and_ready_do_not_change_original_or_recipe(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def prepare(path, revision, denoise, sharpen, *, progress, cancelled):
            calls.append((path, revision, denoise, sharpen))
            progress(.4, "Processing detail tiles.")
            started.set()
            release.wait(2)
            progress(1, "Prepared.")
        jobs = EnhancementJobs(prepare)
        self.addCleanup(release.set)
        self.addCleanup(jobs.close)
        identifier = jobs.start(self.photo, 25, 45)["job"]
        self.assertTrue(started.wait(1))
        running = jobs.progress(identifier)
        self.assertEqual(running["state"], "running")
        self.assertEqual(running["progress"], .4)
        with self.assertRaisesRegex(ValueError, "still running"):
            jobs.start(self.photo, 10, 20)
        release.set()
        result = self.terminal(jobs, identifier)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["progress"], 1)
        self.assertNotIn("recipe", result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2:], (25, 45))
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_cancellation_is_cooperative_and_allows_next_job_after_worker_stops(self):
        started = threading.Event()
        def prepare(path, revision, denoise, sharpen, *, progress, cancelled):
            started.set()
            while not cancelled():
                time.sleep(.005)
            raise RuntimeError("Stopped at tile boundary.")
        jobs = EnhancementJobs(prepare)
        self.addCleanup(jobs.close)
        identifier = jobs.start(self.photo, 30, 0)["job"]
        self.assertTrue(started.wait(1))
        self.assertTrue(jobs.cancel(identifier)["cancel_requested"])
        result = self.terminal(jobs, identifier)
        self.assertEqual(result["state"], "cancelled")
        self.assertNotIn("error", result)
        self.assertNotIn("recipe", result)
        self.assertEqual(self.path.read_bytes(), self.original)
        jobs.prepare = lambda *args, **kwargs: None
        next_id = jobs.start(self.photo, 0, 10)["job"]
        self.assertEqual(self.terminal(jobs, next_id)["state"], "ready")

    def test_changed_source_after_processing_is_an_error_not_ready(self):
        def prepare(*args, **kwargs):
            Image.new("RGB", (60, 50), "blue").save(self.path)
        jobs = EnhancementJobs(prepare)
        result = self.terminal(jobs, jobs.start(self.photo, 20, 30)["job"])
        self.assertEqual(result["state"], "error")
        self.assertIn("changed", result["error"])
        self.assertNotIn("recipe", result)

    def test_model_failure_is_reported_without_changing_edits(self):
        def prepare(*args, **kwargs):
            raise RuntimeError("The local model is missing.")
        jobs = EnhancementJobs(prepare)
        result = self.terminal(jobs, jobs.start(self.photo, 20, 0)["job"])
        self.assertEqual(result["state"], "error")
        self.assertIn("model is missing", result["error"])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_invalid_strengths_never_start_processing(self):
        calls = []
        jobs = EnhancementJobs(lambda *args, **kwargs: calls.append(args))
        for value in (-1, 101, float("nan"), float("inf"), True, "20"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                jobs.start(self.photo, value, 0)
        self.assertEqual(calls, [])

    def test_completed_history_is_bounded_and_close_cancels_active_work(self):
        jobs = EnhancementJobs(lambda *args, **kwargs: None)
        identifiers = []
        for _ in range(10):
            identifier = jobs.start(self.photo, 0, 10)["job"]
            identifiers.append(identifier)
            self.assertEqual(self.terminal(jobs, identifier)["state"], "ready")
        with self.assertRaises(ValueError):
            jobs.progress(identifiers[0])
        self.assertEqual(jobs.progress(identifiers[-1])["state"], "ready")
        def prepare(*args, cancelled, **kwargs):
            while not cancelled():
                time.sleep(.005)
        jobs.prepare = prepare
        identifier = jobs.start(self.photo, 30, 0)["job"]
        jobs.close()
        self.assertEqual(self.terminal(jobs, identifier)["state"], "cancelled")
        with self.assertRaisesRegex(ValueError, "closing"):
            jobs.start(self.photo, 30, 0)


if __name__ == "__main__":
    unittest.main()
