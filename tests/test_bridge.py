from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from bridge import Bridge
from machine import HttpError
from preflight import check_prompt
import wake


def standard_prompt():
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "test.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": "test"}},
        "3": {"class_type": "EmptyLatentImage", "inputs": {"width": 64, "height": 64, "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["2", 0], "latent_image": ["3", 0],
            "seed": 0, "steps": 1, "cfg": 1, "sampler_name": "euler", "scheduler": "simple", "denoise": 1,
        }},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "PreviewImage", "inputs": {"images": ["6", 0]}},
    }


class BridgeRegressionTests(unittest.TestCase):
    def setUp(self):
        self.work = TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        checkpoint_dir = self.root / "ComfyUI/models/checkpoints"
        checkpoint_dir.mkdir(parents=True)
        (checkpoint_dir / "test.safetensors").touch()
        self.machine = Mock()
        self.bridge = Bridge({"comfy_root": str(self.root), "poll_seconds": 0}, self.root / "data", self.machine)

    def test_old_prompt_errors_are_caught_before_handoff(self):
        graph = standard_prompt()
        graph["1"]["inputs"]["ckpt_name"] = "missing.safetensors"
        graph["2"]["inputs"]["clip"] = ["1", 0]
        graph["5"]["inputs"]["seed"] = -1
        del graph["5"]["inputs"]["denoise"]
        with self.assertRaises(HttpError) as rejection:
            self.bridge.submit(graph)
        self.assertEqual(rejection.exception.status, 422)
        problems = rejection.exception.body["tasks"][0]["node_errors"]
        self.assertEqual(
            [(error["node"], error["input"]) for error in problems],
            [("1", "ckpt_name"), ("2", "clip"), ("5", "seed"), ("5", "denoise")],
        )
        self.assertFalse(self.bridge.jobs)
        self.assertIsNone(self.bridge.active)
        self.assertEqual(self.machine.mock_calls, [])

    def test_success_requires_history_even_when_queue_first_reports_empty(self):
        public, _ = self.bridge.submit(standard_prompt())
        job = self.bridge.jobs[public["id"]]
        self.bridge.update_active(batch_ids=[])
        process = Mock()
        process.poll.return_value = None
        prompt_id = job["tasks"][0]["prompt_id"]
        results = iter([
            ("/prompt", {"prompt_id": prompt_id}),
            (f"/history/{prompt_id}", {}),
            ("/queue", {"queue_running": [], "queue_pending": []}),
            (f"/history/{prompt_id}", {}),
            ("/queue", {"queue_running": [[0, prompt_id]], "queue_pending": []}),
            (f"/history/{prompt_id}", {prompt_id: {
                "status": {"status_str": "success", "completed": True},
                "outputs": {"7": {"images": [{"filename": "test.png"}]}},
            }}),
        ])

        def comfy(route, *args):
            expected, value = next(results)
            self.assertEqual(route, expected)
            return value

        self.bridge.comfy = comfy
        self.bridge.run_job(job, process)
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["tasks"][0]["outputs"]["7"]["images"][0]["filename"], "test.png")

    def test_runtime_rejection_is_reported_as_not_queued(self):
        graph = standard_prompt()
        public, _ = self.bridge.submit({"request_id": str(uuid4()), "prompts": [graph]})
        job = self.bridge.jobs[public["id"]]
        self.bridge.update_active(batch_ids=[])
        self.bridge.comfy = Mock(side_effect=HttpError(400, {"error": {"message": "Rejected by ComfyUI"}}))
        self.bridge.run_job(job, Mock())
        self.assertEqual(job["state"], "rejected")
        self.assertEqual(job["tasks"][0]["detail"], "Rejected by ComfyUI")

    def test_unused_bad_node_does_not_block_a_valid_output(self):
        graph = standard_prompt()
        graph["8"] = {"class_type": "KSampler", "inputs": {}}
        self.assertEqual(check_prompt({"prompt": graph}, {}, ["test.safetensors"]), [])

    def test_wake_confirms_first_message_from_desktop_gui(self):
        path = self.root / "state.db"
        with sqlite3.connect(path) as db:
            db.executescript(
                """CREATE TABLE sessions (id TEXT, source TEXT, started_at REAL);
                   CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT);
                   INSERT INTO sessions VALUES ('existing', 'gui', 101), ('fresh', 'gui', 102);
                   INSERT INTO messages VALUES
                      (1, 'existing', 'user', 'earlier'),
                      (2, 'existing', 'user', '醒来'),
                      (3, 'fresh', 'user', '醒来');"""
            )
        db.close()
        with patch("wake.hermes_db", return_value=path):
            self.assertEqual(wake.confirmation(1, 100), "fresh")
            self.assertIsNone(wake.confirmation(3, 100))


if __name__ == "__main__":
    unittest.main()
