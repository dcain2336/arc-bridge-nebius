#!/usr/bin/env python3
# server.py: entry point for the ARC Bridge MCP server (Nebius port).
# Run:  python3 server.py            (local mode, zero configuration)
#       TOKEN_FACTORY_API_KEY=... python3 server.py (Nemotron answers on)
#       ARC_BRIDGE_BACKEND=live python3 server.py   (real mesh, needs env creds)
# The server speaks MCP Streamable HTTP (spec 2025-11-25) on /mcp and,
# when ARC_BRIDGE_AUTH=oauth, fronts it with a local OAuth 2.1 server.
"""ARC Bridge MCP (Nebius port) server entry point (stdlib only)."""
# Import os for environment variables and filesystem paths.
import os
# Import sys so the arc_bridge package imports from this directory.
import sys

# Ensure this file's directory is on sys.path for the arc_bridge import.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Import the vendored Streamable HTTP + OAuth harness (stdlib only).
from arc_bridge import harness  # noqa: E402 - path fix above must run first
# Import the backend factory (local file harness vs live mesh wiring).
from arc_bridge.backends import make_backend  # noqa: E402
# Import the tool/resource registration function.
from arc_bridge.tools import register_tools  # noqa: E402


# Read one environment variable, stripped; default when unset or blank.
def _env(name, default=""):
    # os.environ.get returns None when unset; fall back to the default.
    return (os.environ.get(name) or default).strip()


# Build the configured MCP application; returns (server, handler, sessions).
def build_app():
    # Resolve the data directory for the local backend (./data by default).
    here = os.path.dirname(os.path.abspath(__file__))
    # Allow ARC_BRIDGE_DATA_DIR to relocate the local data files.
    data_dir = _env("ARC_BRIDGE_DATA_DIR", os.path.join(here, "data"))
    # Choose the backend: "live" talks to the real mesh, else local harness.
    backend = make_backend(data_dir)
    # Report which backend was selected so the operator can verify it.
    print(f"backend: {type(backend).__name__}")
    # Report the Token Factory/Nemotron wiring state at startup.
    _model_status = backend.model.status()  # Configured?, model slug, missing pieces.
    # Print the model line with the missing-config reason when unconfigured.
    print(f"model: nebius-token-factory {_model_status['model']} "
          f"configured={_model_status['configured']} "
          f"via {_model_status['gateway']}"
          + ("" if _model_status["configured"]  # Configured: nothing more to explain.
             else f" (missing: {', '.join(_model_status['missing'] or [])})"))  # Unconfigured: name the gap in the banner.
    # Create the MCP application object with name and version.
    server = harness.MCPServer(
        # The server name reported in initialize responses.
        name="arc-bridge-nebius",
        # The server version reported in initialize responses.
        version="0.2.0",
        # Instructions help connected models use the tools correctly.
        instructions=("ARC Bridge exposes David's ARC agent mesh as MCP "
                      "tools: ask questions (arc_ask), check queued answers "  # Name the ask and queue-status tools.
                      "(arc_queue_status), search mesh memory "  # Name the memory search tool.
                      "(arc_memory_search), check mesh health (arc_health), "  # Name the health tool.
                      "and send notifications (arc_notify). When Nebius "  # Name the notify tool; explain the model layer.
                      "Token Factory is configured, arc_ask answers are "  # Token Factory configured means model answers.
                      "written by an NVIDIA Nemotron model grounded in "  # The model is Nemotron, grounded in memory.
                      "mesh memory; arc_health reports the model status."),  # Health exposes the model configuration; finish.
    )  # End the MCPServer constructor call.
    # Register the five tools and the health resource onto the server.
    register_tools(server, backend)
    # Fresh session manager for this process.
    sessions = harness.SessionManager()
    # Read the auth mode: "oauth" enables the OAuth 2.1 front door.
    auth_mode = (_env("ARC_BRIDGE_AUTH") or "none").lower()
    # The OAuth server instance, or None for the open local mode.
    auth = None
    # Extra HTTP routes start empty; OAuth adds its five endpoints.
    extra_routes = None
    # Enable the local OAuth 2.1 authorization server when requested.
    if auth_mode == "oauth":
        # The public issuer URL clients use for discovery and redirects.
        issuer = _env("ARC_BRIDGE_PUBLIC_URL",
                      f"http://127.0.0.1:{_env('PORT', '8765')}")  # Default issuer is this host and port.
        # Create the OAuth server bound to the public issuer URL.
        auth = harness.OAuthServer(issuer=issuer)
        # Wire the metadata/register/authorize/approve/token routes in.
        extra_routes = harness.oauth_routes(auth)
        # Report the OAuth issuer so the operator can verify discovery.
        print(f"auth: oauth (issuer {issuer})")
    # Report the open mode plainly so nobody mistakes it for secured.
    else:
        # Local demo mode: no bearer auth on /mcp.
        print("auth: none (local mode -- bind to 127.0.0.1 or put it "
              "behind a reverse proxy with auth)")  # Finish the open-mode warning.
    # Read the explicit Origin allowlist (comma-separated, optional).
    origins = [o.strip() for o in _env("ARC_BRIDGE_ALLOWED_ORIGINS").split(",")
               if o.strip()]  # Drop blank entries from the allowlist.
    # Build the Streamable HTTP handler bound to this app's pieces.
    handler = harness.make_handler(server, sessions, auth=auth,
                                   allowed_origins=origins,  # Pass the Origin allowlist to the handler.
                                   extra_routes=extra_routes)  # Pass extra routes (OAuth); close the call.
    # Hand the pieces back for serve() or for the test suite.
    return server, handler, sessions


# Entry point: build the app and serve it until interrupted.
def main():
    # Build the fully-configured application.
    _, handler, _ = build_app()
    # Bind host: 127.0.0.1 default keeps the open local mode loopback-only.
    host = _env("ARC_BRIDGE_HOST", "127.0.0.1")
    # Port: PORT env wins, else the harness default (8765).
    port = int(_env("PORT", str(harness.DEFAULT_PORT)))
    # Print the MCP endpoint URL for the operator.
    print(f"ARC Bridge MCP (Nebius port) on http://{host}:{port}/mcp")
    # When OAuth is on, also print the discovery URL for account linking.
    if (_env("ARC_BRIDGE_AUTH") or "none").lower() == "oauth":
        # Alexa+/client account-linking starts at this metadata document.
        print(f"OAuth metadata: http://{host}:{port}"
              f"/.well-known/oauth-protected-resource")  # Finish the metadata URL.
    # Block serving requests (one thread per connection) until Ctrl-C.
    harness.serve(handler, host=host, port=port)


# Run main() only when executed directly (not when imported by tests).
if __name__ == "__main__":
    # Start the server.
    main()
