# nebius.py: the Nebius Token Factory client for ARC Bridge (Nebius port).
# Token Factory exposes an OpenAI-compatible chat-completions API, so this
# client is a thin stdlib wrapper: POST {base_url}/chat/completions with a
# Bearer API key and an OpenAI-shaped JSON body. All configuration comes
# from the environment (TOKEN_FACTORY_API_KEY, TOKEN_FACTORY_BASE_URL,
# NEBIUS_MODEL); nothing is hardcoded and no key ever lives in this repo.
# Fail-closed contract: when the client is not configured, chat() raises
# TokenFactoryNotConfigured with an exact, human-readable reason, and the
# backends surface that reason instead of faking a model answer.
"""Nebius Token Factory (OpenAI-compatible) Nemotron client, stdlib only."""
# Import json for serializing the request body and parsing the response.
import json  # JSON request/response bodies for the chat-completions call.
# Import os for reading the TOKEN_FACTORY_* / NEBIUS_MODEL environment vars.
import os  # Environment-based configuration; no secrets in source code.
# Import urllib.request for the HTTPS POST to Token Factory (stdlib only).
import urllib.request  # The single outbound HTTPS call lives in chat().
# Import urllib.error to catch HTTP failures and report them, not crash.
import urllib.error  # HTTPError carries the status/body for diagnostics.

# Default Token Factory API root (the /v1 OpenAI-compatible surface).
DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"  # Overridable via TOKEN_FACTORY_BASE_URL.
# Default model: an NVIDIA Nemotron instruct model hosted on Token Factory.
DEFAULT_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"  # Overridable via NEBIUS_MODEL; API-list-confirmed Oct 28 (the old 70B instruct is not offered).
# HTTP timeout (seconds) for the Token Factory call; bounded like the mesh.
TOKEN_FACTORY_TIMEOUT_S = 30  # A hung model call must never hang a tool call.
# Speech-safe cap for model answers, mirroring the backends' digest cap.
ANSWER_CHARS = 1100  # Voice-shaped answers stay under the digest limit.


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


# Env-driven Token Factory client; safe to construct when unconfigured.
class TokenFactoryClient:  # Construction never fails; only chat() enforces configuration.
    # Read the configuration from the environment once, at construction.
    def __init__(self):  # Backends build one client and reuse it for every tool call.
        # The API key that authorizes Token Factory calls (secret, env-only).
        self.api_key = _env("TOKEN_FACTORY_API_KEY")  # Empty string means "not configured".
        # The API root; defaults to the public Token Factory /v1 surface.
        self.base_url = (_env("TOKEN_FACTORY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")  # Trailing slashes would double up in the URL join below.
        # The Nemotron model slug every chat call requests.
        self.model = _env("NEBIUS_MODEL") or DEFAULT_MODEL  # Env override wins; otherwise the Nemotron default.
    # Report whether every required piece of configuration is present.
    def configured(self):  # Backends call this to decide between a model answer and an honest note.
        # Only the API key is strictly required; base URL and model default.
        return bool(self.api_key)  # No key, no call: Token Factory has no anonymous tier here.
    # Name the exact missing configuration pieces for error messages.
    def missing(self):  # Used by status reports and the not-configured error text.
        # Collect the names of the pieces that are absent.
        missing = []  # Start empty; append each absent required variable.
        # The API key is the one required piece.
        if not self.api_key:  # Without a key the client cannot authenticate at all.
            # Name the variable the operator must set.
            missing.append("TOKEN_FACTORY_API_KEY")  # The single hard requirement, named exactly.
        # Return the list (empty when fully configured).
        return missing  # Callers join this into the human-readable reason.
    # A JSON-safe status snapshot for health checks and startup logging.
    def status(self):  # Surfaced by arc_health and the server startup banner.
        # Assemble the status dict the tools embed in their results.
        return {  # Open the status dict.
            # Whether a model call would actually be attempted right now.
            "configured": self.configured(),  # True only when the API key is present.
            # The provider label, so results never hide where answers come from.
            "provider": "nebius-token-factory",  # Answers are attributed to Token Factory, never vaguely to "AI".
            # The model slug that would be requested.
            "model": self.model,  # The Nemotron model id for this deployment.
            # The API root that would be called (never the key itself).
            "base_url": self.base_url,  # Safe to show: the URL is not a secret.
            # The exact configuration gap, or None when fully configured.
            "missing": self.missing() or None,  # None reads cleaner than [] in tool JSON.
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
            self.base_url + "/chat/completions",  # Token Factory mirrors the OpenAI route shape.
            # The serialized JSON body.
            data=data,  # Attach the request body.
            # Chat completions are always a POST.
            method="POST")  # Close the request constructor.
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
