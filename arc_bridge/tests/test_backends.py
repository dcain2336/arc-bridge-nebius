# test_backends.py: unit tests for LocalBackend (no network, temp data dir).
# Run from arc-bridge-mcp/: python3 -m unittest discover -s tests -v
"""Unit tests for the file-backed local backend."""
# Import json for inspecting the JSONL files the backend writes.
import json
# Import os for temp directory paths.
import os
# Import shutil for removing the temp data dir after tests.
import shutil
# Import sys so the tests can import the arc_bridge package.
import sys
# Import tempfile for an isolated data directory per test run.
import tempfile
# Import unittest for the test case framework (stdlib).
import unittest

# Put arc-bridge-mcp/ on sys.path so "arc_bridge" imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Import the backend factory and classes under test.
from arc_bridge.backends import make_backend, LocalBackend  # noqa: E402


# Tests for LocalBackend against an isolated temporary data directory.
class LocalBackendTests(unittest.TestCase):
    # Build a fresh backend in a temp dir before each test.
    def setUp(self):
        # Create an isolated temp directory for this test's data files.
        self.tmp = tempfile.mkdtemp(prefix="arc-bridge-test-")
        # Build the backend pointed at the temp directory.
        self.backend = LocalBackend(self.tmp)

    # Remove the temp directory after each test.
    def tearDown(self):
        # Delete the temp dir and everything the test wrote.
        shutil.rmtree(self.tmp, ignore_errors=True)

    # First run seeds clearly-labeled demo memory records.
    def test_seed_once(self):
        # The memory file must exist after construction.
        path = os.path.join(self.tmp, "memory.jsonl")
        # The seed step runs inside __init__.
        self.assertTrue(os.path.exists(path))
        # Every seeded record must carry the demo label.
        with open(path, encoding="utf-8") as handle:
            # Parse each seeded line.
            rows = [json.loads(line) for line in handle if line.strip()]
        # Three demo records are seeded.
        self.assertEqual(len(rows), 3)
        # All of them are labeled demo.
        self.assertTrue(all(r.get("demo") for r in rows))
        # Constructing a second backend must not duplicate the seed.
        LocalBackend(self.tmp)
        # Re-read the file after the second construction.
        with open(path, encoding="utf-8") as handle:
            # Parse each line again.
            rows2 = [json.loads(line) for line in handle if line.strip()]
        # Still exactly three records: seeding ran once.
        self.assertEqual(len(rows2), 3)

    # arc_ask stores the question and returns a retrieval digest.
    def test_ask_and_queue_status(self):
        # Ask a question matching the seeded demo memory.
        result = self.backend.ask("How do voice replies work?")
        # The result must carry a queue id.
        self.assertTrue(result["queue_id"])
        # Local mode answers synchronously.
        self.assertEqual(result["status"], "answered")
        # The mode label must be honest.
        self.assertEqual(result["mode"], "local")
        # The digest should mention Voice Monkey (from the demo record).
        self.assertIn("Voice Monkey", result["digest"])
        # queue_status must find the record by its id.
        record = self.backend.queue_status(result["queue_id"])
        # The record exists.
        self.assertIsNotNone(record)
        # The stored question matches what was asked.
        self.assertEqual(record["question"], "How do voice replies work?")
        # Unknown ids return None.
        self.assertIsNone(self.backend.queue_status("q-does-not-exist"))

    # arc_ask refuses empty questions instead of queueing noise.
    def test_ask_empty_rejected(self):
        # Empty questions raise ValueError (becomes an MCP isError).
        with self.assertRaises(ValueError):
            # Whitespace-only counts as empty.
            self.backend.ask("   ")

    # memory_search ranks keyword matches and honors the limit.
    def test_memory_search(self):
        # Search for a word present in the demo records.
        result = self.backend.memory_search("telegram notify", limit=5)
        # At least one match is expected.
        self.assertTrue(result["matches"])
        # The mode label must be honest.
        self.assertEqual(result["mode"], "local")
        # A query with no usable words matches nothing.
        empty = self.backend.memory_search("a b")
        # No matches for the degenerate query.
        self.assertEqual(empty["matches"], [])

    # health reports a live harness and the real queue depth.
    def test_health(self):
        # Ask one question first so the queue is non-empty.
        self.backend.ask("test question")
        # Take the health snapshot.
        snap = self.backend.health()
        # The worker label identifies the local harness.
        self.assertEqual(snap["worker"], "local-harness")
        # Heartbeat age is zero for the inline harness.
        self.assertEqual(snap["heartbeat_age_s"], 0)
        # Local asks are answered immediately, so depth is zero.
        self.assertEqual(snap["queue_depth"], 0)
        # Three seeded memory records are counted.
        self.assertEqual(snap["memory_records"], 3)

    # notify appends to the alerts inbox and reports delivery honestly.
    def test_notify(self):
        # Send a notification through the log channel.
        result = self.backend.notify("Test", "hello inbox", channel="log")
        # The log channel always delivers locally.
        self.assertTrue(result["delivered"]["log"])
        # Non-log channels are honestly reported as undelivered locally.
        self.assertFalse(result["delivered"]["echo"])
        # The alerts file must contain the record.
        path = os.path.join(self.tmp, "alerts.jsonl")
        # Read the inbox file.
        with open(path, encoding="utf-8") as handle:
            # Parse each line.
            rows = [json.loads(line) for line in handle if line.strip()]
        # One alert was recorded.
        self.assertEqual(len(rows), 1)
        # The title survived the round trip.
        self.assertEqual(rows[0]["title"], "Test")
        # Empty titles raise ValueError.
        with self.assertRaises(ValueError):
            # Title is required.
            self.backend.notify("", "message")

    # make_backend selects local by default and live on request.
    def test_make_backend_selector(self):
        # Default (no env) is the local backend.
        backend = make_backend(self.tmp)
        # The instance must be a LocalBackend.
        self.assertIsInstance(backend, LocalBackend)
        # ARC_BRIDGE_BACKEND=live selects the live backend.
        os.environ["ARC_BRIDGE_BACKEND"] = "live"
        # Rebuild with the env var set.
        try:
            # Import here to avoid polluting the module namespace.
            from arc_bridge.backends import LiveBackend
            # The factory must return a LiveBackend now.
            self.assertIsInstance(make_backend(self.tmp), LiveBackend)
        # Always restore the environment.
        finally:
            # Remove the selector so other tests see the default.
            del os.environ["ARC_BRIDGE_BACKEND"]


# Run the suite when executed directly.
if __name__ == "__main__":
    # unittest main discovers the TestCase classes above.
    unittest.main()
