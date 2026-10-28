# ARC Bridge MCP — Nebius Port

A self-hosted **MCP server** (spec **2025-11-25**, Streamable HTTP) that
exposes David's **ARC free-tier agent mesh** as MCP tools — a **Personal AI**:
an always-on assistant with **persistent memory**, **reusable skills/tool
access**, and **data that stays under the user's control** (self-hosted,
file-or-mesh backed, no third-party data custody). Built for the
**Nebius × NVIDIA Global AI Hackathon**, **Personal AI track**.

**Zero dependencies.** Pure Python 3.9+ standard library. No `pip install`.

## What it does

Five tools plus one resource, over `POST /mcp`:

| Tool | What it really does |
|---|---|
| `arc_ask` | Ask the mesh a question. When Token Factory is configured, an **NVIDIA Nemotron** model writes the answer, grounded in retrieved mesh memory. When it is not, memory retrieval answers and the result states plainly that no model call was made. Live mode queues the question for the real mesh worker instead. |
| `arc_queue_status` | Read back a queued question's record (status, answer/digest). |
| `arc_memory_search` | Search the assistant's persistent long-term memory (`arc_memory`). |
| `arc_health` | Worker liveness (heartbeat age, queue depth) **plus the Token Factory/Nemotron configuration status**. |
| `arc_notify` | Fan out a notification: `log` (always), `echo` (Voice Monkey), `telegram` (Bot API), `ntfy` (ntfy.sh), `all`. Unconfigured channels fail closed with a reason. |
| `arc://mesh/health` | The health snapshot as a read-only resource. |

## ⭐ How Nebius Token Factory + NVIDIA Nemotron are used

> **This is the core of the Nebius port — read this section first.**

- **Every model call in this project runs on Nebius Token Factory** and
  requests an **NVIDIA Nemotron** model. There is no other model provider
  in the codebase.
- Token Factory is **OpenAI-compatible**, so the client
  (`arc_bridge/nebius.py`, stdlib-only) is a thin wrapper:
  `POST {TOKEN_FACTORY_BASE_URL}/chat/completions` with a
  `Bearer TOKEN_FACTORY_API_KEY` header and an OpenAI-shaped body.
- **Configuration is entirely env-driven** (see `.env.example`):
  - `TOKEN_FACTORY_API_KEY` — the only required secret.
  - `TOKEN_FACTORY_BASE_URL` — default `https://api.tokenfactory.nebius.com/v1`.
  - `NEBIUS_MODEL` — default `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`.
- **Where the model sits in the loop:** `arc_ask` first retrieves the
  top memory records for the question, then Nemotron writes the answer
  **grounded in those records only** (system prompt: answer from memory,
  name gaps instead of inventing facts, keep it speech-safe). This is the
  Personal AI pattern: the model is the voice, the user's own memory is
  the source of truth.
- **Attribution is never hidden:** every `arc_ask` result carries a
  `model` block (`provider`, `model`, `used`, and `error` when a call
  failed or was not configured), and `arc_health` reports the same status.
- **Fail-closed, no fakes:** with no key set, the server still runs —
  answers fall back to honest memory retrieval and every result says
  `Nebius Token Factory is not configured: set TOKEN_FACTORY_API_KEY;
  no model call was made`. A configured-but-failing API degrades the
  same way, with the HTTP failure attached.

## Run it (60 seconds)

```bash
python3 server.py
# backend: LocalBackend
# model: nebius-token-factory nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B configured=False (missing: TOKEN_FACTORY_API_KEY)
# auth: none (local mode -- bind to 127.0.0.1 ...)
# ARC Bridge MCP (Nebius port) on http://127.0.0.1:8765/mcp
```

Local mode needs **no configuration**: it seeds clearly-labeled demo
memory and runs the full MCP lifecycle. Add the Token Factory key to turn
on Nemotron answers:

```bash
TOKEN_FACTORY_API_KEY=<your key> python3 server.py
# model: ... configured=True
```

Try it with curl (replace `SID` with the `Mcp-Session-Id` from step 1):

```bash
# 1. initialize
curl -s -D - -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-11-25","capabilities":{},
                 "clientInfo":{"name":"curl","version":"0"}}}'

# 2. list tools
curl -s -X POST http://127.0.0.1:8765/mcp -H 'Content-Type: application/json' \
  -H "Mcp-Session-Id: SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

# 3. ask the mesh (Nemotron answers when the key is set)
curl -s -X POST http://127.0.0.1:8765/mcp -H 'Content-Type: application/json' \
  -H "Mcp-Session-Id: SID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"arc_ask","arguments":{"question":"How do voice replies work?"}}}'
```

## Live mode (the real mesh)

```bash
cp .env.example .env   # fill in values, never commit .env
set -a && source .env && set +a
ARC_BRIDGE_BACKEND=live python3 server.py
```

Live mode wires the same contracts the production mesh uses
(`arc_alexa_queue`, `arc_memory`, `arc_heartbeats` over Supabase REST;
Voice Monkey / Telegram / ntfy notifications). The Token Factory model
status is reported in `arc_health` in both modes.

## OAuth 2.1 account linking

```bash
ARC_BRIDGE_AUTH=oauth ARC_BRIDGE_PUBLIC_URL=https://mcp.example.com \
  python3 server.py
```

Fronts `/mcp` with a local OAuth 2.1 authorization server (RFC 9728
metadata, RFC 7591 dynamic registration, PKCE S256 consent flow, refresh
rotation) — carried over unchanged from the original build.

## What existed before vs. what was built for this hackathon

**Honest disclosure, per the hackathon rules.**

- **Existed before the submission window:** ARC Bridge MCP itself — the
  MCP transport harness, OAuth 2.1 front door, the five tools, the local
  file-backed harness, and the live mesh backends — was built for the
  Amazon Build, Ship, Shape hackathon and submitted there on
  **Oct 19, 2026**. The underlying ARC mesh (worker, memory, voice path)
  predates both hackathons.
- **Built during the Nebius × NVIDIA window (the new work):**
  1. `arc_bridge/nebius.py` — the Nebius Token Factory client
     (OpenAI-compatible, env-driven, fail-closed with exact errors).
  2. The **Nemotron answer path** in `arc_ask`: memory retrieval →
     Nemotron synthesis grounded in that memory, with per-result model
     attribution (`provider`/`model`/`used`/`error`).
  3. **Model status in `arc_health`** and the server startup banner, so
     the Token Factory wiring is observable, not just claimed.
  4. `arc_bridge/tests/test_nebius.py` — configuration-contract tests,
     an offline fake-Token-Factory round-trip test (request shape, auth
     header, response parsing), and backend integration tests.
  5. This README's Token Factory/Nemotron documentation and the setup
     flow around the Builder Program credit.

## Tests

```bash
python3 -m unittest discover -s arc_bridge/tests -v   # 21 tests
```

Covers: backend units, full-stack HTTP (initialize → 5 tools → resource
→ session lifecycle), OAuth challenge shape, and the Nebius layer
(unconfigured errors, env overrides, fake-endpoint round trip, honest
fallback). All tests run **offline** — no real key, no real network.

## Layout

```
server.py                 entry point (env config -> build_app -> serve)
arc_bridge/
  harness.py              vendored mini-mcp-harness (Streamable HTTP + OAuth)
  backends.py             LocalBackend (file harness) / LiveBackend (mesh)
  nebius.py               Nebius Token Factory client (Nemotron answers)
  tools.py                5 tools + 1 resource registration
  tests/                  unit + full-stack HTTP + Nebius tests
data/                     local-mode JSONL files (gitignored, auto-created)
.env.example              every supported variable, documented
```

## Design notes

- **Stdlib only** is deliberate: runs anywhere Python exists, $0 forever,
  immune to SDK version drift.
- **Fail-closed everywhere**: unknown sessions → 404, bad auth → 401 with
  the RFC 9728 challenge, unconfigured model/channels → explicit reasons,
  never a faked success.
- **Every line of code carries a plain-English `#` comment** — the author
  can explain each line because each line says what it does.

## License

MIT — see `LICENSE`. The vendored `arc_bridge/harness.py` is
mini-mcp-harness v0.1.0 (same license, same author).
