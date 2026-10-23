# backends.py: the two execution backends behind the ARC Bridge MCP tools.
# LocalBackend is a file-backed harness that models the real mesh contracts
# (question queue, shared memory, alert inbox, heartbeat) so the server runs
# with zero configuration. LiveBackend talks to David's real ARC mesh over
# Supabase REST + Voice Monkey + Telegram + ntfy, all configured by env vars.
# No secrets live in this repo; see ../.env.example for the variable list.
# Nebius port: both backends also hold a Token Factory client (nebius.py).
# When it is configured, local arc_ask answers are written by an NVIDIA
# Nemotron model grounded in retrieved memory; when it is not, answers stay
# honest retrieval digests and the result says exactly why (fail-closed).
"""Local (file-backed) and live (mesh-backed) tool backends."""
# Import datetime for UTC timestamps on queue entries and alert records.
import datetime
# Import json for reading/writing the JSONL data files and REST payloads.
import json
# Import os for environment variables and data-directory paths.
import os
# Import time for heartbeat-age arithmetic.
import time
# Import urllib.parse for encoding Supabase query parameters.
import urllib.parse
# Import urllib.request for the live backend's HTTPS calls (stdlib only).
import urllib.request
# Import urllib.error to catch HTTP failures without crashing the tool.
import urllib.error
# Import the Nebius Token Factory client (Nemotron model answers).
from arc_bridge import nebius  # noqa: E402 - package import, matches server.py style.

# Voice Monkey v3 announce endpoint (same one the VM worker uses).
VOICEMONKEY_ANNOUNCE_URL = "https://api-v3.voicemonkey.io/announce"
# Telegram Bot API base URL for direct sendMessage calls.
TELEGRAM_API_BASE = "https://api.telegram.org/bot"
# Public ntfy.sh host used as the best-effort secondary notify channel.
NTFY_HOST = "https://ntfy.sh"
# HTTP timeout (seconds) for every live-backend outbound call.
LIVE_TIMEOUT_S = 20
# Speech-safe cap for digests (mirrors the worker's ALEXA_SPEECH_CHARS).
DIGEST_CHARS = 1100
# System prompt that grounds Nemotron answers in retrieved mesh memory only.
MODEL_SYSTEM_PROMPT = (
    "You are ARC, a personal AI assistant with persistent memory. "  # Identity: the user's own assistant, not a generic chatbot.
    "Answer using ONLY the memory records provided; if they do not cover "  # Grounding rule: memory is the only source of facts.
    "the question, say what memory does not contain instead of inventing "  # Honesty rule: name the gap rather than hallucinating.
    "facts. Keep the answer under 150 words, plain language, speech-safe."  # Style rule: short enough to be spoken aloud by the voice path.
)  # End the model system prompt.


# Read one environment variable, stripped; "" when unset or blank.
def _env(name):
    # os.environ.get returns None when unset; default to "".
    return (os.environ.get(name) or "").strip()


# UTC now as an ISO-8601 string, matching the worker's heartbeat format.
def _utcnow():
    # Use timezone-aware UTC, exactly like the mesh worker does.
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# Append one JSON object as a line to a JSONL file (creating parents).
def _append_jsonl(path, obj):
    # Ensure the data directory exists before writing.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Open in append mode with UTF-8 for the new line.
    with open(path, "a", encoding="utf-8") as handle:
        # Serialize compactly; one object per line.
        handle.write(json.dumps(obj, ensure_ascii=False) + "\n")


# Read every JSON object from a JSONL file; [] when the file is missing.
def _read_jsonl(path):
    # Missing files simply mean "no records yet".
    if not os.path.exists(path):
        # Return an empty list so callers need no special-casing.
        return []
    # Collect the parsed objects in order.
    rows = []
    # Read the file line by line.
    with open(path, "r", encoding="utf-8") as handle:
        # Process each line independently (one bad line never kills all).
        for line in handle:
            # Skip blank lines.
            line = line.strip()
            # Ignore empty lines.
            if not line:
                # Move to the next line.
                continue
            # Parse the line; skip it when malformed.
            try:
                # Append the parsed object to the result list.
                rows.append(json.loads(line))
            # A corrupt line is skipped, not fatal.
            except ValueError:
                # Move to the next line.
                continue
    # Hand the parsed rows back to the caller.
    return rows


# Score how well a memory record matches a query (simple keyword overlap).
def _match_score(text, query_words):
    # Lowercase once for case-insensitive matching.
    lowered = text.lower()
    # Count how many distinct query words appear in the text.
    hits = sum(1 for word in query_words if word in lowered)
    # A small length bonus keeps longer, richer records competitive.
    return hits, len(text)


# File-backed backend: zero-config local harness of the mesh contracts.
class LocalBackend:
    # Remember the data directory and seed demo content on first run.
    def __init__(self, data_dir):
        # The directory holding queue.jsonl, memory.jsonl, alerts.jsonl.
        self.data_dir = data_dir
        # File holding the question/answer queue records.
        self.queue_path = os.path.join(data_dir, "queue.jsonl")
        # File holding the shared-memory key/value records.
        self.memory_path = os.path.join(data_dir, "memory.jsonl")
        # File holding the notification inbox records.
        self.alerts_path = os.path.join(data_dir, "alerts.jsonl")
        # The Token Factory client that can upgrade answers to Nemotron.
        self.model = nebius.TokenFactoryClient()  # Reads its config from the environment; safe when unset.
        # Seed demo content when the memory file does not exist yet.
        self._seed_once()

    # Write clearly-labeled demo records when starting from an empty dir.
    def _seed_once(self):
        # Skip seeding when a memory file already exists (never overwrite).
        if os.path.exists(self.memory_path):
            # Existing data wins; seeding runs exactly once ever.
            return
        # Demo records, every one labeled so nobody mistakes them for real.
        demo = [
            # A demo fact about the mesh itself.
            {"key": "demo.mesh", "value": "DEMO DATA: ARC Bridge MCP local "
             "harness. The live mesh worker runs on a GCP e2-micro VM and "  # Say where the live worker runs.
             "answers Alexa questions through the arc_alexa_queue table.",  # Name the live question queue table.
             "ts": _utcnow(), "demo": True},  # Timestamp it; label it demo data.
            # A demo fact about the voice path.
            {"key": "demo.voice", "value": "DEMO DATA: Voice replies go out "
             "through Voice Monkey to the kitchen Echo. ECHO_POLICY is "  # Name the voice announcement path.
             "replies_only: conversational answers speak, proactive "  # State the replies-only policy.
             "announcements are refused.",  # Finish the policy sentence.
             "ts": _utcnow(), "demo": True},  # Timestamp it; label it demo data.
            # A demo fact about notifications.
            {"key": "demo.notify", "value": "DEMO DATA: Notifications fan "
             "out over Telegram (primary) and ntfy.sh (best-effort). Every "  # Name the notify channels.
             "send is also appended to the local alerts inbox.",  # Mention the local inbox copy.
             "ts": _utcnow(), "demo": True},  # Timestamp it; label it demo data.
        ]  # End the demo record list.
        # Append each demo record to the memory file.
        for record in demo:
            # One JSON object per line.
            _append_jsonl(self.memory_path, record)

    # Ask the mesh a question; answered here by local retrieval over memory.
    def ask(self, question, context=""):
        # Strip the question; empty questions are a caller error.
        question = (question or "").strip()
        # Refuse empty questions instead of queueing noise.
        if not question:
            # ValueError becomes an MCP isError result, not a crash.
            raise ValueError("question must not be empty")
        # Compute the answer: Nemotron via Token Factory when configured,
        # otherwise an honest retrieval digest plus the reason why not.
        digest, model_info = self._answer(question)  # (answer text, model attribution/failure info).
        # Pick the honest mode label for how this answer was produced.
        answer_mode = "local-nemotron" if model_info.get("used") else "local-retrieval"  # Nemotron answers and retrieval answers are never confused.
        # Build the queue record, mirroring the arc_alexa_queue shape.
        entry = {
            # Unique id: UTC timestamp plus a random suffix.
            "id": ("q-" + datetime.datetime.now(datetime.timezone.utc)
                   .strftime("%Y%m%d%H%M%S") + "-" + os.urandom(3).hex()),  # Timestamp plus a random hex suffix.
            # The question text as asked.
            "question": question,
            # Optional extra context the caller supplied.
            "context": context or "",
            # Local mode answers synchronously, so mark it answered.
            "status": "answered",
            # The answer text (Nemotron-written or retrieval digest).
            "digest": digest,
            # When the question was asked (UTC ISO-8601).
            "ts": _utcnow(),
            # Always label the answering mode honestly.
            "mode": answer_mode,
        }  # End the queue entry dict.
        # Persist the record to the queue file (the queue genuinely grows).
        _append_jsonl(self.queue_path, entry)
        # The public mode label mirrors the internal one for API callers.
        public_mode = "local+token-factory" if model_info.get("used") else "local"  # Token Factory use is visible in the result, never hidden.
        # Return the public result for the tool response.
        return {"queue_id": entry["id"], "status": entry["status"],
                "digest": entry["digest"], "mode": public_mode,
                "model": model_info}  # Digest, honest mode label, and full model attribution.

    # Compute an answer digest by keyword retrieval over local memory.
    def _digest(self, question):
        # Reuse the memory search with a small limit for the digest.
        hits = self.memory_search(question, limit=3)
        # No matches: say so plainly instead of inventing an answer.
        if not hits["matches"]:
            # Honest empty answer, still labeled as local mode.
            return ("Local memory has nothing on that yet. "
                    "(local-retrieval mode)")  # Finish the empty-answer message.
        # Join the top matches into one speech-safe digest string.
        parts = [m["value"] for m in hits["matches"]]
        # Combine with separators.
        digest = " ".join(parts)
        # Cap the length for voice-style consumption.
        if len(digest) > DIGEST_CHARS:
            # Truncate with an ellipsis marker.
            digest = digest[:DIGEST_CHARS - 1] + "…"
        # Return the assembled digest.
        return digest

    # Produce the answer text plus model attribution for one question.
    def _answer(self, question):
        # Retrieve the memory records this answer may be grounded in.
        hits = self.memory_search(question, limit=3)  # The same retrieval the plain digest uses.
        # Unconfigured Token Factory: retrieval digest + the exact reason.
        if not self.model.configured():  # No API key means no model call, by design.
            # Build the same clear error text the client itself raises.
            reason = ("Nebius Token Factory is not configured: set "  # Plain-language statement of the gap.
                      + ", ".join(self.model.missing())  # Name the missing environment variable(s).
                      + "; no model call was made")  # State plainly that nothing was faked.
            # Return the retrieval digest with the model block marked unused.
            return self._digest(question), {"configured": False,  # Honest flag: no model was involved.
                                            "provider": "nebius-token-factory",  # Where a model answer WOULD come from.
                                            "model": self.model.model,  # The slug that would be requested once configured.
                                            "used": False,  # Retrieval produced this answer.
                                            "error": reason}  # The exact reason, surfaced to the caller.
        # Join the retrieved records into the grounding context block.
        context = "\n".join(m["value"] for m in hits["matches"]) or "No relevant memory records."  # Empty retrieval is stated, not hidden.
        # Build the user prompt: the question plus its grounding context.
        prompt = f"Question: {question}\n\nMemory records:\n{context}"  # The model sees nothing beyond memory + question.
        # Attempt the Nemotron call; failures fall back to retrieval honestly.
        try:  # A configured-but-failing API must degrade, not crash the tool.
            # Run the chat completion on Token Factory.
            text = self.model.chat(MODEL_SYSTEM_PROMPT, prompt)  # Nemotron writes the answer from the memory context.
            # Return the model answer with full attribution.
            return text, {"configured": True,  # A key was present for this call.
                          "provider": "nebius-token-factory",  # The API that served the answer.
                          "model": self.model.model,  # The Nemotron slug that answered.
                          "used": True}  # The model genuinely produced this answer.
        # Token Factory errors degrade to the retrieval digest with a reason.
        except nebius.TokenFactoryError as exc:  # HTTP/network/shape failures land here.
            # Return the retrieval digest plus the failure detail.
            return self._digest(question), {"configured": True,  # Configuration existed; the call failed.
                                            "provider": "nebius-token-factory",  # The API that was attempted.
                                            "model": self.model.model,  # The slug that was attempted.
                                            "used": False,  # Retrieval produced this answer instead.
                                            "error": str(exc)}  # The failure reason, surfaced to the caller.

    # Look up one queue record by id; None when unknown.
    def queue_status(self, queue_id):
        # Scan the queue file for the matching record.
        for entry in _read_jsonl(self.queue_path):
            # Compare string ids exactly.
            if str(entry.get("id")) == str(queue_id):
                # Return the stored record unchanged.
                return entry
        # Unknown ids return None (the tool reports "not found").
        return None

    # Keyword search over the local memory file.
    def memory_search(self, query, limit=5):
        # Clamp the limit to a sane range (1..20).
        limit = max(1, min(int(limit or 5), 20))
        # Split the query into lowercase words of length 3+.
        words = [w for w in query.lower().split() if len(w) >= 3]
        # A query with no usable words matches nothing.
        if not words:
            # Return the empty result shape.
            return {"matches": [], "mode": "local"}
        # Score every memory record against the query words.
        scored = []
        # Walk all records in the memory file.
        for record in _read_jsonl(self.memory_path):
            # Search across the key and value text together.
            text = f"{record.get('key', '')} {record.get('value', '')}"
            # Compute (word hits, length) for ranking.
            hits, length = _match_score(text, words)
            # Keep only records with at least one word hit.
            if hits:
                # Store (hits, length, record) for sorting.
                scored.append((hits, length, record))
        # Sort by word hits first, then by record length, both descending.
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        # Take the top-N records and shape them for the response.
        matches = [
            # Expose key, value, timestamp, and demo label.
            {"key": r.get("key"), "value": r.get("value"),
             "ts": r.get("ts"), "demo": bool(r.get("demo"))}  # Timestamp and demo label; close the match.
            # Unpack the sorted (hits, length, record) tuples.
            for _, _, r in scored[:limit]
        ]  # End the match list.
        # Return the match list plus the honest mode label.
        return {"matches": matches, "mode": "local"}

    # Local health snapshot: harness liveness plus queue depth.
    def health(self):
        # Count queue records not yet answered (normally zero locally).
        depth = sum(1 for e in _read_jsonl(self.queue_path)
                    if e.get("status") != "answered")  # Count only unanswered queue records.
        # Assemble the health snapshot.
        return {
            # The local harness is always "up" when this code runs.
            "worker": "local-harness",
            # Heartbeat age is zero: the harness answers inline.
            "heartbeat_age_s": 0,
            # Depth of unanswered questions in the local queue.
            "queue_depth": depth,
            # Number of memory records available for retrieval.
            "memory_records": len(_read_jsonl(self.memory_path)),
            # Token Factory/Nemotron configuration snapshot.
            "model": self.model.status(),  # Configured?, which Nemotron slug, what is missing.
            # Always label the mode honestly.
            "mode": "local",
            # Timestamp of this snapshot.
            "ts": _utcnow(),
        }  # End the health snapshot dict.

    # Record a notification in the local alerts inbox (all channels = log).
    def notify(self, title, message, channel="log", priority=3):
        # Strip inputs; title and message are required.
        title = (title or "").strip()
        # Strip the message too.
        message = (message or "").strip()
        # Refuse empty titles instead of logging noise.
        if not title:
            # ValueError becomes an MCP isError result, not a crash.
            raise ValueError("title must not be empty")
        # Refuse empty messages for the same reason.
        if not message:
            # ValueError becomes an MCP isError result, not a crash.
            raise ValueError("message must not be empty")
        # Clamp priority into the 1..5 range.
        priority = max(1, min(int(priority or 3), 5))
        # Build the inbox record, mirroring the mesh alerts_inbox.json shape.
        record = {"ts": _utcnow(), "title": title, "message": message,
                  "priority": priority, "channel": channel, "mode": "local"}  # Priority, channel, mode; close the record.
        # Append it to the alerts file (the inbox genuinely grows).
        _append_jsonl(self.alerts_path, record)
        # Local mode delivers to the log only; other channels need live mode.
        delivered = {"log": True, "echo": False, "telegram": False,
                     "ntfy": False}  # Non-log channels stay undelivered locally.
        # Tell the caller plainly that non-log channels need live config.
        note = ("local mode: recorded to the alerts inbox; echo/telegram/ntfy "
                "need live mode with credentials configured")  # Finish the honest local-mode note.
        # Return the delivery report.
        return {"delivered": delivered, "mode": "local", "note": note}


# Perform one Supabase REST call; returns (status, parsed_json_or_None).
def _sb_req(url, key, method, path, body=None, query=""):
    # Build the full REST URL from base + path + query string.
    full = url.rstrip("/") + path + query
    # Serialize the body when one was supplied.
    data = json.dumps(body).encode("utf-8") if body is not None else None
    # Build the request with the explicit HTTP method.
    req = urllib.request.Request(full, data=data, method=method)
    # Supabase REST requires the apikey header on every call.
    req.add_header("apikey", key)
    # The Authorization header carries the same key for anon/service roles.
    req.add_header("Authorization", f"Bearer {key}")
    # JSON bodies declare their content type.
    if data is not None:
        # Tell Supabase the body is JSON.
        req.add_header("Content-Type", "application/json")
    # Prefer header: merge duplicates on heartbeat upserts.
    req.add_header("Prefer", "return=representation")
    # A curl-style UA avoids the egress proxy's urllib 403 quirk.
    req.add_header("User-Agent", "curl/8.0")
    # Perform the call with a bounded timeout.
    try:
        # urlopen raises HTTPError for 4xx/5xx statuses.
        with urllib.request.urlopen(req, timeout=LIVE_TIMEOUT_S) as resp:
            # Read the response body bytes.
            raw = resp.read()
            # Parse JSON when the body is non-empty.
            parsed = json.loads(raw) if raw else None
            # Return the HTTP status and parsed body.
            return resp.status, parsed
    # HTTPError still carries status and body: capture both.
    except urllib.error.HTTPError as exc:
        # Read the error body for diagnostics.
        raw = exc.read()
        # Parse JSON error bodies when possible.
        try:
            # Some Supabase errors are JSON.
            parsed = json.loads(raw) if raw else None
        # Non-JSON bodies stay as raw text.
        except ValueError:
            # Keep the text for the error report.
            parsed = raw.decode("utf-8", errors="replace")[:200]
        # Return the error status and body.
        return exc.code, parsed
    # Network-level failures (DNS, refused, timeout) surface here.
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        # Return status 0 to mean "no HTTP response at all".
        return 0, str(exc)[:200]


# Live backend: David's real ARC mesh via Supabase REST and notify channels.
class LiveBackend:
    # Read every credential from the environment; fail closed when missing.
    def __init__(self):
        # Supabase project URL for the mesh tables.
        self.sb_url = _env("ARC_SUPABASE_URL")
        # Supabase key (anon insert-only or service key per his setup).
        self.sb_key = _env("ARC_SUPABASE_KEY")
        # Voice Monkey token for Echo announcements.
        self.vm_token = _env("VOICEMONKEY_TOKEN")
        # Voice Monkey device name (his kitchen Echo).
        self.vm_speaker = _env("VOICEMONKEY_SPEAKER")
        # Telegram bot token for direct sendMessage calls.
        self.tg_token = _env("TELEGRAM_BOT_TOKEN")
        # Telegram chat id the bot delivers to.
        self.tg_chat = _env("TELEGRAM_CHAT_ID")
        # ntfy.sh topic for the best-effort secondary channel.
        self.ntfy_topic = _env("NTFY_TOPIC")
        # The Token Factory client, reported in health for this port.
        self.model = nebius.TokenFactoryClient()  # Live answers still come from the mesh worker; this tracks the port's model wiring.

    # True when Supabase is configured for queue/memory/health calls.
    def _sb_ready(self):
        # Both the URL and the key must be present.
        return bool(self.sb_url and self.sb_key)

    # Ask the mesh: insert into arc_alexa_queue for the worker to answer.
    def ask(self, question, context=""):
        # Strip the question; empty questions are a caller error.
        question = (question or "").strip()
        # Refuse empty questions instead of queueing noise.
        if not question:
            # ValueError becomes an MCP isError result, not a crash.
            raise ValueError("question must not be empty")
        # Supabase must be configured for the live queue insert.
        if not self._sb_ready():
            # Fail closed with a clear configuration message.
            raise ValueError("live mode needs ARC_SUPABASE_URL and "
                             "ARC_SUPABASE_KEY")  # Name the missing Supabase key.
        # Build the queue row, mirroring the worker's expected shape.
        body = {"question": question, "context": context or "",
                "status": "new", "created_at": _utcnow()}  # New question; close the queue row.
        # Insert the row and ask Supabase to return the new record.
        status, rows = _sb_req(self.sb_url, self.sb_key, "POST",
                               "/rest/v1/arc_alexa_queue", body=body)  # Insert into the worker's question queue.
        # Any non-2xx status is a failed insert.
        if status not in (200, 201):
            # Report the failure with the status for debugging.
            raise ValueError(f"queue insert failed: HTTP {status}: {rows}")
        # Pull the new row's id from the returned representation.
        queue_id = rows[0].get("id") if rows else None
        # Return the queued status: the worker answers asynchronously.
        return {"queue_id": queue_id, "status": "queued", "mode": "live",
                "note": "the mesh worker answers async and announces via "  # Explain the async answer path.
                        "the Echo when done"}  # Finish the note; close the result.

    # Look up one arc_alexa_queue record by id.
    def queue_status(self, queue_id):
        # Supabase must be configured for the live queue read.
        if not self._sb_ready():
            # Fail closed with a clear configuration message.
            raise ValueError("live mode needs ARC_SUPABASE_URL and "
                             "ARC_SUPABASE_KEY")  # Name the missing Supabase key.
        # Encode the id filter for the query string.
        filt = urllib.parse.quote(str(queue_id))
        # Select the status columns for this single row.
        status, rows = _sb_req(
            # GET one row by id.
            self.sb_url, self.sb_key, "GET", "/rest/v1/arc_alexa_queue",
            # Filter, column selection, single-row limit.
            query=f"?id=eq.{filt}&select=id,status,question,answer,"
                  f"voice_delivered,updated_at&limit=1")  # Columns wanted; one row only.
        # Any non-200 status is a failed read.
        if status != 200:
            # Report the failure with the status for debugging.
            raise ValueError(f"queue read failed: HTTP {status}: {rows}")
        # Empty result means the id does not exist.
        if not rows:
            # None signals "not found" to the tool layer.
            return None
        # Tag the record with the honest mode label.
        rows[0]["mode"] = "live"
        # Return the stored queue record.
        return rows[0]

    # Search the shared arc_memory key/value table.
    def memory_search(self, query, limit=5):
        # Clamp the limit to a sane range (1..20).
        limit = max(1, min(int(limit or 5), 20))
        # Supabase must be configured for the live memory read.
        if not self._sb_ready():
            # Fail closed with a clear configuration message.
            raise ValueError("live mode needs ARC_SUPABASE_URL and "
                             "ARC_SUPABASE_KEY")  # Name the missing Supabase key.
        # Split the query into usable words for the OR filter.
        words = [w for w in query.lower().split() if len(w) >= 3]
        # A query with no usable words matches nothing.
        if not words:
            # Return the empty result shape.
            return {"matches": [], "mode": "live"}
        # Build a PostgREST OR filter: key or value ilike each word.
        ors = ",".join(
            # One ilike clause per word per column.
            f"key.ilike.*{w}*,value.ilike.*{w}*" for w in words)
        # URL-encode the OR expression for the query string.
        filt = urllib.parse.quote(ors)
        # Query the memory table with the filter and a row limit.
        status, rows = _sb_req(
            # GET matching memory rows.
            self.sb_url, self.sb_key, "GET", "/rest/v1/arc_memory",
            # Column selection, OR filter, ordering, limit.
            query=f"?select=key,value&or=({filt})&order=key.asc&limit={limit}")
        # Any non-200 status is a failed read.
        if status != 200:
            # Report the failure with the status for debugging.
            raise ValueError(f"memory read failed: HTTP {status}: {rows}")
        # Shape the rows for the tool response.
        matches = [
            # Expose key and value; live rows are never demo-labeled.
            {"key": r.get("key"), "value": r.get("value"), "demo": False}
            # Guard against a None body.
            for r in (rows or [])
        ]  # End the match list.
        # Return the match list plus the honest mode label.
        return {"matches": matches, "mode": "live"}

    # Live health: newest arc_heartbeats row plus queue depth.
    def health(self):
        # Supabase must be configured for the live health read.
        if not self._sb_ready():
            # Fail closed with a clear configuration message.
            raise ValueError("live mode needs ARC_SUPABASE_URL and "
                             "ARC_SUPABASE_KEY")  # Name the missing Supabase key.
        # Read the newest heartbeat row for the vm worker.
        status, rows = _sb_req(
            # GET the latest heartbeat.
            self.sb_url, self.sb_key, "GET", "/rest/v1/arc_heartbeats",
            # Filter to the vm worker, newest first, one row.
            query="?worker=eq.vm&select=ts,ok&order=ts.desc&limit=1")
        # Default to "unknown" when the heartbeat read fails.
        heartbeat_age_s = None
        # Track whether the last heartbeat reported ok.
        worker_ok = None
        # Parse the heartbeat only when the read succeeded with rows.
        if status == 200 and rows:
            # Parse the ISO-8601 timestamp from the heartbeat row.
            try:
                # fromisoformat handles the worker's UTC format.
                ts = datetime.datetime.fromisoformat(rows[0]["ts"])
                # Age is now minus the heartbeat time, in seconds.
                heartbeat_age_s = max(
                    0, (datetime.datetime.now(datetime.timezone.utc)  # Clamp at zero; now minus heartbeat time...
                        - ts).total_seconds())  # ...in seconds gives the heartbeat age.
                # Carry the worker's own ok flag through.
                worker_ok = rows[0].get("ok")
            # A malformed timestamp degrades to unknown, not a crash.
            except (ValueError, KeyError, TypeError):
                # Leave age as None (unknown).
                pass
        # Count unanswered questions in the live queue.
        qstatus, qrows = _sb_req(
            # GET the count of new/working queue rows.
            self.sb_url, self.sb_key, "GET", "/rest/v1/arc_alexa_queue",
            # Filter to open statuses, ask only for the count.
            query="?status=in.(new,working)&select=id")
        # Default the depth to None (unknown) when the read fails.
        queue_depth = len(qrows) if qstatus == 200 and qrows else None
        # Assemble the live health snapshot.
        return {
            # The worker identity matches the mesh's own heartbeat rows.
            "worker": "vm",
            # Seconds since the last heartbeat (None = unknown).
            "heartbeat_age_s": heartbeat_age_s,
            # The worker's own ok flag from its last heartbeat.
            "worker_ok": worker_ok,
            # Open questions waiting in the live queue.
            "queue_depth": queue_depth,
            # Token Factory/Nemotron configuration snapshot.
            "model": self.model.status(),  # Configured?, which Nemotron slug, what is missing.
            # Always label the mode honestly.
            "mode": "live",
            # Timestamp of this snapshot.
            "ts": _utcnow(),
        }  # End the live health snapshot.

    # Fan out a notification over the configured live channels (fail-closed).
    def notify(self, title, message, channel="log", priority=3):
        # Strip inputs; title and message are required.
        title = (title or "").strip()
        # Strip the message too.
        message = (message or "").strip()
        # Refuse empty titles instead of sending noise.
        if not title:
            # ValueError becomes an MCP isError result, not a crash.
            raise ValueError("title must not be empty")
        # Refuse empty messages for the same reason.
        if not message:
            # ValueError becomes an MCP isError result, not a crash.
            raise ValueError("message must not be empty")
        # Clamp priority into the 1..5 range.
        priority = max(1, min(int(priority or 3), 5))
        # Normalize the channel name to lowercase.
        channel = (channel or "log").lower()
        # Track per-channel delivery results for the report.
        delivered = {}
        # "all" fans out to every configured channel.
        targets = ("echo", "telegram", "ntfy") if channel == "all" else (channel,)
        # Attempt each requested channel independently.
        for target in targets:
            # Echo announcements ride Voice Monkey to his speaker.
            if target == "echo":
                # Attempt the Voice Monkey announce call.
                delivered["echo"] = self._notify_echo(message)
            # Telegram goes through the Bot API directly.
            elif target == "telegram":
                # Attempt the Telegram sendMessage call.
                delivered["telegram"] = self._notify_telegram(title, message,
                                                             priority)  # Attempt the Telegram sendMessage call.
            # ntfy.sh is the best-effort secondary channel.
            elif target == "ntfy":
                # Attempt the ntfy publish call.
                delivered["ntfy"] = self._notify_ntfy(title, message, priority)
            # "log" is not a remote channel; it is reported separately.
            elif target == "log":
                # Local log delivery always succeeds in live mode too.
                delivered["log"] = {"ok": True, "detail": "live mode: not "
                                                          "written locally; "  # Live mode keeps no local inbox...
                                                          "use the mesh inbox"}  # ...point at the mesh inbox instead.
            # Unknown channel names fail closed with a clear reason.
            else:
                # Report the unknown channel instead of guessing.
                delivered[target] = {"ok": False,
                                     "detail": f"unknown channel: {target}"}  # Report the bad channel name.
        # Return the per-channel delivery report.
        return {"delivered": delivered, "mode": "live"}

    # Announce text on the Echo via Voice Monkey; fail-closed when unconfigured.
    def _notify_echo(self, message):
        # Both the token and the speaker name must be configured.
        if not (self.vm_token and self.vm_speaker):
            # Fail closed with the exact missing pieces named.
            return {"ok": False, "detail": "VOICEMONKEY_TOKEN and "
                                           "VOICEMONKEY_SPEAKER not set"}  # Name the missing Voice Monkey pieces.
        # Build the announce payload exactly like the VM worker does.
        body = {"token": self.vm_token, "device": self.vm_speaker,
                "text": message}  # The speech text; close the payload.
        # Serialize the payload to JSON bytes.
        data = json.dumps(body).encode("utf-8")
        # Build the POST request to the Voice Monkey announce endpoint.
        req = urllib.request.Request(VOICEMONKEY_ANNOUNCE_URL, data=data,
                                     method="POST")  # POST to the announce endpoint.
        # JSON content type for the announce payload.
        req.add_header("Content-Type", "application/json")
        # Identify as the ARC bridge for Voice Monkey-side debugging.
        req.add_header("User-Agent", "ARC-Bridge-MCP/0.1.0")
        # Perform the call with a bounded timeout.
        try:
            # urlopen raises HTTPError for 4xx/5xx statuses.
            with urllib.request.urlopen(req,
                                        timeout=LIVE_TIMEOUT_S) as resp:  # Bounded timeout; read the response.
                # Voice Monkey answers 200 on accepted announcements.
                ok = resp.status == 200
                # Report success or the unexpected status.
                return {"ok": ok, "detail": f"HTTP {resp.status}"}
        # Any failure is reported, never raised.
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            # Include the failure reason (truncated) for debugging.
            return {"ok": False, "detail": str(exc)[:120]}

    # Send a Telegram message via the Bot API; fail-closed when unconfigured.
    def _notify_telegram(self, title, message, priority):
        # Both the bot token and the chat id must be configured.
        if not (self.tg_token and self.tg_chat):
            # Fail closed with the exact missing pieces named.
            return {"ok": False, "detail": "TELEGRAM_BOT_TOKEN and "
                                           "TELEGRAM_CHAT_ID not set"}  # Name the missing Telegram pieces.
        # High-priority alerts get the same warning prefix the worker uses.
        prefix = "⚠️ " if priority >= 4 else ""
        # Compose the message text within Telegram's 4096-char limit.
        text = f"{prefix}{title}\n{message}"[:4096]
        # Build the sendMessage payload.
        body = {"chat_id": self.tg_chat, "text": text}
        # Serialize the payload to JSON bytes.
        data = json.dumps(body).encode("utf-8")
        # Build the POST request to the bot's sendMessage endpoint.
        req = urllib.request.Request(TELEGRAM_API_BASE + self.tg_token
                                     + "/sendMessage", data=data, method="POST")  # POST to the bot's sendMessage endpoint.
        # JSON content type for the payload.
        req.add_header("Content-Type", "application/json")
        # A curl-style UA avoids the egress proxy's urllib 403 quirk.
        req.add_header("User-Agent", "curl/8.0")
        # Perform the call with a bounded timeout.
        try:
            # urlopen raises HTTPError for 4xx/5xx statuses.
            with urllib.request.urlopen(req,
                                        timeout=LIVE_TIMEOUT_S) as resp:  # Bounded timeout; read the response.
                # Parse the Bot API response envelope.
                parsed = json.loads(resp.read())
                # ok:true plus a message_id means it was delivered.
                ok = resp.status == 200 and parsed.get("ok") is True
                # Report success or the failure detail.
                return {"ok": ok, "detail": f"HTTP {resp.status}"}
        # Any failure is reported, never raised.
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            # Include the failure reason (truncated) for debugging.
            return {"ok": False, "detail": str(exc)[:120]}

    # Publish to ntfy.sh; fail-closed when no topic is configured.
    def _notify_ntfy(self, title, message, priority):
        # A topic must be configured for the publish to have a target.
        if not self.ntfy_topic:
            # Fail closed with the exact missing piece named.
            return {"ok": False, "detail": "NTFY_TOPIC not set"}
        # Build the topic URL on the public ntfy.sh host.
        url = NTFY_HOST + "/" + self.ntfy_topic
        # Build the PUT request with the message as the body.
        req = urllib.request.Request(url, data=message.encode("utf-8"),
                                     method="PUT")  # ntfy publishes via PUT.
        # The Title header becomes the notification title.
        req.add_header("Title", title[:200])
        # Map priority 4-5 to ntfy "urgent", 1-2 to "low", else default.
        req.add_header("Priority", "urgent" if priority >= 4
                       else ("low" if priority <= 2 else "default"))  # Priority 1-2 maps to ntfy "low".
        # A curl-style UA avoids the egress proxy's urllib 403 quirk.
        req.add_header("User-Agent", "curl/8.0")
        # Perform the call with a bounded timeout.
        try:
            # urlopen raises HTTPError for 4xx/5xx statuses.
            with urllib.request.urlopen(req,
                                        timeout=LIVE_TIMEOUT_S) as resp:  # Bounded timeout; read the response.
                # ntfy answers 200 on accepted publishes.
                ok = resp.status == 200
                # Report success or the unexpected status.
                return {"ok": ok, "detail": f"HTTP {resp.status}"}
        # Any failure is reported, never raised.
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            # Include the failure reason (truncated) for debugging.
            return {"ok": False, "detail": str(exc)[:120]}


# Choose the backend from ARC_BRIDGE_BACKEND ("local" default, "live" opt-in).
def make_backend(data_dir):
    # Read the backend selector; anything but "live" means local harness.
    mode = (_env("ARC_BRIDGE_BACKEND") or "local").lower()
    # "live" wires the real mesh; every other value is the local harness.
    if mode == "live":
        # LiveBackend reads its own credentials from the environment.
        return LiveBackend()
    # Default: the zero-config file-backed harness.
    return LocalBackend(data_dir)
