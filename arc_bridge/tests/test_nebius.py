# test_nebius.py: unit tests for the Nebius Token Factory port layer.
# No real network and no real key: a throwaway local HTTP server plays the
# Token Factory endpoint so the client's request/response handling is
# exercised end to end offline. Run: python3 -m unittest discover -s arc_bridge/tests -v
"""Unit tests for arc_bridge.nebius and its backend integration."""
# Import json for building the fake Token Factory response body.
import json  # The fake endpoint answers with OpenAI-shaped JSON.
# Import os for environment manipulation and temp paths.
import os  # Tests set and clear TOKEN_FACTORY_* variables per case.
# Import shutil for removing temp data dirs after backend tests.
import shutil  # Backend tests use throwaway data directories.
# Import sys so the tests can import the arc_bridge package.
import sys  # The repo root goes on sys.path below.
# Import tempfile for isolated backend data directories.
import tempfile  # Each backend test gets a clean data dir.
# Import threading to run the fake endpoint server beside the test.
import threading  # The fake Token Factory serves from a daemon thread.
# Import unittest for the test case framework (stdlib).
import unittest  # All tests are unittest TestCase methods.
# Import http.server for the fake Token Factory endpoint.
from http.server import BaseHTTPRequestHandler, HTTPServer  # Minimal stdlib HTTP server for the fake endpoint.

# Put the repo root on sys.path so "arc_bridge" imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # tests/ -> arc_bridge/ -> repo root.
# Import the module under test: the Token Factory client and its errors.
from arc_bridge import nebius  # noqa: E402 - path fix above must run first.
# Import the local backend for the integration assertions.
from arc_bridge.backends import LocalBackend  # noqa: E402 - path fix above must run first.

# Environment variables these tests own and restore after each case.
ENV_KEYS = ("TOKEN_FACTORY_API_KEY", "TOKEN_FACTORY_BASE_URL", "NEBIUS_MODEL")  # The full Token Factory config surface.


# Save and restore the Token Factory environment around each test.
class EnvGuard(unittest.TestCase):  # Base class: no test leaks config into the next.
    # Snapshot the environment and clear the Token Factory variables.
    def setUp(self):  # Runs before every test method in subclasses.
        # Remember the original values (None when unset).
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}  # Restore exactly what was there before.
        # Start every test from a known-unconfigured environment.
        for key in ENV_KEYS:  # Clear each owned variable in turn.
            # Remove the variable so defaults apply.
            os.environ.pop(key, None)  # pop with default never raises.
    # Restore the snapshot after the test finishes.
    def tearDown(self):  # Runs after every test method in subclasses.
        # Walk the saved snapshot and put everything back.
        for key, value in self._saved.items():  # One variable at a time.
            # Unset variables go back to unset; set ones get their value.
            if value is None:  # Originally absent.
                # Remove the variable again.
                os.environ.pop(key, None)  # Absent stays absent.
            else:  # Originally present.
                # Restore the original value.
                os.environ[key] = value  # The pre-test value returns.


# Tests for the client's configuration contract (no HTTP involved).
class ClientConfigTests(EnvGuard):  # The fail-closed configuration behavior.
    # Without a key the client reports unconfigured and names the gap.
    def test_unconfigured_status(self):  # The default state of a fresh checkout.
        # Build a client against the cleared environment.
        client = nebius.TokenFactoryClient()  # Reads env at construction time.
        # The client must report itself unconfigured.
        self.assertFalse(client.configured())  # No key, no calls: the core contract.
        # The missing list must name the API key variable exactly.
        self.assertEqual(client.missing(), ["TOKEN_FACTORY_API_KEY"])  # Operators see precisely what to set.
        # The status snapshot must carry the Nemotron default model slug.
        self.assertIn("nemotron", client.status()["model"])  # The default model is a Nemotron, per the hackathon rule.
    # chat() without configuration raises the clear error and never calls out.
    def test_chat_unconfigured_raises_clear_error(self):  # Fail-closed, no fake live calls.
        # Build a client against the cleared environment.
        client = nebius.TokenFactoryClient()  # Unconfigured by construction here.
        # The call must raise the dedicated not-configured exception.
        with self.assertRaises(nebius.TokenFactoryNotConfigured) as ctx:  # The exact exception type is part of the contract.
            # Attempt a chat call that must never reach the network.
            client.chat("system", "user")  # Would be a model call if configured.
        # The message must name the missing variable for the operator.
        self.assertIn("TOKEN_FACTORY_API_KEY", str(ctx.exception))  # Actionable errors only.
    # With env set, the client reports configured and honors overrides.
    def test_configured_from_env(self):  # Env-driven configuration, per the port brief.
        # Set a dummy key, a custom base URL, and a custom model slug.
        os.environ["TOKEN_FACTORY_API_KEY"] = "test-key-not-real"  # Dummy value; never used against the real API here.
        # Point the base URL at a dead local port (no call is made).
        os.environ["TOKEN_FACTORY_BASE_URL"] = "http://127.0.0.1:1/v1"  # Construction only reads it; nothing connects.
        # Choose a distinctive model slug to prove the override lands.
        os.environ["NEBIUS_MODEL"] = "nvidia/test-nemotron-slug"  # Any slug proves env wins over the default.
        # Build the client after the environment is set.
        client = nebius.TokenFactoryClient()  # Picks up all three variables.
        # The client must now report itself configured.
        self.assertTrue(client.configured())  # A present key flips the state.
        # The status must reflect the overridden model slug.
        self.assertEqual(client.status()["model"], "nvidia/test-nemotron-slug")  # NEBIUS_MODEL override is honored.
        # The status must reflect the overridden base URL without the key.
        self.assertEqual(client.status()["base_url"], "http://127.0.0.1:1/v1")  # Base URL override is honored; key stays secret.


# A fake Token Factory handler that records the request and answers JSON.
class FakeTokenFactoryHandler(BaseHTTPRequestHandler):  # Stands in for api.tokenfactory.nebius.com offline.
    # Silence the default stderr request logging during tests.
    def log_message(self, *args):  # Signature matches BaseHTTPRequestHandler.
        # Do nothing: test output stays readable.
        pass  # Intentionally silent.
    # Answer POST /chat/completions with an OpenAI-shaped completion.
    def do_POST(self):  # The client only ever POSTs.
        # Record the Authorization header so the test can verify the key.
        self.server.seen_auth = self.headers.get("Authorization")  # The test asserts the Bearer key was sent.
        # Read the request body length from the headers.
        length = int(self.headers.get("Content-Length") or 0)  # The client always sends a JSON body.
        # Parse the request JSON so the test can verify the model slug.
        self.server.seen_body = json.loads(self.rfile.read(length) or b"{}")  # Stored on the server object for assertions.
        # Build the OpenAI-shaped success response.
        payload = {"choices": [{"message": {"content": "Nemotron says: voice replies use Voice Monkey."}}]}  # One choice, one message, like the real API.
        # Serialize the payload to bytes.
        data = json.dumps(payload).encode("utf-8")  # The client parses this exact shape.
        # Send the 200 status line.
        self.send_response(200)  # Success, like a healthy Token Factory.
        # Declare the JSON content type.
        self.send_header("Content-Type", "application/json")  # Matches the real API's responses.
        # Declare the body length so the client can read it fully.
        self.send_header("Content-Length", str(len(data)))  # Exact byte count.
        # Finish the headers.
        self.end_headers()  # Header block ends; body follows.
        # Write the response body.
        self.wfile.write(data)  # The client reads and parses this.


# Tests for the client's HTTP behavior against the fake endpoint.
class ClientHttpTests(EnvGuard):  # Request shape, auth header, response parsing.
    # chat() sends the Nemotron slug + Bearer key and returns the text.
    def test_chat_round_trip(self):  # Full offline round trip through urllib.
        # Start the fake endpoint on an ephemeral localhost port.
        server = HTTPServer(("127.0.0.1", 0), FakeTokenFactoryHandler)  # Port 0 lets the OS pick a free port.
        # Serve requests from a daemon thread so the test can call chat().
        thread = threading.Thread(target=server.serve_forever, daemon=True)  # Daemon so a failure can never hang the suite.
        # Start serving.
        thread.start()  # The fake endpoint is live.
        # Point the client at the fake endpoint with a dummy key.
        os.environ["TOKEN_FACTORY_API_KEY"] = "test-key-not-real"  # Dummy; only ever sent to localhost here.
        # Build the base URL from the server's actual port.
        os.environ["TOKEN_FACTORY_BASE_URL"] = f"http://127.0.0.1:{server.server_address[1]}/v1"  # The client's configured root.
        # Build the client against this environment.
        client = nebius.TokenFactoryClient()  # Configured, aimed at the fake.
        # Run one chat call through the real urllib code path.
        text = client.chat("grounding rules", "How do voice replies work?")  # Exercises headers, body, parsing, cleanup.
        # Shut the fake endpoint down now that the call is done.
        server.shutdown()  # Clean teardown for the suite.
        # The returned text must be the fake completion, whitespace-normalized.
        self.assertEqual(text, "Nemotron says: voice replies use Voice Monkey.")  # Response parsing works end to end.
        # The fake must have seen the Bearer key on the wire.
        self.assertEqual(server.seen_auth, "Bearer test-key-not-real")  # Auth header shape matches OpenAI-compatible APIs.
        # The fake must have seen the default Nemotron model slug.
        self.assertIn("nemotron", server.seen_body["model"])  # The request asks for a Nemotron model by default.


# Tests for how the local backend behaves with and without the model.
class BackendIntegrationTests(EnvGuard):  # arc_ask and arc_health model reporting.
    # Build a fresh local backend in a temp dir before each test.
    def setUp(self):  # EnvGuard.setUp plus a temp data directory.
        # Run the environment guard first.
        super().setUp()  # Clears the Token Factory variables.
        # Create the isolated data directory.
        self.tmp = tempfile.mkdtemp(prefix="arc-bridge-nebius-test-")  # Backend files live here only.
        # Build the backend against the temp dir (seeds demo memory).
        self.backend = LocalBackend(self.tmp)  # The unit under test.
    # Remove the temp dir and restore the environment after each test.
    def tearDown(self):  # Mirror of setUp, in reverse.
        # Delete the temp dir and its JSONL files.
        shutil.rmtree(self.tmp, ignore_errors=True)  # Nothing leaks between tests.
        # Restore the environment snapshot.
        super().tearDown()  # EnvGuard cleanup runs last.
    # Without a key, ask answers by retrieval and says why (no fake call).
    def test_ask_unconfigured_is_honest(self):  # The demo-anywhere path stays truthful.
        # Ask about a topic the seeded demo memory covers.
        result = self.backend.ask("How do voice replies work?")  # Matches the demo.voice record.
        # The public mode must stay plain local (no Token Factory claim).
        self.assertEqual(result["mode"], "local")  # Unconfigured never advertises the model.
        # The model block must report unused with the reason attached.
        self.assertFalse(result["model"]["used"])  # Retrieval produced the answer.
        # The reason must name the missing variable.
        self.assertIn("TOKEN_FACTORY_API_KEY", result["model"]["error"])  # The caller learns exactly what to set.
        # The digest must still contain genuinely retrieved memory text.
        self.assertIn("Voice Monkey", result["digest"])  # Retrieval itself keeps working offline.
    # Health must carry the model status block for the demo and judges.
    def test_health_reports_model_status(self):  # arc_health surfaces the port's wiring.
        # Take a health snapshot from the unconfigured backend.
        snap = self.backend.health()  # The same dict the MCP tool returns.
        # The snapshot must include a model block.
        self.assertIn("model", snap)  # Judges and operators can see the wiring state.
        # The block must report unconfigured with the Nemotron default slug.
        self.assertFalse(snap["model"]["configured"])  # Honest about the missing key.
        # The slug in the block must be a Nemotron model.
        self.assertIn("nemotron", snap["model"]["model"])  # The hackathon's model rule, visible in health.


# Run the tests when this file is executed directly.
if __name__ == "__main__":  # Direct execution mirrors unittest discovery.
    # Run every test in this module with default verbosity.
    unittest.main()  # Exits nonzero on any failure, like discovery.
