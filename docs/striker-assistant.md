# Striker — the CPL assistant

Striker is a chat assistant for Carrom Premier League. The public site uses it to answer questions about the league. The admin console uses it to look things up and to make changes by conversation, limited to what the signed-in admin is allowed to do.

## What it must do

1. Answer only from league data: seasons, teams, players, rosters, matches, schedules, standings, statistics, competition settings, countries and the public gallery. Anything else is politely refused.
2. On the public site, see only what the public site shows. Hidden seasons, draft gallery items and admin accounts are out of reach.
3. In the admin console, offer exactly the tools the signed-in admin's permissions allow. Every change is shown as a proposed action and runs only after the admin confirms, either by typing "yes" or by pressing Confirm.
4. Use Gemini first and fall back to other free providers (Groq, OpenRouter) when Gemini is not configured, rate limited or failing.
5. Never send a provider key to the browser.

## Architecture

```
Browser (StrikerChat.vue)
  │  POST /assistant/public/chat   (anonymous, rate limited)
  │  POST /assistant/admin/chat    (bearer token)
  │  POST /assistant/admin/confirm (bearer token, signed action)
  ▼
routes/assistant_routes.py
  ▼
services/assistant/engine.py ── providers.py ── Gemini → Groq → OpenRouter
  │ tool calls
  ▼
services/assistant/tools.py      catalog: name, endpoint, permission, read or write
services/assistant/executor.py   calls the API's own endpoints in-process
  ▼
existing FastAPI routes (validation, business rules, require_permission)
```

The tools do not query the database directly. Each tool maps to an existing endpoint and is executed in-process through an ASGI loopback, forwarding the admin's bearer token. The same validation, business rules and permission checks that protect the admin console protect the assistant. Tool parameter schemas are generated from the endpoints' own Pydantic request models, so they cannot drift.

## Safety

- Tools are filtered by mode and by the admin's permissions before the model sees them. The endpoint's `require_permission` check runs again when the tool executes.
- Path parameters must be positive integers, so a model cannot steer a call to another path.
- Write tools never run inside the model loop. The engine returns a pending action signed with the API's JWT secret, bound to the admin id and valid for ten minutes. Only a confirm request with that signature runs it.
- Public mode is read-only. Season-scoped results are limited to seasons marked visible.
- Tool results are size-capped before they go back to the model. Messages and history are length-capped. The public endpoint is rate limited per client address.

## Subtasks

| # | Subtask | Where |
|---|---|---|
| 1 | Settings for provider keys and models; `httpx` dependency | `core/config.py`, `requirements.txt` |
| 2 | Pydantic → LLM function-schema converter | `services/assistant/schema.py` |
| 3 | Tool catalog: public reads, admin reads, admin writes, with permissions | `services/assistant/tools.py` |
| 4 | In-process executor with forwarded auth, visibility filter and caps | `services/assistant/executor.py` |
| 5 | Providers: Gemini (native function calling) and OpenAI-compatible (Groq, OpenRouter) with fallback | `services/assistant/providers.py` |
| 6 | Engine: data-only system prompts, tool loop, signed pending actions, confirm and cancel | `services/assistant/engine.py`, `actions.py` |
| 7 | Routes and per-address rate limit | `routes/assistant_routes.py` |
| 8 | Tests | `tests/test_assistant.py` |
| 9 | Chat widget: launcher, panel, messages, safe markdown, suggestions, confirmation cards, sources | Web `components/striker/` |
| 10 | Mount public mode on the public site and admin mode in the admin console; refresh the open admin page after a change | Web `App.vue`, `AdminLayout.vue` |
| 11 | Responsive layout (phone sheet, tablet and desktop panel), dynamic viewport and safe-area insets, reduced motion, cross-browser CSS | Web |
| 12 | Deploy: sync provider keys from repository secrets to Render, deploy API and web | `.github/workflows/deploy-render.yml` |

## Configuration

| Variable | Required | Default |
|---|---|---|
| `CPL_GEMINI_API_KEY` | One provider key is needed | – |
| `CPL_GEMINI_MODEL` | No | `gemini-2.5-flash` |
| `CPL_GROQ_API_KEY` | No | – |
| `CPL_GROQ_MODEL` | No | `llama-3.3-70b-versatile` |
| `CPL_OPENROUTER_API_KEY` | No | – |
| `CPL_OPENROUTER_MODEL` | No | `meta-llama/llama-3.3-70b-instruct:free` |

The Render deploy workflow copies the repository secrets `GEMINI_API_KEY`, `GROQ_API_KEY` and `OPENROUTER_API_KEY` into those variables when they are set. With no key configured, `GET /assistant/status` reports the assistant as unavailable and the widget says so instead of failing.
