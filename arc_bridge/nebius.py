# nebius.py: the Nebius Token Factory client for ARC Bridge (Nebius port).
# Token Factory exposes an OpenAI-compatible chat-completions API, so this
# client is a thin stdlib wrapper: POST {base_url}/chat/completions with a
# Bearer API key and an OpenAI-shaped JSON body. All configuration comes
# from the environment (TOKEN_FACTORY_API_KEY, TOKEN_FACTORY_BASE_URL,
# NEBIUS_MODEL); nothing is hardcoded and no key ever lives in this repo.
# Portkey mode: when PORTKEY_API_KEY and PORTKEY_VIRTUAL_KEY are both set,
# calls go through the Portkey gateway (api.portkey.ai) instead, which
# forwards to the same Token Factory custom host under a revocable virtual
# key. Portkey wins when fully configured; otherwise the direct key is used.
# Spend cap: every model call is metered against ARC_BRIDGE_SPEND_CAP_USD
# (default $5.00) using a deliberately conservative price ceiling, and the
# counter persists in ARC_BRIDGE_SPEND_STATE_FILE. At the cap the client
# refuses the call, so backends fall back to honest retrieval instead of
# spending more. Portkey's free tier offers no budget control of its own,
# which is why this server-side cap exists.
# Fail-closed contract: when the client is not configured, chat() raises
# TokenFactoryNotConfigured with an exact, human-readable reason, and the
# backends surface that reason instead of faking a model answer.
"""Nebius Token Factory (OpenAI-compatible) Nemotron client, stdlib only."""
# Import datetime for timestamps on the spend-state file.
import datetime  # The state file records when the counter was last updated.
# Import json for serializing the request body and parsing the response.
import json  # JSON request/response bodies for the chat-completions call.
# Import os for reading the TOKEN_FACTORY_* / NEBIUS_MODEL environment vars.
import os  # Environment-based configuration; no secrets in source code.
# Import threading for the spend-state lock (the server is multithreaded).
import threading  # One lock guards the read-modify-write of the state file.
# Import urllib.request for the HTTPS POST to Token Factory (stdlib only).
import urllib.request  # The single outbound HTTPS call lives in chat().
# Import urllib.error to catch HTTP failures and report them, not crash.
import urllib.error  # HTTPError carries the status/body for diagnostics.

# Default Token Factory API root (the /v1 OpenAI-compatible surface).
DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"  # Overridable via TOKEN_FACTORY_BASE_URL.
# Portkey gateway root for the virtual-key mode (OpenAI-compatible shape).
PORTKEY_GATEWAY_URL = "https://api.portkey.ai/v1"  # Fixed: the gateway address is not a secret.
# Default model: an NVIDIA Nemotron instruct model hosted on Token Factory.
DEFAULT_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"  # Overridable via NEBIUS_MODEL; API-list-confirmed Oct 28 (the old 70B instruct is not offered).
# HTTP timeout (seconds) for the Token Factory call; bounded like the mesh.
TOKEN_FACTORY_TIMEOUT_S = 30  # A hung model call must never hang a tool call.
# Speech-safe cap for model answers, mirroring the backends' digest cap.
ANSWER_CHARS = 1100  # Voice-shaped answers stay under the digest limit.
# Default server-side spend cap in USD for model calls through this client.
DEFAULT_SPEND_CAP_USD = 5.00  # Overridable via ARC_BRIDGE_SPEND_CAP_USD; $5 matches the operator's Oct 28 ruling.
# Conservative price ceiling per million tokens used for spend estimates.
DEFAULT_PRICE_PER_1M_USD = 1.00  # Deliberately above plausible Nemotron pricing, so estimates overstate real spend.
# Token estimate per call when the provider returns no usage block.
FALLBACK_TOKENS_PER_CALL = 4000  # Covers a long grounded prompt plus the 400-token answer cap, with headroom.
# Default spend-state file: the operator's home dir, outside the repo.
DEFAULT_SPEND_STATE_FILE = os.path.expanduser("~/.arc-bridge-spend.json")  # Never inside the repo, so pulls never touch it.


# Raised when chat() is called without the required configuration present.
class TokenFactoryNotConfigured(Exception):  # A distinct type so callers can report "not configured" precisely.
    # This class needs no body beyond its docstring-level purpose.
    pass  # The exception message itself carries the exact missing pieces.


# Raised when Token Factory is configured but the call itself fails.
class TokenFactoryError(Exception):  # Network/HTTP/parse failures land here with a readable detail.
    # This class needs no body beyond its docstring-level purpose.
    pass  # The exception message carries the HTTP status or failure reason.


# Read one environment variable, stripped; "" when unset or blank.
def _env(name):  # Small helper so config reads match the backends' style.
    # os.environ.get returns None when unset; default to "".
    return (os.environ.get(name) or "").strip()  # Blank values count as unset, matching backends._env.


# Read one float environment variable; the default when unset or malformed.
def _env_float(name, default):  # Spend config must never crash on a typo.
    # Try the normal float parse first.
    try:  # A clean number wins.
        # Parse the stripped value as a float.
        return float(_env(name) or default)  # Blank falls back to the default.
    # A malformed value degrades to the default, never an exception.
    except ValueError:  # Non-numeric input is operator error, handled safely.
        # Return the safe default.
        return float(default)  # The cap stays enforceable even on bad input.


# Persistent, file-backed meter for model spend against a USD cap.
class SpendTracker:  # One tracker per client; the state file survives restarts.
    # Remember the cap, price ceiling, and state path; start with a lock.
    def __init__(self, cap_usd, price_per_1m_usd, state_path):  # All three come from the environment via the client.
        # The hard cap: estimated spend at or above this refuses new calls.
        self.cap_usd = float(cap_usd)  # Stored as a float for arithmetic.
        # The conservative price ceiling used to convert tokens to dollars.
        self.price_per_1m_usd = float(price_per_1m_usd)  # Overstating is the safe direction.
        # The JSON file holding the running totals.
        self.state_path = state_path  # Outside the repo by default.
        # The lock serializing state-file read-modify-write cycles.
        self._lock = threading.Lock()  # Two threads never interleave a counter update.
    # Load the running totals; zeros when the file is missing or corrupt.
    def _load(self):  # Callers hold the lock; this never raises.
        # Missing state simply means nothing has been spent yet.
        if not os.path.exists(self.state_path):  # First run has no file.
            # Return the zero state.
            return {"estimated_spend_usd": 0.0, "requests": 0, "tokens": 0}  # Fresh counters.
        # Try to parse the existing state file.
        try:  # A well-formed file loads normally.
            # Open and parse the JSON state.
            with open(self.state_path, "r", encoding="utf-8") as handle:  # Read-only here; writes go through _save.
                # Parse the JSON object.
                raw = json.load(handle)  # Must be a dict of numbers.
            # Coerce each field defensively (a hand-edited file may vary).
            return {"estimated_spend_usd": float(raw.get("estimated_spend_usd", 0.0)),  # Spend so far, in USD.
                    "requests": int(raw.get("requests", 0)),  # Completed model calls counted.
                    "tokens": int(raw.get("tokens", 0))}  # Total tokens metered.
        # A corrupt file resets to zeros rather than crashing the server.
        except (ValueError, TypeError, OSError):  # Bad JSON, wrong types, or a read error.
            # Return the zero state (the next save overwrites the bad file).
            return {"estimated_spend_usd": 0.0, "requests": 0, "tokens": 0}  # Safe fallback; spend restarts from zero.
    # Persist the running totals atomically (temp file plus rename).
    def _save(self, state):  # Callers hold the lock; this never raises.
        # Stamp the save time for operators reading the file.
        state["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()  # UTC ISO-8601, like the mesh timestamps.
        # Write to a temp sibling first so a crash never leaves a half file.
        tmp_path = self.state_path + ".tmp"  # Same directory, so rename stays atomic.
        try:  # The normal path writes and renames.
            # Ensure the parent directory exists.
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)  # Handles bare filenames too.
            # Write the full state as one JSON object.
            with open(tmp_path, "w", encoding="utf-8") as handle:  # Temp file first.
                # Serialize compactly; one object, no trailing newline needed.
                handle.write(json.dumps(state, ensure_ascii=False))  # The state file is operator-readable JSON.
            # Atomically replace the old state with the new one.
            os.replace(tmp_path, self.state_path)  # Readers never see a partial write.
        # A failed save must not break the model call it was metering.
        except OSError:  # Disk full, permissions, or a missing directory race.
            # Swallow the error: the in-memory call already succeeded.
            pass  # The next call reloads the last good state (or zeros).
    # A JSON-safe snapshot for health checks and status reports.
    def snapshot(self):  # Reads the file fresh so long-running servers report live totals.
        # Serialize state access under the lock.
        with self._lock:  # No torn reads while another thread saves.
            # Load the current totals.
            state = self._load()  # Zeros when no file exists yet.
        # Shape the snapshot with a rounded spend figure and the cap verdict.
        return {"estimated_spend_usd": round(state["estimated_spend_usd"], 4),  # Four decimals keeps small calls visible.
                "cap_usd": self.cap_usd,  # The cap this snapshot is measured against.
                "requests": state["requests"],  # Completed model calls so far.
                "tokens": state["tokens"],  # Total tokens metered so far.
                "capped": state["estimated_spend_usd"] >= self.cap_usd}  # True once the cap refuses new calls.
    # Refuse the call when estimated spend has reached the cap.
    def check(self):  # Called before every model call; raises on cap.
        # Read the live totals under the lock.
        with self._lock:  # Consistent read even under concurrent calls.
            # Load the current totals.
            state = self._load()  # Fresh read, not a cached guess.
        # Compare estimated spend against the cap.
        if state["estimated_spend_usd"] >= self.cap_usd:  # At the cap, no new spend is allowed.
            # Raise the refusal with the figures, so backends surface it.
            raise TokenFactoryError(  # Backends catch this and fall back to retrieval honestly.
                f"Spend cap reached: estimated ${state['estimated_spend_usd']:.2f} "  # What has been spent (estimated, conservative).
                f"of ${self.cap_usd:.2f} cap; no model call was made")  # What the cap is, and that nothing was spent on this call.
    # Add one call's tokens to the running totals and persist them.
    def record(self, tokens):  # Called after every successful HTTP 200, even on empty content.
        # Convert tokens to estimated dollars at the ceiling price.
        cost = max(0, int(tokens or 0)) / 1000000.0 * self.price_per_1m_usd  # Never negative; ceiling price overstates real cost.
        # Serialize the update under the lock.
        with self._lock:  # No lost updates between concurrent calls.
            # Load the current totals.
            state = self._load()  # Fresh base for the increment.
            # Add this call's estimated cost.
            state["estimated_spend_usd"] = state["estimated_spend_usd"] + cost  # Running total grows monotonically.
            # Count the call.
            state["requests"] = state["requests"] + 1  # One more completed call.
            # Add this call's tokens.
            state["tokens"] = state["tokens"] + max(0, int(tokens or 0))  # Token total grows alongside the spend.
            # Persist the updated totals.
            self._save(state)  # Atomic write; failures are swallowed inside.


# Env-driven Token Factory client; safe to construct when unconfigured.
class TokenFactoryClient:  # Construction never fails; only chat() enforces configuration.
    # Read the configuration from the environment once, at construction.
    def __init__(self):  # Backends build one client and reuse it for every tool call.
        # The direct API key that authorizes Token Factory calls (secret, env-only).
        self.api_key = _env("TOKEN_FACTORY_API_KEY")  # Empty string means "not configured" for direct mode.
        # The Portkey API key for gateway mode (secret, env-only).
        self.portkey_api_key = _env("PORTKEY_API_KEY")  # The "Shared Team Workspace default key" value from the Portkey dashboard.
        # The Portkey virtual key for the Token Factory integration (secret, env-only).
        self.portkey_virtual_key = _env("PORTKEY_VIRTUAL_KEY")  # Revocable without rotating the master Token Factory key.
        # Portkey wins when both of its keys are present; else direct mode.
        self.gateway = "portkey" if (self.portkey_api_key and self.portkey_virtual_key) else "direct"  # Revocable-key mode is preferred for a public demo.
        # The API root actually called: the gateway in Portkey mode, else the direct root.
        self.base_url = ((_env("PORTKEY_BASE_URL") or PORTKEY_GATEWAY_URL).rstrip("/")  # Gateway root when routing through Portkey (override exists for tests).
                         if self.gateway == "portkey"  # Portkey mode: gateway root.
                         else (_env("TOKEN_FACTORY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"))  # Direct root otherwise; trailing slashes would double up in the URL join below.
        # The Nemotron model slug every chat call requests.
        self.model = _env("NEBIUS_MODEL") or DEFAULT_MODEL  # Env override wins; otherwise the Nemotron default.
        # The spend meter for this client (cap, ceiling price, state file).
        self.spend = SpendTracker(  # One meter per client; state persists across restarts.
            # The USD cap from the environment, defaulting to $5.
            _env_float("ARC_BRIDGE_SPEND_CAP_USD", DEFAULT_SPEND_CAP_USD),  # Malformed input degrades to the default, never a crash.
            # The conservative price ceiling, defaulting to $1 per million tokens.
            _env_float("ARC_BRIDGE_PRICE_PER_1M_USD", DEFAULT_PRICE_PER_1M_USD),  # Overstating keeps real spend below the estimate.
            # The state file path, defaulting to the home directory.
            _env("ARC_BRIDGE_SPEND_STATE_FILE") or DEFAULT_SPEND_STATE_FILE)  # Outside the repo so pulls never touch it.
    # Report whether every required piece of configuration is present.
    def configured(self):  # Backends call this to decide between a model answer and an honest note.
        # Direct mode needs its key; Portkey mode needs both of its keys.
        return bool(self.api_key) or bool(self.portkey_api_key and self.portkey_virtual_key)  # Either complete route configures the client.
    # Name the exact missing configuration pieces for error messages.
    def missing(self):  # Used by status reports and the not-configured error text.
        # Collect the names of the pieces that are absent.
        missing = []  # Start empty; append each absent required variable.
        # A partial Portkey setup names its own gap (the operator started that route).
        if self.portkey_api_key and not self.portkey_virtual_key:  # Portkey API key present, virtual key absent.
            # Name the missing virtual key variable.
            missing.append("PORTKEY_VIRTUAL_KEY")  # The one Portkey piece still needed.
        # The mirror case: virtual key present, API key absent.
        elif self.portkey_virtual_key and not self.portkey_api_key:  # Virtual key present, API key absent.
            # Name the missing API key variable.
            missing.append("PORTKEY_API_KEY")  # The one Portkey piece still needed.
        # With no route started at all, name the direct key (the historic default).
        elif not self.api_key:  # Nothing configured: direct, Portkey, or partial.
            # Name the variable the operator must set.
            missing.append("TOKEN_FACTORY_API_KEY")  # The single hard requirement, named exactly (existing tests assert this).
        # Return the list (empty when fully configured).
        return missing  # Callers join this into the human-readable reason.
    # A JSON-safe status snapshot for health checks and startup logging.
    def status(self):  # Surfaced by arc_health and the server startup banner.
        # Assemble the status dict the tools embed in their results.
        return {  # Open the status dict.
            # Whether a model call would actually be attempted right now.
            "configured": self.configured(),  # True only when a complete route is present.
            # The provider label, so results never hide where answers come from.
            "provider": "nebius-token-factory",  # Answers are attributed to Token Factory, never vaguely to "AI".
            # The routing mode: direct Token Factory calls, or via Portkey.
            "gateway": self.gateway,  # "portkey" means the revocable virtual key fronts the call.
            # The model slug that would be requested.
            "model": self.model,  # The Nemotron model id for this deployment.
            # The API root that would be called (never the key itself).
            "base_url": self.base_url,  # Safe to show: the URL is not a secret.
            # The exact configuration gap, or None when fully configured.
            "missing": self.missing() or None,  # None reads cleaner than [] in tool JSON.
            # The spend meter snapshot (estimates, cap, and capped flag).
            "spend": self.spend.snapshot(),  # Safe to show: counters and estimates, never keys.
        }  # Close the status dict.
    # Run one chat completion against Nemotron on Token Factory.
    def chat(self, system_prompt, user_prompt):  # Returns the model's answer text, capped speech-safe.
        # Fail closed first: an unconfigured client must never fake a call.
        if not self.configured():  # The key check happens before any network activity.
            # Raise with the exact variable the operator must set.
            raise TokenFactoryNotConfigured(  # A clear, actionable error, per the port's contract.
                "Nebius Token Factory is not configured: set "  # Say what is wrong in plain words.
                + ", ".join(self.missing())  # Name the missing environment variable(s).
                + " (see .env.example); no model call was made")  # State plainly that nothing was faked.
        # Enforce the spend cap before any network activity.
        self.spend.check()  # Raises TokenFactoryError at the cap; backends fall back to retrieval.
        # Build the OpenAI-shaped chat-completions request body.
        body = {  # Open the request body dict.
            # The Nemotron model slug chosen at construction time.
            "model": self.model,  # Token Factory routes on this exact model id.
            # The two-message conversation: system framing, then the request.
            "messages": [  # Open the messages list.
                # The system message carries the assistant's ground rules.
                {"role": "system", "content": system_prompt},  # Grounding rules (cite memory, stay honest).
                # The user message carries the question plus retrieved context.
                {"role": "user", "content": user_prompt},  # The actual question and its memory context.
            ],  # Close the messages list.
            # A modest temperature keeps answers factual, not creative.
            "temperature": 0.2,  # Low temperature suits a memory-grounded assistant.
            # Cap the answer length so digests stay speech-safe.
            "max_tokens": 400,  # Roughly the ANSWER_CHARS budget in tokens.
        }  # Close the request body dict.
        # Serialize the body to UTF-8 JSON bytes for the POST.
        data = json.dumps(body).encode("utf-8")  # urllib sends bytes, not str.
        # Build the POST request against the chat-completions endpoint.
        req = urllib.request.Request(  # One explicit request object, headers added below.
            # The OpenAI-compatible endpoint path under the configured root.
            self.base_url + "/chat/completions",  # Token Factory mirrors the OpenAI route shape; the gateway mirrors it too.
            # The serialized JSON body.
            data=data,  # Attach the request body.
            # Chat completions are always a POST.
            method="POST")  # Close the request constructor.
        # Portkey mode authenticates with its two gateway headers.
        if self.gateway == "portkey":  # The gateway forwards to Token Factory under the virtual key.
            # The Portkey API key identifies the workspace.
            req.add_header("x-portkey-api-key", self.portkey_api_key)  # Gateway workspace auth (secret, header-only).
            # The virtual key selects the Token Factory integration.
            req.add_header("x-portkey-virtual-key", self.portkey_virtual_key)  # Gateway provider routing (secret, header-only).
        # Direct mode authenticates with the Token Factory Bearer key.
        else:  # Plain Token Factory calls use the standard header.
            # The Bearer header carries the API key (the only secret on the wire).
            req.add_header("Authorization", f"Bearer {self.api_key}")  # Standard OpenAI-compatible auth header.
        # Declare the JSON content type Token Factory expects.
        req.add_header("Content-Type", "application/json")  # Matches the serialized body above.
        # A curl-style UA avoids the egress proxy's urllib-default 403 quirk.
        req.add_header("User-Agent", "curl/8.0")  # Same workaround the live mesh backend uses.
        # Perform the call with a bounded timeout; translate every failure.
        try:  # Network and HTTP failures become TokenFactoryError, not crashes.
            # urlopen raises HTTPError for 4xx/5xx statuses.
            with urllib.request.urlopen(req, timeout=TOKEN_FACTORY_TIMEOUT_S) as resp:  # Bounded timeout; read the response.
                # Parse the OpenAI-shaped JSON response body.
                parsed = json.loads(resp.read())  # Token Factory returns standard chat-completion JSON.
        # An HTTP error still carries a status worth reporting precisely.
        except urllib.error.HTTPError as exc:  # 401/404/429 etc. land here with their status code.
            # Raise a readable error naming the HTTP status.
            raise TokenFactoryError(f"Token Factory HTTP {exc.code}") from exc  # Status only; the body may echo request data, so it is not quoted.
        # Network-level failures (DNS, refused, timeout) land here.
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            # Raise a readable error with the truncated failure reason.
            raise TokenFactoryError(f"Token Factory call failed: {str(exc)[:160]}") from exc  # Truncated so tool results stay tidy.
        # Meter the call from the provider's usage block, before content checks.
        usage = parsed.get("usage") or {}  # The usage block may be absent on some gateways.
        # Prefer prompt + completion tokens when both are present and numeric.
        prompt_tokens = usage.get("prompt_tokens")  # Input tokens for this call.
        # Completion tokens for this call.
        completion_tokens = usage.get("completion_tokens")  # Output tokens for this call.
        # Total tokens as the fallback when the split is unavailable.
        total_tokens = usage.get("total_tokens")  # Provider-summed tokens, when offered.
        # Compute the metered token count with a safe fallback.
        if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):  # Both halves present: sum them.
            # Sum the two halves.
            metered_tokens = prompt_tokens + completion_tokens  # Exact token count for this call.
        # Otherwise fall back to the provider total when it is an integer.
        elif isinstance(total_tokens, int):  # Total present without the split.
            # Use the provider total.
            metered_tokens = total_tokens  # Still an exact provider count.
        # With no usable usage block, use the conservative per-call estimate.
        else:  # No usage data: estimate high rather than meter zero.
            # Use the fallback estimate (covers prompt + answer cap + headroom).
            metered_tokens = FALLBACK_TOKENS_PER_CALL  # Over-metering is the safe direction.
        # Record the call even if the content check below fails (thinking burned tokens too).
        self.spend.record(metered_tokens)  # Persists the running totals; failures inside are swallowed.
        # Extract the answer text from the first choice, defensively.
        try:  # A malformed success body must degrade to an error, not a crash.
            # The OpenAI shape: choices[0].message.content.
            text = parsed["choices"][0]["message"]["content"]  # The model's answer string.
        # Any shape surprise is reported as a Token Factory error.
        except (KeyError, IndexError, TypeError) as exc:  # Missing keys or an empty choices list.
            # Name the shape problem instead of returning an empty answer.
            raise TokenFactoryError("Token Factory returned an unexpected response shape") from exc  # Honest failure beats a silent blank.
        # Normalize whitespace so the digest reads cleanly aloud.
        text = " ".join((text or "").split())  # Collapse newlines/runs the way the worker's digests do.
        # An empty completion is a failure, not an answer.
        if not text:  # The model returned nothing usable.
            # Report it rather than returning a blank digest.
            raise TokenFactoryError("Token Factory returned an empty completion")  # Callers fall back to retrieval with this reason attached.
        # Cap the answer at the speech-safe digest length.
        if len(text) > ANSWER_CHARS:  # Long answers get truncated with an ellipsis marker.
            # Truncate with the same ellipsis convention the backends use.
            text = text[:ANSWER_CHARS - 1] + "…"  # Matches LocalBackend._digest truncation style.
        # Return the model's answer text to the backend.
        return text  # The backend labels it with provider + model in its result.
