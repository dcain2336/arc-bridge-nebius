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
ENV_KEYS = ("TOKEN_FACTORY_API_KEY", "TOKEN_FACTORY_BASE_URL", "NEBIUS_MODEL",  # The direct Token Factory config surface.
            "PORTKEY_API_KEY", "PORTKEY_VIRTUAL_KEY", "PORTKEY_BASE_URL",  # The Portkey gateway config surface.
            "ARC_BRIDGE_SPEND_CAP_USD", "ARC_BRIDGE_SPEND_STATE_FILE",  # The spend-cap config surface.
            "ARC_BRIDGE_PRICE_PER_1M_USD")  # The spend-estimate price ceiling.


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
        # Point the spend-state file at a throwaway path for this test.
        self.spend_path = os.path.join(tempfile.gettempdir(),  # System temp dir, never the operator's home.
                                       f"arc-bridge-spend-test-{os.getpid()}-{id(self)}.json")  # Unique per test case.
        # Set the state path so no test ever writes the real home file.
        os.environ["ARC_BRIDGE_SPEND_STATE_FILE"] = self.spend_path  # Clients under test meter into this file only.
    # Restore the snapshot after the test finishes.
    def tearDown(self):  # Runs after every test method in subclasses.
        # Remove the throwaway spend-state file when one was created.
        try:  # The file may not exist when no metered call ran.
            # Delete it quietly.
            os.remove(self.spend_path)  # No test residue on disk.
        # Missing files are the normal case here.
        except OSError:  # File never created.
            # Nothing to clean up.
            pass  # Continue to the environment restore.
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
        self.assertIn("nemotron", client.status()["model"].lower())  # The default model is a Nemotron, per the hackathon rule (case-insensitive: the live API slug uses a capital N).
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
        # Record every header so Portkey-mode tests can verify the gateway headers.
        self.server.seen_headers = dict(self.headers)  # Header names keep their sent casing here.
        # Read the request body length from the headers.
        length = int(self.headers.get("Content-Length") or 0)  # The client always sends a JSON body.
        # Parse the request JSON so the test can verify the model slug.
        self.server.seen_body = json.loads(self.rfile.read(length) or b"{}")  # Stored on the server object for assertions.
        # Build the OpenAI-shaped success response.
        payload = {"choices": [{"message": {"content": "Nemotron says: voice replies use Voice Monkey."}}]}  # One choice, one message, like the real API.
        # Attach the usage block unless the test explicitly disabled it.
        usage = getattr(self.server, "usage", {"prompt_tokens": 100, "completion_tokens": 50})  # Default usage: 150 tokens per call.
        # None means "no usage block" so the fallback estimate is exercised.
        if usage is not None:  # Only attach when the test wants usage present.
            # Add the usage block to the payload.
            payload["usage"] = usage  # The client meters from this block.
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
        self.assertIn("nemotron", server.seen_body["model"].lower())  # The request asks for a Nemotron model by default (case-insensitive: the live API slug uses a capital N).


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
        self.assertIn("nemotron", snap["model"]["model"].lower())  # The hackathon's model rule, visible in health (case-insensitive: the live API slug uses a capital N).


# Tests for the Portkey gateway configuration contract (no HTTP involved).
class PortkeyConfigTests(EnvGuard):  # Gateway selection and missing-piece naming.
    # Both Portkey keys present means Portkey mode, even with a direct key set.
    def test_portkey_mode_wins_when_both_keys_set(self):  # The revocable-key route is preferred for a public demo.
        # Set both Portkey keys plus a direct key to prove precedence.
        os.environ["PORTKEY_API_KEY"] = "pk-test-key-not-real"  # Dummy workspace key.
        # Set the virtual key for the Token Factory integration.
        os.environ["PORTKEY_VIRTUAL_KEY"] = "vk-test-key-not-real"  # Dummy virtual key.
        # Set a direct key too, so the test proves Portkey wins.
        os.environ["TOKEN_FACTORY_API_KEY"] = "direct-test-key-not-real"  # Dummy direct key.
        # Build the client against this environment.
        client = nebius.TokenFactoryClient()  # Reads all three variables.
        # The client must report itself configured.
        self.assertTrue(client.configured())  # A complete Portkey route configures the client.
        # The gateway must be Portkey, not direct.
        self.assertEqual(client.gateway, "portkey")  # Portkey wins when fully configured.
        # The status base URL must be the Portkey gateway root.
        self.assertEqual(client.status()["base_url"], "https://api.portkey.ai/v1")  # Gateway root, never the direct root here.
        # Nothing must be missing.
        self.assertEqual(client.missing(), [])  # Fully configured means no gaps.
    # A lone Portkey API key names the missing virtual key exactly.
    def test_partial_portkey_api_key_names_virtual_key(self):  # Half a Portkey route is unconfigured, with a precise gap.
        # Set only the Portkey API key.
        os.environ["PORTKEY_API_KEY"] = "pk-test-key-not-real"  # Dummy workspace key, no virtual key.
        # Build the client against this environment.
        client = nebius.TokenFactoryClient()  # Sees the partial route.
        # The client must report itself unconfigured.
        self.assertFalse(client.configured())  # One Portkey key is not a complete route.
        # The missing list must name the virtual key variable exactly.
        self.assertEqual(client.missing(), ["PORTKEY_VIRTUAL_KEY"])  # Operators see precisely what to set.
    # A lone Portkey virtual key names the missing API key exactly.
    def test_partial_portkey_virtual_key_names_api_key(self):  # Mirror case of the previous test.
        # Set only the Portkey virtual key.
        os.environ["PORTKEY_VIRTUAL_KEY"] = "vk-test-key-not-real"  # Dummy virtual key, no workspace key.
        # Build the client against this environment.
        client = nebius.TokenFactoryClient()  # Sees the partial route.
        # The client must report itself unconfigured.
        self.assertFalse(client.configured())  # One Portkey key is not a complete route.
        # The missing list must name the API key variable exactly.
        self.assertEqual(client.missing(), ["PORTKEY_API_KEY"])  # Operators see precisely what to set.


# Tests for Portkey-mode HTTP behavior and spend metering (fake endpoint).
class PortkeyHttpTests(EnvGuard):  # Gateway headers, usage metering, fallback estimate.
    # Start a fake endpoint aimed at by PORTKEY_BASE_URL for one test.
    def _start_fake(self):  # Shared setup: server, thread, and env pointing at it.
        # Start the fake endpoint on an ephemeral localhost port.
        server = HTTPServer(("127.0.0.1", 0), FakeTokenFactoryHandler)  # Port 0 lets the OS pick a free port.
        # Serve requests from a daemon thread so the test can call chat().
        thread = threading.Thread(target=server.serve_forever, daemon=True)  # Daemon so a failure can never hang the suite.
        # Start serving.
        thread.start()  # The fake endpoint is live.
        # Point the Portkey gateway override at the fake endpoint.
        os.environ["PORTKEY_BASE_URL"] = f"http://127.0.0.1:{server.server_address[1]}/v1"  # The test-only gateway override.
        # Set both Portkey keys so the client routes through the gateway.
        os.environ["PORTKEY_API_KEY"] = "pk-test-key-not-real"  # Dummy workspace key.
        # Set the virtual key for the fake integration.
        os.environ["PORTKEY_VIRTUAL_KEY"] = "vk-test-key-not-real"  # Dummy virtual key.
        # Hand the server back for header and state assertions.
        return server  # The test shuts it down after the call.
    # chat() in Portkey mode sends the two gateway headers, never Bearer.
    def test_portkey_headers_sent(self):  # Gateway auth shape, verified on the wire.
        # Start the fake endpoint and configure Portkey mode.
        server = self._start_fake()  # Server live, env pointed at it.
        # Build the client against this environment.
        client = nebius.TokenFactoryClient()  # Portkey mode by construction here.
        # Run one chat call through the real urllib code path.
        text = client.chat("grounding rules", "How do voice replies work?")  # Exercises the gateway header branch.
        # Shut the fake endpoint down now that the call is done.
        server.shutdown()  # Clean teardown for the suite.
        # The returned text must be the fake completion.
        self.assertEqual(text, "Nemotron says: voice replies use Voice Monkey.")  # Response parsing works in gateway mode too.
        # Lowercase the seen headers so casing differences cannot hide a miss.
        headers = {k.lower(): v for k, v in server.seen_headers.items()}  # Normalized for robust assertions.
        # The gateway workspace key header must carry the dummy value.
        self.assertEqual(headers.get("x-portkey-api-key"), "pk-test-key-not-real")  # Workspace auth header present.
        # The gateway virtual key header must carry the dummy value.
        self.assertEqual(headers.get("x-portkey-virtual-key"), "vk-test-key-not-real")  # Provider routing header present.
        # No direct Bearer header may be sent in gateway mode.
        self.assertNotIn("authorization", headers)  # Gateway mode never leaks the direct key header.
    # Provider usage blocks meter tokens and spend into the state file.
    def test_usage_metered_to_state_file(self):  # 100 + 50 tokens at $1 per million is $0.00015.
        # Start the fake endpoint and configure Portkey mode.
        server = self._start_fake()  # Default fake usage: 100 prompt + 50 completion tokens.
        # Build the client against this environment.
        client = nebius.TokenFactoryClient()  # Portkey mode by construction here.
        # Run one chat call through the real urllib code path.
        client.chat("grounding rules", "How do voice replies work?")  # Meters 150 tokens on success.
        # Shut the fake endpoint down now that the call is done.
        server.shutdown()  # Clean teardown for the suite.
        # Read the state file the client wrote.
        with open(self.spend_path, "r", encoding="utf-8") as handle:  # The guard-provided throwaway path.
            # Parse the state JSON.
            state = json.load(handle)  # Running totals after one call.
        # Tokens must equal the fake usage sum.
        self.assertEqual(state["tokens"], 150)  # 100 prompt + 50 completion.
        # Requests must count the single call.
        self.assertEqual(state["requests"], 1)  # One completed call.
        # Spend must equal 150 tokens at the $1 per million default ceiling.
        self.assertAlmostEqual(state["estimated_spend_usd"], 0.00015, places=6)  # 150 / 1,000,000 * $1.00.
    # A missing usage block meters the conservative fallback estimate.
    def test_missing_usage_uses_fallback_estimate(self):  # No usage data must never meter zero.
        # Start the fake endpoint and configure Portkey mode.
        server = self._start_fake()  # Server live, env pointed at it.
        # Disable the usage block so the fallback path is exercised.
        server.usage = None  # The fake omits usage when this is None.
        # Build the client against this environment.
        client = nebius.TokenFactoryClient()  # Portkey mode by construction here.
        # Run one chat call through the real urllib code path.
        client.chat("grounding rules", "How do voice replies work?")  # No usage block arrives.
        # Shut the fake endpoint down now that the call is done.
        server.shutdown()  # Clean teardown for the suite.
        # Read the state file the client wrote.
        with open(self.spend_path, "r", encoding="utf-8") as handle:  # The guard-provided throwaway path.
            # Parse the state JSON.
            state = json.load(handle)  # Running totals after one call.
        # Tokens must equal the fallback per-call estimate.
        self.assertEqual(state["tokens"], 4000)  # FALLBACK_TOKENS_PER_CALL, not zero.


# Tests for the server-side spend cap (no real spend, fake endpoint only).
class SpendCapTests(EnvGuard):  # Cap refusal before network, and accumulate-then-cap.
    # Write a spend-state file with the given totals for the test client.
    def _write_state(self, spend, requests, tokens):  # Pre-seeds the meter before the call.
        # Build the state object in the tracker's on-disk shape.
        state = {"estimated_spend_usd": spend, "requests": requests, "tokens": tokens}  # Running totals as the tracker stores them.
        # Write it to the guard-provided throwaway path.
        with open(self.spend_path, "w", encoding="utf-8") as handle:  # The client under test reads this file.
            # Serialize the state as JSON.
            json.dump(state, handle)  # Pre-seeded totals.
    # At the cap, chat() refuses before any network activity happens.
    def test_cap_refuses_before_network(self):  # $5.00 spent of a $5.00 cap must refuse, not call.
        # Pre-seed the meter exactly at the default $5 cap.
        self._write_state(5.00, 3, 999)  # At-cap state.
        # Configure a direct key aimed at a dead port (a call would fail differently).
        os.environ["TOKEN_FACTORY_API_KEY"] = "test-key-not-real"  # Dummy direct key.
        # Point at a dead local port so any attempted call errors distinctly.
        os.environ["TOKEN_FACTORY_BASE_URL"] = "http://127.0.0.1:1/v1"  # No server listens here.
        # Build the client against this environment.
        client = nebius.TokenFactoryClient()  # Direct mode, at-cap meter.
        # The call must raise the spend-cap error, not a network error.
        with self.assertRaises(nebius.TokenFactoryError) as ctx:  # Cap refusal is a TokenFactoryError, like other call failures.
            # Attempt a chat call that must never reach the network.
            client.chat("system", "user")  # Refused by the cap check before urlopen.
        # The message must name the cap so the backend fallback text is honest.
        self.assertIn("cap", str(ctx.exception))  # "Spend cap reached" is the refusal text.
    # Spend accumulates across calls and the next call past the cap refuses.
    def test_spend_accumulates_then_caps(self):  # $0.0099 + one 150-token call crosses a $0.01 test cap.
        # Set a tiny test cap so one call crosses it.
        os.environ["ARC_BRIDGE_SPEND_CAP_USD"] = "0.01"  # One-cent cap for this test only.
        # Pre-seed the meter just below the test cap.
        self._write_state(0.0099, 1, 9900)  # $0.0099 spent, one prior request.
        # Start the fake endpoint for the one call that must succeed.
        server = HTTPServer(("127.0.0.1", 0), FakeTokenFactoryHandler)  # Port 0 lets the OS pick a free port.
        # Serve requests from a daemon thread so the test can call chat().
        thread = threading.Thread(target=server.serve_forever, daemon=True)  # Daemon so a failure can never hang the suite.
        # Start serving.
        thread.start()  # The fake endpoint is live.
        # Configure a direct key aimed at the fake endpoint.
        os.environ["TOKEN_FACTORY_API_KEY"] = "test-key-not-real"  # Dummy direct key.
        # Point the direct base URL at the fake endpoint.
        os.environ["TOKEN_FACTORY_BASE_URL"] = f"http://127.0.0.1:{server.server_address[1]}/v1"  # The client's configured root.
        # Build the client against this environment.
        client = nebius.TokenFactoryClient()  # Direct mode, below-cap meter.
        # The first call must succeed (meter below cap before the call).
        client.chat("grounding rules", "How do voice replies work?")  # Meters 150 tokens: $0.0099 + $0.00015 = $0.01005.
        # Shut the fake endpoint down now that the call is done.
        server.shutdown()  # Clean teardown for the suite.
        # Read the state file after the successful call.
        with open(self.spend_path, "r", encoding="utf-8") as handle:  # The guard-provided throwaway path.
            # Parse the state JSON.
            state = json.load(handle)  # Running totals after the crossing call.
        # Requests must count both the seeded call and the new one.
        self.assertEqual(state["requests"], 2)  # One seeded plus one new.
        # The new call must have pushed estimated spend past the test cap.
        self.assertGreaterEqual(state["estimated_spend_usd"], 0.01)  # $0.01005 crosses the $0.01 cap.
        # The next call must now refuse on the cap.
        with self.assertRaises(nebius.TokenFactoryError) as ctx:  # Cap refusal, not a network error.
            # Attempt a second chat call that must never reach the network.
            client.chat("system", "user")  # Refused by the cap check before urlopen.
        # The message must name the cap.
        self.assertIn("cap", str(ctx.exception))  # "Spend cap reached" is the refusal text.


# Run the tests when this file is executed directly.
if __name__ == "__main__":  # Direct execution mirrors unittest discovery.
    # Run every test in this module with default verbosity.
    unittest.main()  # Exits nonzero on any failure, like discovery.
