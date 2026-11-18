# Striker — the CPL assistant

Striker is a chat assistant for Carrom Premier League. The public site uses it to answer questions about the league. The admin console uses it to look things up and to make changes by conversation, limited to what the signed-in admin is allowed to do.

## What it must do

1. Answer only from league data: seasons, teams, players, rosters, matches, schedules, standings, statistics, competition settings, countries and the public gallery. Anything else is politely refused.
2. On the public site, see only what the public site shows. Hidden seasons, draft gallery items and admin accounts are out of reach.
3. In the admin console, offer exactly the tools the signed-in admin's permissions allow. Every change is shown as a proposed action and runs only after the admin confirms, either by typing "yes" or by pressing Confirm.
4. Use Gemini first, then Groq, then OpenRouter, then the Cursor Cloud Agents API when that key is set.
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
services/assistant/engine.py ── providers.py ── Gemini → Groq → OpenRouter → Cursor
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
| 5 | Providers: Gemini, OpenAI-compatible Groq and OpenRouter, and Cursor Cloud Agents, with fallback | `services/assistant/providers.py` |
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
| `CPL_GROQ_MODEL` | No | `openai/gpt-oss-120b` |
| `CPL_OPENROUTER_API_KEY` | No | – |
| `CPL_OPENROUTER_MODEL` | No | `meta-llama/llama-3.3-70b-instruct:free` |
| `CPL_CURSOR_API_KEY` | No | – |
| `CPL_CURSOR_MODEL` | No | account default |

`CPL_CURSOR_API_KEY` is a Cursor user or service-account key. Striker calls the Cloud Agents API with no repository and does not open a pull request. Each model step is one short run, then the agent is archived. A status check only calls `GET /v1/me`, so it does not start an agent. Cursor is last: Gemini, Groq, and OpenRouter answer first when they can.

The Render deploy workflow copies the repository secrets `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` and `CURSOR_API_KEY` into those variables when they are set. `GET /assistant/status` reports the assistant as unavailable when no key is configured, a key is rejected, or every configured provider is out of credits. A short rate limit still counts as available. The public site and the admin console hide the widget in that case even when the settings switch is on.

## Who can see Striker

Striker has two switches in `tblSettings`, both on by default and both on `GET` and `PUT /settings`:

| Column | Field | Controls |
| --- | --- | --- |
| `AssistantPublicEnabled` | `assistant_public_enabled` | The widget on the public site and `POST /assistant/public/chat` |
| `AssistantAdminEnabled` | `assistant_admin_enabled` | The widget in the admin console, `POST /assistant/admin/chat` and `POST /assistant/admin/confirm` |

When a switch is off, that audience loses the widget and its endpoints answer that Striker is turned off without calling a model or applying a pending change. If settings cannot be read, the assistant stays off.

The old single `assistant_enabled` still works for older clients. A `PUT` that sends only `assistant_enabled` sets both switches, and a response reports it as on while either switch is on. `AssistantEnabled` is still written so an older API reading the same database keeps working. The migration copies the saved single value into both new columns once.

### Per-admin access

With the admin switch on, an admin also needs the `assistant.use` permission ("Use Striker", area "Striker assistant", role "Striker user"). A permission manager grants it from the Permissions screen like any other permission, and can only grant it while holding it. The migration gives `assistant.use` once to every admin who already holds `permissions.manage`; other admins get it when a manager grants it.

The server checks the permission from the database on every admin chat and confirm request. A revoked admin is refused at once, whatever their token says.

### The `ast` token claim

Tokens carry `ast`, true when the admin held `assistant.use` at issue time. It is set at login and when a sign-in from another device is approved. The console reads it to decide what to show before the profile loads, and the server never trusts it. Granting or revoking access does not change the token version, so a held token can lag behind. When the console loads the profile and the claim disagrees with the permissions the server returned, it calls `POST /auth/refresh` once. That reissues the token with current claims and keeps the original expiry, so it cannot extend a session. The admin launcher shows when the admin switch is on, a provider can answer, and the signed-in admin holds `assistant.use`.
