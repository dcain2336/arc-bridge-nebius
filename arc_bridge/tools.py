# tools.py: the ARC Bridge MCP tool and resource definitions.
# Five tools plus one read-only resource, registered onto the MCPServer.
# Every handler delegates to the configured backend (local or live), so the
# tools are real in both modes: they read and write genuine state.
"""MCP tool/resource registration for ARC Bridge."""
# Import json for serializing structured handler results when needed.
import json


# Register all ARC Bridge tools and resources on the given MCPServer.
def register_tools(mcp_server, backend):
    # --- arc_ask: ask the mesh a question -------------------------------
    # Define the handler before the decorator references it below.
    def _ask(args):
        # Pull the question; the backend validates non-emptiness.
        question = args.get("question", "")
        # Pull the optional extra context string.
        context = args.get("context", "")
        # Delegate to the backend (local retrieval or live queue insert).
        result = backend.ask(question, context)
        # Handlers may return dicts; the dispatcher serializes them.
        return result

    # Register arc_ask with its name, description, and JSON Schema.
    mcp_server.tool(
        # The tool name MCP clients call.
        name="arc_ask",
        # Description shown to models; explains both backend behaviors.
        description=("Ask David's ARC agent mesh a question. In local mode "
                     "the question is answered immediately: when Nebius "  # Local mode answers immediately.
                     "Token Factory is configured, an NVIDIA Nemotron model "  # Token Factory configured means Nemotron writes the answer.
                     "writes the answer grounded in retrieved memory; "  # The model answer is grounded in memory records.
                     "otherwise plain retrieval over the local memory corpus "  # Unconfigured means honest retrieval, never a fake model call.
                     "answers. In live mode it is queued for the mesh worker, "  # Live mode queues for the worker.
                     "which answers async and announces via the Echo."),  # The worker answers async and announces on the Echo.
        # JSON Schema for the tool arguments.
        input_schema={
            # Arguments are always a JSON object.
            "type": "object",
            # The question property is required.
            "required": ["question"],
            # Property declarations with descriptions for the model.
            "properties": {
                # The question to ask the mesh.
                "question": {"type": "string",
                             "description": "The question to ask."},  # Help text; close the property.
                # Optional extra context for the question.
                "context": {"type": "string",
                            "description": "Optional extra context."},  # Help text; close the property.
            },  # End the properties dict.
        },  # End the input schema.
    # Apply the decorator to the handler defined above.
    )(_ask)

    # --- arc_queue_status: check a queued question ----------------------
    # Define the handler before the decorator references it below.
    def _queue_status(args):
        # Pull the queue id; the backend validates it.
        queue_id = args.get("queue_id", "")
        # Refuse empty ids instead of scanning for nothing.
        if not str(queue_id).strip():
            # ValueError becomes an MCP isError result, not a crash.
            raise ValueError("queue_id must not be empty")
        # Look the record up in the backend.
        record = backend.queue_status(queue_id)
        # Unknown ids are reported plainly, not as crashes.
        if record is None:
            # Return a not-found payload the caller can act on.
            return {"found": False, "queue_id": queue_id}
        # Tag the record as found for the caller's convenience.
        record = dict(record)
        # Mark the lookup as successful.
        record["found"] = True
        # Return the stored queue record.
        return record

    # Register arc_queue_status with its name, description, and schema.
    mcp_server.tool(
        # The tool name MCP clients call.
        name="arc_queue_status",
        # Description shown to models.
        description=("Check the status of a question submitted with arc_ask. "
                     "Returns the stored record including the answer or "  # The record carries the answer...
                     "digest when available."),  # ...or digest when ready.
        # JSON Schema for the tool arguments.
        input_schema={
            # Arguments are always a JSON object.
            "type": "object",
            # The queue_id property is required.
            "required": ["queue_id"],
            # Property declarations.
            "properties": {
                # The queue id returned by arc_ask.
                "queue_id": {"type": "string",
                             "description": "The queue id from arc_ask."},  # Help text; close the property.
            },  # End the properties dict.
        },  # End the input schema.
    # Apply the decorator to the handler defined above.
    )(_queue_status)

    # --- arc_memory_search: search the mesh's shared memory --------------
    # Define the handler before the decorator references it below.
    def _memory_search(args):
        # Pull the query; empty queries are a caller error.
        query = (args.get("query") or "").strip()
        # Refuse empty queries instead of returning everything.
        if not query:
            # ValueError becomes an MCP isError result, not a crash.
            raise ValueError("query must not be empty")
        # Pull the limit with a default of 5.
        limit = args.get("limit", 5)
        # Delegate the search to the backend.
        return backend.memory_search(query, limit)

    # Register arc_memory_search with its name, description, and schema.
    mcp_server.tool(
        # The tool name MCP clients call.
        name="arc_memory_search",
        # Description shown to models.
        description=("Search the ARC mesh's shared long-term memory "
                     "(durable facts the mesh remembers). Local mode uses "  # Memory holds durable facts.
                     "keyword retrieval over the local corpus; live mode "  # Local mode uses keyword retrieval.
                     "queries the mesh's memory table."),  # Live mode queries the real table.
        # JSON Schema for the tool arguments.
        input_schema={
            # Arguments are always a JSON object.
            "type": "object",
            # The query property is required.
            "required": ["query"],
            # Property declarations.
            "properties": {
                # The search query text.
                "query": {"type": "string",
                          "description": "Keywords to search memory for."},  # Help text; close the property.
                # Optional result limit, default 5, max 20.
                "limit": {"type": "integer",
                          "description": "Max results (1-20, default 5).",  # Help text for the limit.
                          "default": 5, "minimum": 1, "maximum": 20},  # Default 5, clamped 1-20; close the property.
            },  # End the properties dict.
        },  # End the input schema.
    # Apply the decorator to the handler defined above.
    )(_memory_search)

    # --- arc_health: mesh/worker health snapshot -------------------------
    # Define the handler before the decorator references it below.
    def _health(args):
        # No arguments are used; the snapshot is unconditional.
        _ = args
        # Delegate to the backend's health snapshot.
        return backend.health()

    # Register arc_health with its name, description, and schema.
    mcp_server.tool(
        # The tool name MCP clients call.
        name="arc_health",
        # Description shown to models.
        description=("Get a health snapshot of the ARC mesh: worker "
                     "liveness, heartbeat age, question-queue depth, and "  # What the snapshot contains.
                     "the Nebius Token Factory / Nemotron configuration "  # The snapshot also reports the model layer's status.
                     "status. Use it before arc_ask to confirm the mesh "  # Advise checking health first.
                     "and the model are up."),  # ...and the model configuration too.
        # No arguments: an empty object schema.
        input_schema={"type": "object", "properties": {}},
    # Apply the decorator to the handler defined above.
    )(_health)

    # --- arc_notify: send a notification through mesh channels -----------
    # Define the handler before the decorator references it below.
    def _notify(args):
        # Pull the title; the backend validates non-emptiness.
        title = args.get("title", "")
        # Pull the message; the backend validates non-emptiness.
        message = args.get("message", "")
        # Pull the channel with a default of "log".
        channel = args.get("channel", "log")
        # Pull the priority with a default of 3.
        priority = args.get("priority", 3)
        # Delegate the fan-out to the backend (fail-closed per channel).
        return backend.notify(title, message, channel, priority)

    # Register arc_notify with its name, description, and schema.
    mcp_server.tool(
        # The tool name MCP clients call.
        name="arc_notify",
        # Description shown to models; documents the fail-closed behavior.
        description=("Send a notification through the mesh's channels. "
                     "Channels: log (always works, writes the alerts inbox), "  # The log channel always works.
                     "echo (Voice Monkey announcement on the Echo), telegram "  # Echo and Telegram channels.
                     "(Telegram bot message), ntfy (ntfy.sh push), all "  # Telegram detail, ntfy, and all-channels.
                     "(every configured channel). Unconfigured channels "  # "all" fans out; unconfigured fail closed.
                     "fail closed with a clear reason; nothing is faked."),  # Nothing is ever faked.
        # JSON Schema for the tool arguments.
        input_schema={
            # Arguments are always a JSON object.
            "type": "object",
            # Title and message are required.
            "required": ["title", "message"],
            # Property declarations.
            "properties": {
                # The notification title.
                "title": {"type": "string",
                          "description": "Short notification title."},  # Help text; close the property.
                # The notification body.
                "message": {"type": "string",
                            "description": "Notification message text."},  # Help text; close the property.
                # The delivery channel.
                "channel": {"type": "string",
                            "description": "log, echo, telegram, ntfy, all.",  # Help text for the channel.
                            "default": "log",  # Log is the default channel.
                            "enum": ["log", "echo", "telegram", "ntfy",  # The allowed channel values...
                                     "all"]},  # ...including "all"; close the property.
                # The priority, 1 (low) to 5 (urgent).
                "priority": {"type": "integer",
                             "description": "1 (low) to 5 (urgent).",  # Help text for the priority.
                             "default": 3, "minimum": 1, "maximum": 5},  # Default 3, clamped 1-5; close the property.
            },  # End the properties dict.
        },  # End the input schema.
    # Apply the decorator to the handler defined above.
    )(_notify)

    # --- arc://mesh/health: the health snapshot as a resource ------------
    # Define the reader before the decorator references it below.
    def _health_resource():
        # Reuse the backend's health snapshot for the resource content.
        snapshot = backend.health()
        # Readers return JSON-serializable values; dispatcher serializes.
        return snapshot

    # Register the read-only health resource.
    mcp_server.resource(
        # The URI clients pass to resources/read.
        uri="arc://mesh/health",
        # Human-readable resource name.
        name="Mesh health snapshot",
    # Apply the decorator to the reader defined above.
    )(_health_resource)
