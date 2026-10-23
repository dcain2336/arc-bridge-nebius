# VENDORED from mini-mcp-harness v0.1.0 (../opensource-mini/mini-mcp-harness/).
# Upstream is the standalone package; this copy lets arc-bridge-mcp run
# with zero install steps. Do not edit here -- change upstream, re-vendor.

# mini-mcp-harness: a zero-dependency MCP server harness (Python stdlib only).
# Implements the MCP Streamable HTTP transport (spec 2025-11-25) plus an
# optional local OAuth 2.1 authorization server (PKCE, RFC 9728 metadata).
# Drop this single file into any project, register tools, serve. MIT license.
"""Zero-dependency MCP Streamable-HTTP + OAuth 2.1 harness (stdlib only)."""
# Import base64 for PKCE code_challenge hashing (S256) and token encoding.
import base64
# Import hashlib for SHA-256 (PKCE S256) and opaque token derivation.
import hashlib
# Import html for escaping user-controlled strings in the consent page.
import html
# Import http.server for the stdlib HTTP server and request handler base.
import http.server
# Import json for JSON-RPC message parsing and serialization.
import json
# Import secrets for cryptographically random tokens, codes, and verifiers.
import secrets
# Import socketserver for the threading mix-in (one thread per connection).
import socketserver
# Import threading for the session outbox lock and the SSE keep-alive timer.
import threading
# Import time for token/code expiry timestamps and SSE retry bookkeeping.
import time
# Import urllib.parse for query strings, form bodies, and redirect building.
import urllib.parse

# The MCP protocol version this harness speaks (spec 2025-11-25 or later).
MCP_PROTOCOL_VERSION = "2025-11-25"
# JSON-RPC error code: the request body was not valid JSON.
JSONRPC_PARSE_ERROR = -32700
# JSON-RPC error code: the JSON was valid but not a JSON-RPC request.
JSONRPC_INVALID_REQUEST = -32600
# JSON-RPC error code: the requested method does not exist.
JSONRPC_METHOD_NOT_FOUND = -32601
# JSON-RPC error code: the params failed validation for the method.
JSONRPC_INVALID_PARAMS = -32602
# JSON-RPC error code: something went wrong inside the method handler.
JSONRPC_INTERNAL_ERROR = -32603
# Default TCP port used by the example servers (override per deployment).
DEFAULT_PORT = 8765
# How long (seconds) an SSE GET stream idles before closing the connection.
SSE_IDLE_CLOSE_SECONDS = 2.0
# How long (seconds) an OAuth authorization code stays valid.
AUTH_CODE_TTL_SECONDS = 600
# How long (seconds) an OAuth access token stays valid.
ACCESS_TOKEN_TTL_SECONDS = 3600
# How long (seconds) an MCP session survives without any request.
SESSION_TTL_SECONDS = 1800


# A JSON-RPC 2.0 protocol error the dispatcher converts into an error reply.
class JSONRPCError(Exception):
    # Store the numeric code, human message, and optional extra data.
    def __init__(self, code, message, data=None):
        # Call the base Exception constructor with the message text.
        super().__init__(message)
        # Keep the JSON-RPC numeric error code on the instance.
        self.code = code
        # Keep any extra structured data for the error payload.
        self.data = data


# Build a successful JSON-RPC 2.0 response object for the given id/result.
def make_result(request_id, result):
    # Return the dict; the caller serializes it to JSON.
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


# Build a JSON-RPC 2.0 error response object for the given id/code/message.
def make_error(request_id, code, message, data=None):
    # Start with the required jsonrpc version marker and the request id.
    payload = {"jsonrpc": "2.0", "id": request_id}
    # Attach the error object with its numeric code and message.
    payload["error"] = {"code": code, "message": message}
    # Include extra data only when the caller supplied some.
    if data is not None:
        # Nest caller data under the error object per the JSON-RPC spec.
        payload["error"]["data"] = data
    # Hand the finished error payload back to the caller.
    return payload


# Compute the PKCE S256 code_challenge for a code_verifier string.
def pkce_challenge(verifier):
    # Hash the verifier bytes with SHA-256 per RFC 7636 section 4.2.
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    # Base64url-encode without padding to form the challenge string.
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# Generate a URL-safe opaque token with 256 bits of entropy.
def new_token():
    # secrets.token_urlsafe(32) gives 32 random bytes as URL-safe text.
    return secrets.token_urlsafe(32)


# A single registered MCP tool: name, description, schema, and handler.
class MCPTool:
    # Capture the tool's identity, docs, schema, and Python handler.
    def __init__(self, name, description, input_schema, handler):
        # The tool name as MCP clients will call it (e.g. "arc_ask").
        self.name = name
        # Human-readable description shown to models in tools/list.
        self.description = description
        # JSON Schema describing the tool's input arguments.
        self.input_schema = input_schema
        # The Python callable invoked on tools/call (args dict -> result).
        self.handler = handler

    # Render the tool in the shape MCP tools/list responses require.
    def to_dict(self):
        # Return name, description, and inputSchema keys per the MCP spec.
        return {
            "name": self.name,  # Tool name for tools/list.
            "description": self.description,  # Shown to models in tools/list.
            "inputSchema": self.input_schema,  # JSON Schema of the arguments.
        }  # End the tool wire dict.


# A single registered MCP resource: URI, name, MIME type, and reader.
class MCPResource:
    # Capture the resource's URI, display name, MIME type, and reader.
    def __init__(self, uri, name, mime_type, reader):
        # The resource URI clients pass to resources/read (e.g. arc://x).
        self.uri = uri
        # Human-readable name shown in resources/list.
        self.name = name
        # MIME type of the resource content (usually application/json).
        self.mime_type = mime_type
        # Callable returning (text_content) for resources/read.
        self.reader = reader

    # Render the resource in the shape MCP resources/list responses require.
    def to_dict(self):
        # Return uri, name, and mimeType keys per the MCP spec.
        return {"uri": self.uri, "name": self.name, "mimeType": self.mime_type}

# The MCP application: tool/resource registries plus the JSON-RPC dispatcher.
class MCPServer:
    # Set up empty registries and remember the server name and version.
    def __init__(self, name, version, instructions=""):
        # Public server name reported in initialize responses.
        self.name = name
        # Public server version reported in initialize responses.
        self.version = version
        # Optional instructions text shown to clients after initialize.
        self.instructions = instructions
        # Map of tool name -> MCPTool, filled by the tool() decorator.
        self._tools = {}
        # Map of resource URI -> MCPResource, filled by resource().
        self._resources = {}
        # Lock guarding registry reads during concurrent request handling.
        self._lock = threading.Lock()

    # Decorator registering a function as an MCP tool with schema.
    def tool(self, name, description, input_schema):
        # The outer call captures name/description/schema for the tool.
        def decorator(handler):
            # Build the MCPTool record wrapping the handler function.
            tool = MCPTool(name, description, input_schema, handler)
            # Register it under lock so concurrent reads stay consistent.
            with self._lock:
                # Store by name; later registrations overwrite earlier ones.
                self._tools[name] = tool
            # Return the original function unchanged for normal imports.
            return handler

        # Hand the decorator back so @server.tool(...) applies it.
        return decorator

    # Decorator registering a function as an MCP resource reader.
    def resource(self, uri, name, mime_type="application/json"):
        # The outer call captures uri/name/mime for the resource.
        def decorator(reader):
            # Build the MCPResource record wrapping the reader function.
            res = MCPResource(uri, name, mime_type, reader)
            # Register it under lock so concurrent reads stay consistent.
            with self._lock:
                # Store by URI; later registrations overwrite earlier ones.
                self._resources[uri] = res
            # Return the original function unchanged for normal imports.
            return reader

        # Hand the decorator back so @server.resource(...) applies it.
        return decorator

    # Handle one parsed JSON-RPC message; returns a reply dict or None.
    def handle_message(self, msg):
        # Anything that is not a dict cannot be a JSON-RPC message.
        if not isinstance(msg, dict):
            # -32600 Invalid Request: the payload shape is wrong.
            raise JSONRPCError(JSONRPC_INVALID_REQUEST, "message must be an object")
        # Read the jsonrpc version marker; default to "" when missing.
        if msg.get("jsonrpc") != "2.0":
            # -32600: only JSON-RPC 2.0 messages are accepted here.
            raise JSONRPCError(JSONRPC_INVALID_REQUEST, "jsonrpc must be '2.0'")
        # Read the method name; missing method means this is a reply, not ours.
        method = msg.get("method")
        # The id distinguishes requests (need replies) from notifications.
        request_id = msg.get("id")
        # Params default to an empty dict when the client omitted them.
        params = msg.get("params") or {}
        # A message without a method is a response to something we never sent.
        if method is None:
            # Silently drop it: we never issue client-bound requests ourselves.
            return None
        # Dispatch table mapping MCP method names to handler methods.
        handlers = {
            # initialize negotiates the protocol version and capabilities.
            "initialize": self._on_initialize,
            # notifications/initialized is the client's handshake completion.
            "notifications/initialized": self._on_initialized,
            # ping is a lightweight liveness check.
            "ping": self._on_ping,
            # tools/list advertises the registered tools.
            "tools/list": self._on_tools_list,
            # tools/call invokes one registered tool.
            "tools/call": self._on_tools_call,
            # resources/list advertises the registered resources.
            "resources/list": self._on_resources_list,
            # resources/read returns one resource's content.
            "resources/read": self._on_resources_read,
        }  # End the method dispatch table.
        # Look up the handler; unknown methods get -32601 Method not found.
        handler = handlers.get(method)
        # A missing handler means this method is not implemented by us.
        if handler is None:
            # Raise so the caller converts it into a JSON-RPC error reply.
            raise JSONRPCError(JSONRPC_METHOD_NOT_FOUND, f"unknown method: {method}")
        # Run the handler; it returns the result payload for the reply.
        result = handler(params)
        # Notifications (no id) never get a reply, even on success.
        if request_id is None:
            # Returning None tells the transport to send nothing back.
            return None
        # Wrap the handler result in a JSON-RPC success envelope.
        return make_result(request_id, result)

    # Handle a decoded request body: single message or batch array.
    def handle_body(self, body):
        # A JSON array body is a JSON-RPC batch: handle each element.
        if isinstance(body, list):
            # An empty batch array is invalid per JSON-RPC 2.0.
            if not body:
                # Raise a single Invalid Request error for the empty batch.
                raise JSONRPCError(JSONRPC_INVALID_REQUEST, "batch must not be empty")
            # Collect one reply per element; notifications yield None.
            replies = [self._safe_handle(msg) for msg in body]
            # Drop the Nones (notifications) so only real replies remain.
            replies = [r for r in replies if r is not None]
            # Return the reply list; empty means every element was a notification.
            return replies
        # A single object body goes through the same per-message path.
        return self._safe_handle(body)

    # Run handle_message but convert JSONRPCError into an error reply dict.
    def _safe_handle(self, msg):
        # Try the normal dispatch path first.
        try:
            # Return the reply dict, or None for notifications.
            return self.handle_message(msg)
        # JSONRPCError carries the code/message the client should see.
        except JSONRPCError as exc:
            # Pull the id for the reply; non-dicts get a null id.
            request_id = msg.get("id") if isinstance(msg, dict) else None
            # Build the error envelope, preserving any extra data.
            return make_error(request_id, exc.code, str(exc), exc.data)
        # Any other exception is an internal server error (-32603).
        except Exception as exc:  # noqa: BLE001 - must not leak tracebacks
            # Pull the id for the reply; non-dicts get a null id.
            request_id = msg.get("id") if isinstance(msg, dict) else None
            # Hide internals; the message stays generic on purpose.
            return make_error(request_id, JSONRPC_INTERNAL_ERROR, "internal error")

    # MCP initialize: negotiate version, report capabilities and server info.
    def _on_initialize(self, params):
        # Read the protocol version the client claims to speak.
        client_version = params.get("protocolVersion", "")
        # We only speak 2025-11-25; anything else gets our version back.
        # (Per spec the server replies with the version it will actually use.)
        negotiated = MCP_PROTOCOL_VERSION
        # Advertise exactly the capabilities this harness implements.
        capabilities = {
            # tools: listChanged false (registries are static after startup).
            "tools": {"listChanged": False},
            # resources: same static-registry note for resource lists.
            "resources": {"listChanged": False},
        }  # End the capabilities dict.
        # Identify this server to the client for debugging and logging.
        server_info = {"name": self.name, "version": self.version}
        # Assemble the initialize result per the MCP lifecycle spec.
        result = {
            # The protocol version the server will use for this session.
            "protocolVersion": negotiated,
            # The capability flags defined above.
            "capabilities": capabilities,
            # The server name/version identity block.
            "serverInfo": server_info,
        }  # End the initialize result dict.
        # Include usage instructions only when the app supplied some.
        if self.instructions:
            # instructions help the model use the tools correctly.
            result["instructions"] = self.instructions
        # Note the client's requested version for the friction log (unused).
        _ = client_version
        # Hand the result back for JSON-RPC envelope wrapping.
        return result

    # notifications/initialized: handshake done; nothing to return.
    def _on_initialized(self, params):
        # Params are unused; the notification itself is the signal.
        _ = params
        # Return an empty result (dropped anyway: notifications get no reply).
        return {}

    # ping: answer with an empty object to prove liveness.
    def _on_ping(self, params):
        # Params are unused for ping.
        _ = params
        # Empty object is the conventional ping reply.
        return {}

    # tools/list: return every registered tool in MCP shape.
    def _on_tools_list(self, params):
        # Params are unused; the registry is static.
        _ = params
        # Snapshot the registry under lock for a consistent list.
        with self._lock:
            # Convert each MCPTool to its wire dict.
            tools = [t.to_dict() for t in self._tools.values()]
        # The MCP result wraps the array under the "tools" key.
        return {"tools": tools}

    # tools/call: validate args minimally, run the handler, wrap the result.
    def _on_tools_call(self, params):
        # The tool name is required in every tools/call request.
        name = params.get("name")
        # Arguments default to {} when the client sent none.
        arguments = params.get("arguments") or {}
        # Arguments must be an object per the MCP spec.
        if not isinstance(arguments, dict):
            # -32602: wrong-typed arguments are a client error.
            raise JSONRPCError(JSONRPC_INVALID_PARAMS, "arguments must be an object")
        # Look the tool up under lock for thread safety.
        with self._lock:
            # None when no tool was registered under this name.
            tool = self._tools.get(name)
        # Unknown tool names are a client error, not a crash.
        if tool is None:
            # -32602 keeps it a clean "you asked wrong" signal.
            raise JSONRPCError(JSONRPC_INVALID_PARAMS, f"unknown tool: {name}")
        # Run the handler; tool errors become isError results, not transport errors.
        try:
            # Handlers return JSON-serializable values (dict/list/str/...).
            output = tool.handler(arguments)
        # A ValueError from a handler means bad arguments (client's fault).
        except ValueError as exc:
            # Return the MCP tool-error shape instead of raising.
            return {"content": [{"type": "text", "text": f"error: {exc}"}],
                    "isError": True}  # Flag the result as a tool error.
        # Any other exception is a tool failure; report it, don't crash.
        except Exception as exc:  # noqa: BLE001 - tools must fail soft
            # Same isError envelope with a generic message.
            return {"content": [{"type": "text", "text": f"tool failed: {exc}"}],
                    "isError": True}  # Flag the result as a tool error.
        # Serialize the handler output so content is always a string.
        text = output if isinstance(output, str) else json.dumps(output, indent=2)
        # Normal success: MCP content array with one text block.
        return {"content": [{"type": "text", "text": text}], "isError": False}

    # resources/list: return every registered resource in MCP shape.
    def _on_resources_list(self, params):
        # Params are unused; the registry is static.
        _ = params
        # Snapshot the registry under lock for a consistent list.
        with self._lock:
            # Convert each MCPResource to its wire dict.
            resources = [r.to_dict() for r in self._resources.values()]
        # The MCP result wraps the array under the "resources" key.
        return {"resources": resources}

    # resources/read: run the reader and return its content as text.
    def _on_resources_read(self, params):
        # The resource URI is required in every resources/read request.
        uri = params.get("uri")
        # Look the resource up under lock for thread safety.
        with self._lock:
            # None when no resource was registered for this URI.
            res = self._resources.get(uri)
        # Unknown URIs are a client error.
        if res is None:
            # -32602 keeps it a clean "you asked wrong" signal.
            raise JSONRPCError(JSONRPC_INVALID_PARAMS, f"unknown resource: {uri}")
        # Run the reader; failures here are server errors.
        content = res.reader()
        # Readers may return dicts; serialize non-strings to JSON text.
        text = content if isinstance(content, str) else json.dumps(content, indent=2)
        # MCP resource content block with uri and mimeType.
        return {"contents": [{"uri": uri, "mimeType": res.mime_type, "text": text}]}

# One MCP session: id, creation time, and the SSE outbox event log.
class Session:
    # Create a session with a fresh id, timestamp, and empty outbox.
    def __init__(self, session_id):
        # The opaque session id sent in the Mcp-Session-Id header.
        self.session_id = session_id
        # Creation time (seconds since epoch) for TTL expiry checks.
        self.created_at = time.time()
        # Last activity time; refreshed on every request in this session.
        self.last_seen = self.created_at
        # Outbox: list of (event_id, method, params) pending SSE delivery.
        self.outbox = []
        # Monotonic counter so SSE event ids are unique per session.
        self._event_seq = 0
        # Lock protecting the outbox across request threads.
        self._lock = threading.Lock()

    # Queue a server-initiated notification for SSE delivery; returns event id.
    def push_event(self, method, params):
        # Build the event under lock so ids stay ordered.
        with self._lock:
            # Increment first so event ids start at 1.
            self._event_seq += 1
            # Event id combines session id and sequence for uniqueness.
            event_id = f"{self.session_id}:{self._event_seq}"
            # Append the tuple; GET streams drain this list.
            self.outbox.append((event_id, method, params))
        # Refresh activity so an active stream does not expire mid-use.
        self.touch()
        # Return the event id for Last-Event-ID resumability bookkeeping.
        return event_id

    # Drain outbox events newer than last_event_id; returns list of tuples.
    def drain_events(self, last_event_id=None):
        # Snapshot and clear under lock so each event is delivered once.
        with self._lock:
            # Take the current outbox contents...
            events = list(self.outbox)
            # ...and clear it so redelivery only happens via replay log.
            self.outbox = []
        # No Last-Event-ID means the client wants everything pending.
        if not last_event_id:
            # Return all drained events in order.
            return events
        # Otherwise skip events the client already saw (resumability).
        # (The replay log below re-adds history when asked; see SessionManager.)
        return [e for e in events if e[0] > last_event_id]

    # Refresh the last-activity timestamp to the current time.
    def touch(self):
        # Update last_seen so TTL expiry measures idle time, not age.
        self.last_seen = time.time()

    # True when the session has been idle longer than SESSION_TTL_SECONDS.
    def expired(self):
        # Compare now against last activity plus the TTL window.
        return (time.time() - self.last_seen) > SESSION_TTL_SECONDS


# Thread-safe registry of live MCP sessions with an SSE replay log.
class SessionManager:
    # Start with an empty session map and replay log.
    def __init__(self):
        # Map of session id -> Session object.
        self._sessions = {}
        # Replay log: session id -> list of all (event_id, method, params).
        self._replay = {}
        # One lock guarding both maps.
        self._lock = threading.Lock()

    # Create a session, store it, and return the Session object.
    def create(self):
        # Generate an opaque session id (256 bits of entropy, URL-safe).
        session_id = new_token()
        # Build the Session record around the new id.
        session = Session(session_id)
        # Store under lock so concurrent creates stay consistent.
        with self._lock:
            # Index the live session by its id.
            self._sessions[session_id] = session
            # Start an empty replay log for resumable SSE delivery.
            self._replay[session_id] = []
        # Hand the live Session object back to the caller.
        return session

    # Look up a live session by id; prunes expired ones; None when missing.
    def get(self, session_id):
        # Guard the map while checking and pruning.
        with self._lock:
            # Unknown ids (or empty) mean "no such session".
            session = self._sessions.get(session_id)
            # Missing session: nothing more to do.
            if session is None:
                # Return None so the handler can answer 404.
                return None
            # Expired sessions are dropped as if they never existed.
            if session.expired():
                # Remove the live entry...
                del self._sessions[session_id]
                # ...and its replay history to bound memory.
                self._replay.pop(session_id, None)
                # Report it as missing so the client re-initializes.
                return None
        # Refresh activity outside the lock (touch is cheap and thread-safe).
        session.touch()
        # Return the live session for request handling.
        return session

    # Queue a server-initiated notification; records it in the replay log too.
    def notify(self, session_id, method, params):
        # Fetch the session (None when unknown or expired).
        session = self.get(session_id)
        # No session means there is nobody to notify.
        if session is None:
            # Return None so callers can log the miss.
            return None
        # Push into the live outbox and get the event id back.
        event_id = session.push_event(method, params)
        # Mirror the event into the replay log under lock.
        with self._lock:
            # Append so Last-Event-ID replays can find it later.
            self._replay.setdefault(session_id, []).append((event_id, method, params))
            # Bound the replay log to the newest 200 events per session.
            self._replay[session_id] = self._replay[session_id][-200:]
        # Return the event id for the caller's bookkeeping.
        return event_id

    # Replay events newer than last_event_id for SSE resumability.
    def replay(self, session_id, last_event_id):
        # Guard the replay map while slicing it.
        with self._lock:
            # Unknown sessions have no history to replay.
            events = self._replay.get(session_id, [])
            # No Last-Event-ID means "send everything you still have".
            if not last_event_id:
                # Return a copy so callers cannot mutate the log.
                return list(events)
            # Otherwise return only events the client has not seen yet.
            return [e for e in events if e[0] > last_event_id]

    # Delete a session and its replay history; True when something was removed.
    def delete(self, session_id):
        # Guard both maps during removal.
        with self._lock:
            # pop returns None when the session was already gone.
            removed = self._sessions.pop(session_id, None)
            # Drop the replay history to bound memory.
            self._replay.pop(session_id, None)
        # True/False tells the handler whether to answer 200 or 404.
        return removed is not None


# Validate a decoded JSON-RPC body shape before dispatching it.
def _check_body_shape(body):
    # The body must be a dict (single message) or list (batch).
    if not isinstance(body, (dict, list)):
        # Anything else (string, number) is an Invalid Request.
        raise JSONRPCError(JSONRPC_INVALID_REQUEST, "body must be object or array")

# Build a request-handler class bound to one MCPServer/SessionManager pair.
def make_handler(mcp_server, sessions, auth=None, allowed_origins=None,
                 extra_routes=None):  # Extra (method, path) routes, e.g. OAuth.
    # Normalize the explicit origin allowlist into a set for fast lookup.
    allowed = set(allowed_origins or [])

    # The per-server handler class; created fresh for each server instance.
    class StreamableHTTPHandler(http.server.BaseHTTPRequestHandler):
        # Silence the default server/version banner (less fingerprinting).
        server_version = "MCPHarness"
        # Match the banner above for the sys_version half of the header.
        sys_version = ""

        # Extra (method, path) -> handler-name routes (e.g. OAuth endpoints).
        _extra = dict(extra_routes or {})

        # --- small response helpers -------------------------------------
        # Send a JSON body with the given HTTP status and extra headers.
        def _send_json(self, status, obj, headers=None):
            # Serialize the payload; failures here are programmer errors.
            body = json.dumps(obj).encode("utf-8")
            # Write the status line first.
            self.send_response(status)
            # JSON responses always declare their content type and length.
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))  # Declare the exact body length.
            # Merge any caller-supplied extra headers (e.g. Mcp-Session-Id).
            for key, value in (headers or {}).items():
                # Add each extra header verbatim.
                self.send_header(key, value)
            # Blank line ends the header section.
            self.end_headers()
            # Write the serialized JSON body to the socket.
            self.wfile.write(body)

        # Send an empty body with the given HTTP status and extra headers.
        def _send_empty(self, status, headers=None):
            # Write the status line first.
            self.send_response(status)
            # Merge any caller-supplied extra headers.
            for key, value in (headers or {}).items():
                # Add each extra header verbatim.
                self.send_header(key, value)
            # Blank line ends the header section (no body follows).
            self.end_headers()

        # --- request plumbing -------------------------------------------
        # Read and JSON-parse the request body; raises JSONRPCError on trouble.
        def _read_json_body(self):
            # Content-Length tells us exactly how many bytes to read.
            length = int(self.headers.get("Content-Length", "0") or 0)
            # A missing/empty body cannot be a JSON-RPC message.
            if length <= 0:
                # -32600 Invalid Request for the empty-body case.
                raise JSONRPCError(JSONRPC_INVALID_REQUEST, "empty request body")
            # Read exactly length bytes from the request stream.
            raw = self.rfile.read(length)
            # Decode as UTF-8; undecodable bytes are a parse error.
            try:
                # Strict UTF-8 decode of the raw request bytes.
                text = raw.decode("utf-8")
            # UnicodeDecodeError means the bytes were not valid UTF-8.
            except UnicodeDecodeError:
                # -32700 Parse error per JSON-RPC for undecodable input.
                raise JSONRPCError(JSONRPC_PARSE_ERROR, "body is not valid UTF-8")
            # Parse the JSON text into Python objects.
            try:
                # json.loads raises on malformed JSON text.
                return json.loads(text)
            # ValueError covers json.JSONDecodeError (its subclass).
            except ValueError:
                # -32700 Parse error per JSON-RPC for malformed JSON.
                raise JSONRPCError(JSONRPC_PARSE_ERROR, "body is not valid JSON")

        # Enforce the Origin allowlist (DNS-rebinding defense); True when ok.
        def _check_origin(self):
            # Browsers send Origin; curl/scripts usually send none.
            origin = self.headers.get("Origin")
            # No Origin header means a non-browser client: allow it.
            if not origin:
                # Non-browser clients are not subject to rebinding checks.
                return True
            # Parse the origin so we can inspect its host separately.
            parts = urllib.parse.urlparse(origin)
            # Localhost origins (any port) are always allowed for development.
            if parts.hostname in ("localhost", "127.0.0.1", "::1"):
                # Developer loopback: safe against DNS rebinding by definition.
                return True
            # Otherwise the full origin string must be explicitly allowlisted.
            if origin in allowed:
                # Explicitly configured trusted origin.
                return True
            # Anything else is rejected to block DNS-rebinding attacks.
            return False

        # Enforce bearer auth on /mcp when an OAuthServer is configured.
        def _check_auth(self):
            # No auth server means the deployment chose the open local mode.
            if auth is None:
                # Nothing to check; the request proceeds.
                return True
            # Read the Authorization header the client sent.
            header = self.headers.get("Authorization", "")
            # It must be exactly "Bearer <token>" (case-sensitive scheme).
            if not header.startswith("Bearer "):
                # Missing or wrong scheme: fail closed with 401 below.
                self._auth_challenge("invalid_request")
                # Return False so the caller stops handling the request.
                return False
            # Strip the "Bearer " prefix to get the opaque token value.
            token = header[len("Bearer "):].strip()
            # Ask the OAuth server whether this token is live and unexpired.
            if not auth.validate_token(token):
                # Unknown or expired token: fail closed with 401 below.
                self._auth_challenge("invalid_token")
                # Return False so the caller stops handling the request.
                return False
            # The token is valid; the request may proceed.
            return True

        # Answer 401 with the RFC 9728 WWW-Authenticate challenge header.
        def _auth_challenge(self, error):
            # The metadata URL tells the client where to start OAuth.
            metadata_url = auth.issuer.rstrip("/") + "/.well-known/oauth-protected-resource"
            # Build the Bearer challenge with resource metadata per RFC 9728.
            challenge = (f'Bearer resource_metadata="{metadata_url}", '
                         f'error="{error}"')  # Name the OAuth error in the challenge.
            # 401 with the challenge; no body needed.
            self._send_empty(401, {"WWW-Authenticate": challenge})

        # Pull the Mcp-Session-Id header value, or "" when absent.
        def _session_id(self):
            # Header lookup is case-insensitive in BaseHTTPRequestHandler.
            return (self.headers.get("Mcp-Session-Id") or "").strip()

        # --- routing ------------------------------------------------------
        # Route POST requests: /mcp plus any registered extra POST routes.
        def do_POST(self):
            # Split off any query string; routing uses the path only.
            path = urllib.parse.urlparse(self.path).path
            # The MCP endpoint handles all Streamable HTTP client messages.
            if path == "/mcp":
                # Delegate to the MCP POST pipeline.
                self._handle_mcp_post()
                # Done; do not fall through to extra routes.
                return
            # Look for an extra route registered for (POST, path).
            target = self._extra.get(("POST", path))
            # Unknown POST paths are a clean 404.
            if target is None:
                # No route matched: standard not-found response.
                self._send_empty(404)
                # Stop here.
                return
            # Dispatch to the bound extra-route handler method.
            target(self)

        # Route GET requests: /mcp (SSE) plus registered extra GET routes.
        def do_GET(self):
            # Split off any query string; routing uses the path only.
            path = urllib.parse.urlparse(self.path).path
            # GET /mcp opens the server-to-client SSE notification stream.
            if path == "/mcp":
                # Delegate to the MCP SSE pipeline.
                self._handle_mcp_get()
                # Done; do not fall through to extra routes.
                return
            # Look for an extra route registered for (GET, path).
            target = self._extra.get(("GET", path))
            # Unknown GET paths are a clean 404.
            if target is None:
                # No route matched: standard not-found response.
                self._send_empty(404)
                # Stop here.
                return
            # Dispatch to the bound extra-route handler method.
            target(self)

        # Route DELETE requests: only /mcp (session termination) is valid.
        def do_DELETE(self):
            # Split off any query string; routing uses the path only.
            path = urllib.parse.urlparse(self.path).path
            # DELETE is only meaningful on the MCP endpoint (close session).
            if path != "/mcp":
                # Anything else is a clean 404.
                self._send_empty(404)
                # Stop here.
                return
            # Delegate to the session-termination pipeline.
            self._handle_mcp_delete()

        # --- MCP Streamable HTTP pipelines --------------------------------
        # Handle POST /mcp: one JSON-RPC message or batch from the client.
        def _handle_mcp_post(self):
            # Reject cross-origin browser requests that are not allowlisted.
            if not self._check_origin():
                # 403 without a body: fail closed on origin mismatch.
                self._send_empty(403)
                # Stop here.
                return
            # Enforce bearer auth when the deployment enabled OAuth.
            if not self._check_auth():
                # _check_auth already sent the 401 challenge.
                return
            # Read and parse the JSON body; errors become JSON-RPC replies.
            try:
                # May raise JSONRPCError for empty/undecodable/malformed bodies.
                body = self._read_json_body()
                # Reject top-level non-object/array shapes early.
                _check_body_shape(body)
            # JSONRPCError here means the body itself was unusable.
            except JSONRPCError as exc:
                # Malformed input is an HTTP 400 with a JSON-RPC error body.
                self._send_json(400, make_error(None, exc.code, str(exc)))
                # Stop here.
                return
            # Find whether this payload contains an initialize request.
            initializing = self._is_initialize(body)
            # Non-initialize requests must carry a live session id.
            session = None
            # Initialize is the one call allowed without a session id.
            if not initializing:
                # Read the session id the client sent.
                sid = self._session_id()
                # Look it up (prunes expired sessions internally).
                session = sessions.get(sid)
                # Unknown/expired/missing session ids get a clean 404.
                if session is None:
                    # Client must re-run initialize to get a fresh session.
                    self._send_json(404, make_error(
                        None, JSONRPC_INVALID_REQUEST, "unknown session"))  # The session id was missing or expired.
                    # Stop here.
                    return
            # Dispatch the body through the MCP server (single or batch).
            reply = mcp_server.handle_body(body)
            # Notifications-only payloads produce no reply: HTTP 202.
            if reply is None or reply == []:
                # 202 Accepted: the notifications were received.
                self._send_empty(202)
                # Stop here.
                return
            # Extra response headers start empty.
            extra = {}
            # Initialize responses mint a brand-new session for the client.
            if initializing:
                # Create and register the session now.
                session = sessions.create()
                # Tell the client its id via the Mcp-Session-Id header.
                extra["Mcp-Session-Id"] = session.session_id
            # Optional protocol-version echo for debugging clients.
            extra["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
            # Batch replies serialize as a JSON array; singles as an object.
            self._send_json(200, reply, extra)

        # True when the body is (or contains) an MCP initialize request.
        def _is_initialize(self, body):
            # Normalize to a list so single and batch share the check.
            messages = body if isinstance(body, list) else [body]
            # Scan every message in the payload.
            for msg in messages:
                # Only dict messages can carry a method name.
                if isinstance(msg, dict) and msg.get("method") == "initialize":
                    # Found it: this payload starts a new session.
                    return True
            # No initialize request present in this payload.
            return False

        # Handle GET /mcp: SSE stream of server-initiated session events.
        def _handle_mcp_get(self):
            # Reject cross-origin browser requests that are not allowlisted.
            if not self._check_origin():
                # 403 without a body: fail closed on origin mismatch.
                self._send_empty(403)
                # Stop here.
                return
            # Enforce bearer auth when the deployment enabled OAuth.
            if not self._check_auth():
                # _check_auth already sent the 401 challenge.
                return
            # SSE streams are always tied to a live session.
            session = sessions.get(self._session_id())
            # Unknown/expired/missing session ids get a clean 404.
            if session is None:
                # Client must re-run initialize to get a fresh session.
                self._send_json(404, make_error(
                    None, JSONRPC_INVALID_REQUEST, "unknown session"))  # The session id was missing or expired.
                # Stop here.
                return
            # Read Last-Event-ID so resumed streams skip already-seen events.
            last_id = self.headers.get("Last-Event-ID")
            # Merge replay history (for resumes) with the live outbox drain.
            pending = sessions.replay(session.session_id, last_id)
            # Open the SSE response: 200 with the event-stream content type.
            self.send_response(200)
            # SSE requires this exact content type.
            self.send_header("Content-Type", "text/event-stream")
            # Disable proxy/request buffering so events flush immediately.
            self.send_header("Cache-Control", "no-cache")
            # Close the connection when the handler returns: with no
            # Content-Length, "close" is the only framing that lets the
            # client see end-of-stream (keep-alive would hang the read).
            self.send_header("Connection", "close")
            # Blank line ends the header section; events follow.
            self.end_headers()
            # Wait briefly for events when the outbox starts empty.
            deadline = time.time() + SSE_IDLE_CLOSE_SECONDS
            # Loop until the idle deadline, flushing events as they arrive.
            while time.time() < deadline:
                # Drain anything queued since the last loop iteration.
                for event_id, method, params in session.drain_events():
                    # Write one SSE event: id, event name, JSON data, blank line.
                    self._write_sse(event_id, method, params)
                # Also include replay items not yet sent on this connection.
                for event_id, method, params in pending:
                    # Same SSE framing for replayed history events.
                    self._write_sse(event_id, method, params)
                # Clear the replay batch so it is not sent twice.
                pending = []
                # If we sent at least one event, keep waiting for more.
                # (Falls through to the sleep below; loop re-checks deadline.)
                # Sleep 100ms between polls to avoid a hot spin.
                time.sleep(0.1)
            # Idle window elapsed: close the stream (client may reconnect).

        # Write one SSE-formatted event (id + event + data) to the stream.
        def _write_sse(self, event_id, method, params):
            # Build the JSON-RPC notification payload for this event.
            payload = json.dumps({"jsonrpc": "2.0", "method": method,
                                  "params": params})  # Event parameters for the SSE payload.
            # SSE framing: id line, event line, data line, blank line.
            frame = (f"id: {event_id}\nevent: message\n"
                     f"data: {payload}\n\n")  # Data line plus the blank line ending the event.
            # Encode and flush immediately so the client sees it at once.
            self.wfile.write(frame.encode("utf-8"))
            # Flush the socket buffer now (no Nagle-style delay for events).
            self.wfile.flush()

        # Handle DELETE /mcp: terminate the session named by Mcp-Session-Id.
        def _handle_mcp_delete(self):
            # Reject cross-origin browser requests that are not allowlisted.
            if not self._check_origin():
                # 403 without a body: fail closed on origin mismatch.
                self._send_empty(403)
                # Stop here.
                return
            # Enforce bearer auth when the deployment enabled OAuth.
            if not self._check_auth():
                # _check_auth already sent the 401 challenge.
                return
            # Read the session id the client wants to terminate.
            sid = self._session_id()
            # Delete it; False means it was already gone.
            if sessions.delete(sid):
                # 200: the session was terminated.
                self._send_empty(200)
            # Unknown session ids get a clean 404.
            else:
                # Nothing to terminate; tell the client plainly.
                self._send_empty(404)

        # Quiet the default logging; uncomment to debug request traffic.
        def log_message(self, fmt, *args):  # noqa: D102 - stdlib override
            # Write one line per request to stderr for operator visibility.
            import sys as _sys
            # Prefix with the client address for log correlation.
            _sys.stderr.write(f"{self.client_address[0]} - {fmt % args}\n")

    # Hand the fully-bound handler class back to the caller.
    return StreamableHTTPHandler


# Run a bound handler class on host:port with one thread per connection.
def serve(handler_class, host="127.0.0.1", port=DEFAULT_PORT):
    # ThreadingTCPServer handles each connection in its own thread.
    with socketserver.ThreadingTCPServer((host, port), handler_class) as httpd:
        # Allow immediate rebinding after a restart (no TIME_WAIT stall).
        httpd.allow_reuse_address = True
        # Block here serving requests until the process is interrupted.
        httpd.serve_forever()

# Minimal OAuth 2.1 authorization server: dynamic client registration (RFC 7591),
# authorization-code flow with PKCE S256, refresh-token rotation, and RFC 9728
# protected-resource metadata. In-memory stores: fine for self-hosted demos and
# single-instance deployments; a multi-instance deployment would persist these.
class OAuthServer:
    # Remember the public issuer URL and start with empty stores.
    def __init__(self, issuer):
        # The public base URL clients use (e.g. https://mcp.example.com).
        self.issuer = issuer.rstrip("/")
        # Registered clients: client_id -> {client_secret, redirect_uris}.
        self._clients = {}
        # Authorization codes: code -> {client_id, redirect_uri, challenge,
        # scope, exp, used}.
        self._codes = {}
        # Access tokens: token -> {client_id, scope, exp}.
        self._tokens = {}
        # Refresh tokens: token -> {client_id, scope, exp}.
        self._refresh = {}
        # One lock guarding all four stores.
        self._lock = threading.Lock()

    # --- RFC 9728 protected-resource metadata ---------------------------
    # Return the protected-resource metadata document for /.well-known/.
    def protected_resource_metadata(self):
        # resource is this server's own base URL.
        # authorization_servers lists where clients can get tokens.
        # scopes_supported advertises the scopes this server understands.
        # bearer_methods_supported says tokens ride the Authorization header.
        return {
            # The protected resource identifier (this MCP server).
            "resource": self.issuer,
            # The authorization server(s) able to issue tokens for it.
            "authorization_servers": [self.issuer],
            # Scope names clients may request.
            "scopes_supported": ["mcp:tools", "mcp:resources"],
            # Tokens travel in the Authorization header only.
            "bearer_methods_supported": ["header"],
        }  # End the protected-resource metadata dict.

    # --- RFC 7591 dynamic client registration ---------------------------
    # Register a client; body is the parsed JSON registration request.
    def register_client(self, body):
        # redirect_uris is required and must be a non-empty list.
        redirect_uris = body.get("redirect_uris") or []
        # Reject missing or wrong-typed redirect_uris up front.
        if not isinstance(redirect_uris, list) or not redirect_uris:
            # Raise a ValueError the HTTP layer converts to a 400.
            raise ValueError("redirect_uris must be a non-empty list")
        # Every redirect URI must be a valid http(s) URL.
        for uri in redirect_uris:
            # Parse each candidate URI.
            parts = urllib.parse.urlparse(uri)
            # Only http and https schemes are acceptable.
            if parts.scheme not in ("http", "https") or not parts.netloc:
                # Reject the whole registration on the first bad URI.
                raise ValueError(f"invalid redirect_uri: {uri}")
        # Mint the client credentials under lock.
        with self._lock:
            # Opaque public client identifier.
            client_id = new_token()
            # Opaque secret for confidential clients (may be unused by MCP).
            client_secret = new_token()
            # Store the client record for later authorize/token checks.
            self._clients[client_id] = {
                # Keep the secret for token-endpoint client authentication.
                "client_secret": client_secret,
                # Keep the exact registered redirect URI list.
                "redirect_uris": list(redirect_uris),
            }  # End the stored client record.
        # Return the RFC 7591 registration response to the client.
        return {
            # The new client's public identifier.
            "client_id": client_id,
            # The new client's secret (shown only once, at registration).
            "client_secret": client_secret,
            # Echo the registered redirect URIs back.
            "redirect_uris": list(redirect_uris),
            # This server issues secrets that never expire on their own.
            "client_secret_expires_at": 0,
        }  # End the RFC 7591 registration response.

    # --- authorization endpoint -----------------------------------------
    # Validate authorize params; returns (client, redirect_uri, scope, state,
    # code_challenge) or raises ValueError for a redirect-with-error.
    def _validate_authorize(self, params):
        # response_type must be exactly "code" (authorization-code flow only).
        if params.get("response_type") != "code":
            # Anything else is unsupported by this server.
            raise ValueError("only response_type=code is supported")
        # client_id identifies the registered OAuth client.
        client_id = params.get("client_id", "")
        # Look the client up under lock.
        with self._lock:
            # None when the client_id was never registered here.
            client = self._clients.get(client_id)
        # Unknown clients cannot proceed.
        if client is None:
            # Raise before touching redirect_uri (it may be attacker input).
            raise ValueError("unknown client_id")
        # redirect_uri must exactly match one registered URI (exact-match rule).
        redirect_uri = params.get("redirect_uri", "")
        # Exact string match against the registered list (no fuzzy matching).
        if redirect_uri not in client["redirect_uris"]:
            # Mismatched redirect targets are rejected outright.
            raise ValueError("redirect_uri is not registered for this client")
        # PKCE code_challenge is required (OAuth 2.1 mandates PKCE).
        challenge = params.get("code_challenge", "")
        # Empty challenges are rejected: PKCE is not optional here.
        if not challenge:
            # The client must send a challenge with the authorize request.
            raise ValueError("code_challenge is required")
        # Only the S256 challenge method is supported (no plain).
        if params.get("code_challenge_method", "S256") != "S256":
            # "plain" would leak the verifier in server logs; refuse it.
            raise ValueError("only code_challenge_method=S256 is supported")
        # scope is optional; default to the tools scope when omitted.
        scope = params.get("scope", "mcp:tools")
        # state is optional but echoed back when the client sent one.
        state = params.get("state", "")
        # Return the validated pieces for the consent page / approval step.
        return client, redirect_uri, scope, state, challenge

    # Render the human consent page HTML for the authorize request.
    def consent_page(self, client_id, redirect_uri, scope, state, challenge):
        # Escape every reflected value against HTML injection.
        cid = html.escape(client_id)
        # redirect_uri is shown so the user sees where approval goes.
        ruri = html.escape(redirect_uri)
        # Scope text is shown so the user knows what is being granted.
        scp = html.escape(scope)
        # state/challenge ride along as hidden form fields.
        st = html.escape(state)
        # The challenge must survive the round-trip untouched.
        ch = html.escape(challenge)
        # Build a minimal, dependency-free consent form posting to /oauth/approve.
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Authorize MCP access</title></head>
<body style="font-family:sans-serif;max-width:36em;margin:3em auto">
<h2>Authorize MCP access</h2>
<p>Client <code>{cid}</code> is requesting access to this MCP server
with scope <code>{scp}</code>.</p>
<p>On approval you will be redirected to <code>{ruri}</code>.</p>
<form method="post" action="/oauth/approve">
<input type="hidden" name="client_id" value="{cid}">
<input type="hidden" name="redirect_uri" value="{ruri}">
<input type="hidden" name="scope" value="{scp}">
<input type="hidden" name="state" value="{st}">
<input type="hidden" name="code_challenge" value="{ch}">
<button type="submit" name="decision" value="allow">Allow</button>
<button type="submit" name="decision" value="deny">Deny</button>
</form></body></html>"""

    # Process the consent decision; returns the 302 redirect Location URL.
    def approve(self, form):
        # The decision comes from which submit button the user pressed.
        decision = form.get("decision", "")
        # Re-validate everything from the hidden fields (never trust the form).
        client_id = form.get("client_id", "")
        # redirect_uri must still exactly match a registered URI.
        redirect_uri = form.get("redirect_uri", "")
        # Scope granted is what the consent page displayed.
        scope = form.get("scope", "mcp:tools")
        # state echoes back to the client untouched.
        state = form.get("state", "")
        # The PKCE challenge binds the code to the future token request.
        challenge = form.get("code_challenge", "")
        # Look the client up again under lock (it may have been deleted).
        with self._lock:
            # None when the client_id is unknown.
            client = self._clients.get(client_id)
        # Unknown client or unregistered redirect: refuse without redirecting.
        if client is None or redirect_uri not in client["redirect_uris"]:
            # Raise so the HTTP layer answers 400 (no safe redirect target).
            raise ValueError("unknown client or redirect_uri")
        # A denied consent redirects back with error=access_denied per RFC 6749.
        if decision != "allow":
            # Build the error redirect: redirect_uri?error=access_denied&state=...
            err = urllib.parse.urlencode({"error": "access_denied", "state": state})
            # Return the full redirect Location for the HTTP layer to send.
            return f"{redirect_uri}?{err}" if "?" not in redirect_uri else \
                f"{redirect_uri}&{err}"  # The URI already had a query string; append.
        # Consent granted: mint a single-use authorization code.
        code = new_token()
        # Store the code bound to client, redirect, challenge, and scope.
        with self._lock:
            # The code record enforces every binding at redemption time.
            self._codes[code] = {
                # The client this code was issued to.
                "client_id": client_id,
                # The exact redirect_uri it must be redeemed with.
                "redirect_uri": redirect_uri,
                # The S256 challenge the verifier must match.
                "challenge": challenge,
                # The scope granted by the user.
                "scope": scope,
                # Codes expire after AUTH_CODE_TTL_SECONDS (10 minutes).
                "exp": time.time() + AUTH_CODE_TTL_SECONDS,
                # Single-use flag: redemption marks it used.
                "used": False,
            }  # End the stored authorization code record.
        # Build the success redirect: redirect_uri?code=...&state=...
        ok = urllib.parse.urlencode({"code": code, "state": state})
        # Return the full redirect Location for the HTTP layer to send.
        return f"{redirect_uri}?{ok}" if "?" not in redirect_uri else \
            f"{redirect_uri}&{ok}"  # The URI already had a query string; append.

    # --- token endpoint ---------------------------------------------------
    # Handle a token request body (form or JSON dict); returns token response.
    def token(self, body):
        # grant_type selects the token flow being requested.
        grant_type = body.get("grant_type", "")
        # Authorization-code exchange: code -> access + refresh tokens.
        if grant_type == "authorization_code":
            # Delegate to the code-redemption helper.
            return self._token_from_code(body)
        # Refresh-token grant: refresh token -> rotated token pair.
        if grant_type == "refresh_token":
            # Delegate to the refresh helper.
            return self._token_from_refresh(body)
        # Anything else is unsupported by this server.
        raise ValueError("unsupported grant_type")

    # Redeem an authorization code (with PKCE) for a token pair.
    def _token_from_code(self, body):
        # The code the client received on the redirect.
        code = body.get("code", "")
        # The redirect_uri must match the one used at authorize time.
        redirect_uri = body.get("redirect_uri", "")
        # The PKCE verifier the client kept secret until now.
        verifier = body.get("code_verifier", "")
        # The client authenticates with its id (secret optional for MCP).
        client_id = body.get("client_id", "")
        # Look the code up under lock.
        with self._lock:
            # None when the code was never issued (or already pruned).
            record = self._codes.get(code)
        # Unknown codes are rejected.
        if record is None:
            # Do not reveal whether the code ever existed.
            raise ValueError("invalid code")
        # Codes are single-use: reject replays.
        if record["used"]:
            # A replayed code invalidates nothing else, but fails closed.
            raise ValueError("code already used")
        # Expired codes are rejected.
        if time.time() > record["exp"]:
            # Prune the expired code under lock to bound memory.
            with self._lock:
                # Remove it so the store does not grow forever.
                self._codes.pop(code, None)
            # Fail closed on expiry.
            raise ValueError("code expired")
        # The redeeming client must be the one the code was issued to.
        if record["client_id"] != client_id:
            # Client mismatch: fail closed.
            raise ValueError("client mismatch")
        # The redirect_uri must match the authorize-time value exactly.
        if record["redirect_uri"] != redirect_uri:
            # Redirect mismatch: fail closed.
            raise ValueError("redirect_uri mismatch")
        # Verify PKCE: SHA256(verifier) base64url must equal the challenge.
        expected = record["challenge"]
        # Compute the challenge from the presented verifier.
        actual = pkce_challenge(verifier)
        # Constant-time compare so timing leaks nothing about the challenge.
        if not secrets.compare_digest(actual, expected):
            # Wrong verifier: fail closed.
            raise ValueError("PKCE verification failed")
        # All checks passed: mark the code used (single-use enforced).
        with self._lock:
            # Flag it so any replay attempt fails.
            record["used"] = True
        # Issue the token pair for the granted scope.
        return self._issue_tokens(client_id, record["scope"])

    # Rotate a refresh token into a fresh access + refresh token pair.
    def _token_from_refresh(self, body):
        # The refresh token the client is rotating.
        refresh_token = body.get("refresh_token", "")
        # Look it up under lock.
        with self._lock:
            # None when the refresh token was never issued (or was rotated).
            record = self._refresh.get(refresh_token)
        # Unknown refresh tokens are rejected.
        if record is None:
            # Do not reveal whether the token ever existed.
            raise ValueError("invalid refresh_token")
        # Expired refresh tokens are rejected (30-day lifetime).
        if time.time() > record["exp"]:
            # Prune the expired token under lock.
            with self._lock:
                # Remove it so the store does not grow forever.
                self._refresh.pop(refresh_token, None)
            # Fail closed on expiry.
            raise ValueError("refresh_token expired")
        # Rotation: invalidate the presented refresh token first...
        with self._lock:
            # Delete before issuing so a concurrent replay cannot double-spend.
            self._refresh.pop(refresh_token, None)
        # ...then issue a brand-new pair for the same client and scope.
        return self._issue_tokens(record["client_id"], record["scope"])

    # Mint and store an access + refresh token pair for client/scope.
    def _issue_tokens(self, client_id, scope):
        # Mint the opaque access token.
        access = new_token()
        # Mint the opaque refresh token.
        refresh = new_token()
        # Record both under lock with their expiries.
        with self._lock:
            # Access tokens live ACCESS_TOKEN_TTL_SECONDS (1 hour).
            self._tokens[access] = {
                # The client this token was issued to.
                "client_id": client_id,
                # The scope granted with this token.
                "scope": scope,
                # Absolute expiry timestamp.
                "exp": time.time() + ACCESS_TOKEN_TTL_SECONDS,
            }  # End the stored access-token record.
            # Refresh tokens live 30 days.
            self._refresh[refresh] = {
                # The client this token was issued to.
                "client_id": client_id,
                # The scope granted with this token.
                "scope": scope,
                # Absolute expiry timestamp (30 days).
                "exp": time.time() + 30 * 24 * 3600,
            }  # End the stored refresh-token record.
        # Return the RFC 6749 token response to the client.
        return {
            # The token the client puts in the Authorization header.
            "access_token": access,
            # The token type is always "Bearer".
            "token_type": "Bearer",
            # Seconds until the access token expires.
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            # The refresh token for rotation when the access token lapses.
            "refresh_token": refresh,
            # Echo the granted scope back to the client.
            "scope": scope,
        }  # End the RFC 6749 token response.

    # True when the token exists and has not expired (prunes the dead).
    def validate_token(self, token):
        # Look the token up under lock.
        with self._lock:
            # None when the token was never issued.
            record = self._tokens.get(token)
        # Unknown tokens are invalid.
        if record is None:
            # Fail closed.
            return False
        # Expired tokens are invalid (and pruned to bound memory).
        if time.time() > record["exp"]:
            # Prune under lock.
            with self._lock:
                # Remove the dead token.
                self._tokens.pop(token, None)
            # Fail closed on expiry.
            return False
        # The token is live.
        return True


# Wire the OAuth endpoints into a handler made by make_handler().
def oauth_routes(oauth):
    # Build the (method, path) -> bound-handler map for the five endpoints.
    def _metadata(handler):
        # GET /.well-known/oauth-protected-resource -> the RFC 9728 document.
        handler._send_json(200, oauth.protected_resource_metadata())

    # POST /oauth/register -> RFC 7591 dynamic client registration.
    def _register(handler):
        # Parse the JSON registration body.
        try:
            # May raise JSONRPCError for empty/malformed bodies.
            body = handler._read_json_body()
        # Convert body errors into a 400 for the registration endpoint.
        except JSONRPCError as exc:
            # Registration failures are plain 400s with an error message.
            handler._send_json(400, {"error": str(exc)})
            # Stop here.
            return
        # Attempt the registration (validates redirect_uris).
        try:
            # Returns the RFC 7591 client information response.
            resp = oauth.register_client(body)
        # ValueError carries the human-readable validation failure.
        except ValueError as exc:
            # 400 with the validation message.
            handler._send_json(400, {"error": str(exc)})
            # Stop here.
            return
        # 201 Created with the new client credentials.
        handler._send_json(201, resp)

    # GET /oauth/authorize?... -> validate params, render the consent page.
    def _authorize(handler):
        # Parse the query string into a flat dict of parameters.
        query = urllib.parse.urlparse(handler.path).query
        # parse_qs gives lists; take the first value of each key.
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        # Validate the authorize request (client, redirect, PKCE, ...).
        try:
            # Returns (client, redirect_uri, scope, state, challenge).
            _, redirect_uri, scope, state, challenge = \
                oauth._validate_authorize(params)  # Validate client, redirect URI, and PKCE challenge.
        # ValueError carries the human-readable validation failure.
        except ValueError as exc:
            # 400: the request itself was invalid (no safe redirect target).
            handler._send_json(400, {"error": str(exc)})
            # Stop here.
            return
        # Render the consent HTML with the validated values embedded.
        page = oauth.consent_page(params.get("client_id", ""), redirect_uri,
                                  scope, state, challenge)  # Render consent with the validated values.
        # Encode the page to bytes for the response body.
        body = page.encode("utf-8")
        # 200 with the HTML consent page.
        handler.send_response(200)
        # Declare the HTML content type and exact body length.
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        # Exact length so the client knows when the page ends.
        handler.send_header("Content-Length", str(len(body)))
        # Blank line ends the header section.
        handler.end_headers()
        # Write the consent page bytes to the socket.
        handler.wfile.write(body)

    # POST /oauth/approve -> process the consent form, 302 to redirect_uri.
    def _approve(handler):
        # Read the form-encoded consent decision body.
        length = int(handler.headers.get("Content-Length", "0") or 0)
        # An empty approval POST cannot carry a decision.
        if length <= 0:
            # 400: nothing to decide on.
            handler._send_json(400, {"error": "empty form body"})
            # Stop here.
            return
        # Read exactly length bytes from the request stream.
        raw = handler.rfile.read(length)
        # Decode the form body as UTF-8 text.
        text = raw.decode("utf-8", errors="replace")
        # Parse application/x-www-form-urlencoded into a flat dict.
        form = {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}
        # Process the decision; returns the 302 Location URL.
        try:
            # May raise ValueError for unknown client/redirect.
            location = oauth.approve(form)
        # ValueError means there is no safe redirect target.
        except ValueError as exc:
            # 400 with the failure reason.
            handler._send_json(400, {"error": str(exc)})
            # Stop here.
            return
        # 302 Found with the Location header pointing at the redirect_uri.
        handler.send_response(302)
        # The Location carries ?code=... (or ?error=access_denied).
        handler.send_header("Location", location)
        # Blank line ends the header section (no body needed).
        handler.end_headers()

    # POST /oauth/token -> grant handling for code exchange and refresh.
    def _token(handler):
        # Read the Content-Length to know how many bytes to read.
        length = int(handler.headers.get("Content-Length", "0") or 0)
        # An empty token POST cannot carry a grant.
        if length <= 0:
            # 400: nothing to process.
            handler._send_json(400, {"error": "empty body"})
            # Stop here.
            return
        # Read exactly length bytes from the request stream.
        raw = handler.rfile.read(length)
        # Check whether the client sent JSON or a form-encoded body.
        ctype = handler.headers.get("Content-Type", "")
        # Parse the body according to its declared content type.
        try:
            # JSON bodies parse from the bytes we already read.
            if "application/json" in ctype:
                # Decode the raw bytes as UTF-8, then parse the JSON text.
                body = json.loads(raw.decode("utf-8"))
            # Otherwise treat it as application/x-www-form-urlencoded.
            else:
                # Decode and parse the form into a flat dict.
                text = raw.decode("utf-8", errors="replace")
                # parse_qs gives lists; take the first value of each key.
                body = {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}
        # ValueError covers malformed JSON or undecodable bytes.
        except ValueError:
            # 400: the token request body was unusable.
            handler._send_json(400, {"error": "invalid request body"})
            # Stop here.
            return
        # Attempt the grant (code exchange or refresh rotation).
        try:
            # Returns the RFC 6749 token response dict.
            resp = oauth.token(body)
        # ValueError carries the human-readable grant failure.
        except ValueError as exc:
            # 400 with error=invalid_grant per RFC 6749 section 5.2.
            handler._send_json(400, {"error": "invalid_grant",
                                     "error_description": str(exc)})  # Human-readable reason the grant failed.
            # Stop here.
            return
        # 200 with the access/refresh token pair.
        handler._send_json(200, resp)

    # Map each (method, path) pair to its handler function.
    return {
        # RFC 9728 protected-resource metadata document.
        ("GET", "/.well-known/oauth-protected-resource"): _metadata,
        # RFC 7591 dynamic client registration.
        ("POST", "/oauth/register"): _register,
        # OAuth 2.1 authorization endpoint (renders consent).
        ("GET", "/oauth/authorize"): _authorize,
        # Consent decision endpoint (issues the code via 302).
        ("POST", "/oauth/approve"): _approve,
        # Token endpoint (code exchange + refresh rotation).
        ("POST", "/oauth/token"): _token,
    }  # End the OAuth route table.
