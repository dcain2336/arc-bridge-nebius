# DEVPOST submission draft — Nebius × NVIDIA Global AI Hackathon
# (DRAFT — not submitted. Deadline Fri Oct 30, 2026, 1:00 PM EDT.)
# Items marked [LIVE-CHECK] need a fact only a live run can provide.

## Project name
ARC Bridge MCP — Nebius Port (Personal AI edition)

## Track
**Personal AI**

## Tagline
Your own always-on assistant, your own data: an MCP server that gives any
MCP client a persistent memory, a tool surface, and answers written by
NVIDIA Nemotron on Nebius Token Factory — grounded in *your* memory, not
the open internet.

## Description
ARC Bridge MCP exposes a real, running personal-assistant mesh (ARC) as a
Model Context Protocol server that any MCP client can connect to. The
assistant has the three things the Personal AI track asks for:

- **Persistent memory** — a long-term memory store the assistant reads on
  every question (`arc_memory_search`, and retrieval inside `arc_ask`).
  Local mode keeps it in user-owned JSONL files; live mode uses the mesh's
  real memory table. Either way the data lives with the user.
- **Reusable skills / tool access** — five MCP tools (ask, queue status,
  memory search, health, notify) plus a health resource, over standard
  MCP Streamable HTTP (spec 2025-11-25), with an optional OAuth 2.1
  account-linking front door.
- **Data under user control** — self-hosted, zero-dependency Python
  stdlib, no third-party data custody, fail-closed everywhere: an
  unconfigured channel or model reports exactly why instead of faking a
  result.

**The Nebius port (new work for this hackathon):** every model call now
runs on **Nebius Token Factory** against an **NVIDIA Nemotron** model.
`arc_ask` retrieves the relevant memory records, then Nemotron writes the
answer grounded in those records alone — the model is the voice, the
user's memory is the source of truth. Each result carries a `model`
attribution block (provider, model slug, whether the model actually ran,
and the error when it didn't), and `arc_health` exposes the Token Factory
configuration status, so the integration is verifiable from the outside.

## Features
- MCP Streamable HTTP server (sessions, SSE notifications, resumability),
  hand-rolled in pure Python stdlib — no SDK, no dependencies.
- Nemotron-on-Token-Factory answer path with memory grounding and
  per-result attribution; honest retrieval fallback when unconfigured.
- Persistent memory with keyword retrieval (local) or the live mesh
  memory table (live mode).
- Async question queue contract: fast questions answer inline; hard ones
  queue for the mesh worker and announce back (voice-shaped design).
- Notification fan-out (log / Echo via Voice Monkey / Telegram / ntfy),
  each channel fail-closed with an explicit reason.
- OAuth 2.1 account linking (RFC 9728 metadata, RFC 7591 registration,
  PKCE S256, refresh rotation) for client integrations that require it.
- 21 automated tests, all runnable offline, including an offline
  fake-Token-Factory round-trip test of the client.

## How Token Factory + Nemotron are used
- Client: `arc_bridge/nebius.py` — `POST {TOKEN_FACTORY_BASE_URL}/chat/completions`,
  `Authorization: Bearer TOKEN_FACTORY_API_KEY`, OpenAI-shaped body.
- Model: NVIDIA Nemotron — default slug
  `nvidia/llama-3.1-nemotron-70b-instruct`, overridable via `NEBIUS_MODEL`.
  [LIVE-CHECK: confirm the exact Nemotron slug available in the Token
  Factory console once the Builder Program credit lands, and update the
  default if the console's slug differs.]
- Call pattern: one chat completion per `arc_ask`, system prompt enforcing
  memory-only grounding, temperature 0.2, max 400 tokens, answer capped
  speech-safe.
- [LIVE-CHECK: paste one real arc_ask request/response pair from a live
  Token Factory run (question, model block showing "used": true, answer
  text) once the $25 Builder Program Token Factory credit is activated.]
- [LIVE-CHECK: note observed Token Factory latency for the demo questions
  from the live run — do not estimate here.]

## Tech used
Python 3 (stdlib only) · MCP (spec 2025-11-25, Streamable HTTP) ·
Nebius Token Factory · NVIDIA Nemotron · OAuth 2.1 / PKCE · Supabase REST
(live mode) · Voice Monkey / Telegram Bot API / ntfy.sh (live notify)

## What existed before vs. built during the hackathon window
- **Before:** ARC Bridge MCP (transport harness, OAuth front door, five
  tools, local + live backends) — built for the Amazon Build, Ship, Shape
  hackathon, submitted Oct 19, 2026. The ARC mesh itself (worker, memory,
  voice path) is older still.
- **During this window:** the Token Factory client, the Nemotron grounded
  answer path in `arc_ask`, model status in `arc_health` and the startup
  banner, the Nebius test suite, and this port's documentation. Full
  detail in README.md ("What existed before vs. what was built for this
  hackathon").

## Demo
- Demo video (≤3 min, public YouTube): [LIVE-CHECK: record after the live
  Token Factory run — script beats: (1) start server, banner shows the
  Nemotron model configured; (2) arc_health shows model status; (3) an
  arc_ask answered by Nemotron with the attribution block visible; (4)
  the same question with the key removed, honestly labeled retrieval.]
- Demo URL: [LIVE-CHECK: URL of the hosted/test deployment used in the
  video, or state that the demo is the local run shown in the video.]
- Public repo: [LIVE-CHECK: public GitHub repo URL once created, with MIT
  license visible and this README as the repo README.]

## Feedback on Nebius Token Factory / NVIDIA tools (draft)
Truthful scope note: this draft is based on the porting experience —
reading the Token Factory API shape and building the client against it.
Anything about live behavior is marked and will be replaced with observed
facts after the credit-funded live run.

- The OpenAI-compatible surface is the single biggest accelerator:
  porting an existing OpenAI-shaped client was a base-URL + key + model
  slug change, and our stdlib client needed no SDK at all. Whatever
  Token Factory does next, keeping 100% OpenAI-shape parity (including
  error bodies) is worth more than extra proprietary endpoints.
- Model discoverability is the friction point we hit while porting: the
  exact Nemotron slugs available to a given account are only knowable
  inside the console, which forced a code default plus a "confirm at
  demo time" note. A public, unauthenticated `GET /v1/models`-style
  listing (or a docs page that pins canonical slugs per model family)
  would remove the guesswork for every porter.
  [LIVE-CHECK: after the live run, confirm whether the default slug
  worked as-is or had to be changed — that fact decides whether this
  paragraph stays as-is, softens, or gets replaced with praise.]
- [LIVE-CHECK: one paragraph of observed live behavior after the credit
  run — response quality of the Nemotron model on memory-grounded
  questions, any rate-limit or error-shape surprises, console UX for
  key creation and credit balance. No content here until observed.]
- NVIDIA side: Nemotron's instruction-following on the grounding system
  prompt ("answer only from the provided records, name gaps") is the
  behavior this design leans on hardest.
  [LIVE-CHECK: report from the live transcripts whether Nemotron held
  the grounding rule on the demo questions, with one example either way.]

## Submission checklist status
- [x] Working project + track (Personal AI) + description — this draft
- [ ] Working demo URL — [LIVE-CHECK above]
- [ ] ≤3-minute public YouTube demo video — record after live run
- [ ] Public repo with OSS license + README — repo creation is a human-gated step
- [x] Feedback draft — above, completed after the live run
- [x] Pre-existing project explanation — above and in README.md
