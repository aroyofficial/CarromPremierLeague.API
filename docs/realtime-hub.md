# Realtime hub

Status: implemented for every polled public screen and for the admin session handoff, behind flags that default off. It runs in one process. It is not a multi-instance fan-out.

## Feasibility

The API is FastAPI on one uvicorn process (`Dockerfile` starts `uvicorn main:app` with no `--workers`). Render runs that as one service. There is no Redis, Service Bus, RabbitMQ, Kafka, NATS, or Postgres `LISTEN/NOTIFY`. The database is SQL Server. The web app is a static Vue build on GitHub Pages. Authentication is a bearer JWT (`Authorization`), not a cookie. Public match, schedule, table, and player reads are unauthenticated GETs.

A realtime hub is feasible for change notices at no extra cost, inside the API process. It is not feasible on the current host as a multi-instance collaborative editor.

## Decision

Use Server-Sent Events. The server sends a notice naming what changed; the browser refetches the existing API. Notices carry no scores, names, or tokens.

| Option | Why it was not selected |
| --- | --- |
| WebSockets | Bidirectional protocol and a library. Every consumer only needs server-to-client notices. |
| ASP.NET SignalR / Azure SignalR | The API is Python. Azure SignalR would be a new paid vendor. |
| Socket.IO | Extra protocol and a Node-shaped client. The API is not Node. |
| GraphQL subscriptions | GraphQL is not in the stack. |
| Redis or another broker | No broker is deployed. Adding one is a paid or new service. |
| Long polling | Already what the timers do. It stays as the fallback. |

## Streams

| Route | Who | Events |
| --- | --- | --- |
| `GET /realtime/public` | anyone allowed to call the public GETs | `league.invalidated` |
| `POST /realtime/ticket` | a signed-in admin (bearer token) | returns a one-time ticket, valid 60 seconds |
| `GET /realtime/admin?ticket=...` | the admin the ticket was issued to | `admin.login_request.changed`, `realtime.revoked` |
| `GET /realtime/health` | anyone | enabled, backplane, subscribers, published, dropped, rejected |

Native `EventSource` cannot send `Authorization`. The JWT never goes in a URL. The web app posts for a ticket with its bearer token. The ticket is random, single use, held in memory for 60 seconds, and bound to the admin id, username, token version, and token expiry.

## Producers

`PublicChangeMiddleware` (in `core/realtime.py`) publishes `league.invalidated` after any 2xx `POST`, `PUT`, `PATCH`, or `DELETE` under `/matches`, `/seasons`, `/teams`, `/players`, `/countries`, `/rosters`, or `/settings`. Those repositories call `commit()` inside the handler, so the notice goes out only after the change is stored. A failed write publishes nothing. `/auth`, `/admins`, `/assistant`, `/gallery`, and `/media` never publish on the public stream.

`AuthService` publishes `admin.login_request.changed` to `admin:{id}` when another device asks to sign in, and when that request is approved or declined.

## Consumers

| Screen | Before | Now |
| --- | --- | --- |
| Schedule, Points table, Stats, Player stats, Players | 5s poll, plus when the tab becomes visible | refetch when a notice names an area that screen shows, and again when the stream opens or drops. 60s poll while the socket is healthy, 5s when it is down or off. Silence for 35s, or no open within 20s, counts as down. Visible-tab refetch unchanged |
| Session handoff (admin) | 4s poll | same rules, with a 4s fallback. Each attempt asks for a new ticket, so a restart does not leave a dead one |
| Login wait (device asking to sign in) | 3s poll | unchanged, see below |
| Match clock, countdown, password cooldown | local 1s timers | unchanged; no API |

All public screens in a tab share one `EventSource` (`watchPublic` in `src/utils/realtime.js`). A screen listens only for the areas in `src/utils/realtimeRules.js`: a country edit refreshes the players page and leaves the points table alone. Reconnect uses exponential backoff with jitter, capped at 30 seconds, and reopens when the browser comes back online. The server sends `realtime.heartbeat` every 15 seconds. The browser ignores SSE comments, so the heartbeat is a real event and the client refetches nothing for it. If 35 seconds pass with no heartbeat and no notice, the client closes the socket, refetches, and polls every 5 seconds. A socket that never opens within 20 seconds is abandoned the same way. Events are applied once per event id. There is no stored replay: opening the stream refetches, which covers anything missed while it was down.

The login wait stays on polling. The waiting device is not signed in, and its only credential is the request id. Putting the request id in a stream URL would leak it into access logs, and the wait lasts at most 10 minutes.

## Event contracts (version 1)

Common envelope: `eventId` (UUID, new per publish), `eventType`, `eventVersion` (1), `occurredAtUtc` (UTC ISO-8601), `entityType`, `entityId`, `correlationId` (null), `payload`.

| eventType | Channel | entityType / entityId | Payload | Client action |
| --- | --- | --- | --- | --- |
| `league.invalidated` | `public` | `area` / the route group, for example `matches` | `{ "area": "matches" }` | refetch if this screen displays that area |
| `admin.login_request.changed` | `admin:{id}` | `admin` / the admin id | `{}` | call `GET /auth/login-request` |
| `realtime.revoked` | `admin:{id}` | none | `{}` | stop the stream, refetch, keep polling |
| `realtime.heartbeat` | both | none | `{}` | proof the socket is alive. The browser cannot see SSE comments, so this is a real event. It does not refetch |

Delivery is at least once inside one process. There is no replay after a disconnect. The fallback poll and the reconnect refetch cover missed notices. A client ignores an unknown `eventVersion`.

## Security and authorization

- Both flags default off. Stream routes answer 404 when `CPL_REALTIME_ENABLED` is off.
- `Origin`, when present, must be in `CPL_APP_ALLOWED_HOSTS`; otherwise 403.
- Public notices name only a route group, which is no more than any public GET reveals.
- The admin channel comes from the ticket on the server. The client cannot name a channel or another admin.
- A missing, forged, reused, or expired ticket gets 401.
- The admin stream re-reads the admin row on its first heartbeat (about 15 seconds) and then once a minute. If the account is inactive or the token version changed (logout elsewhere, approval of another device, password change), the server sends `realtime.revoked` and closes. API calls keep checking the token on every request, so this delay does not extend access to data. The stream also closes when the bearer token it was issued from expires.
- At most 200 subscribers and 1000 outstanding tickets. Each queue holds 32 events; a slow consumer drops the oldest.
- No client-to-server messages on any stream.
- Logs record the channel kind (`public` or `admin`) and subscriber counts, not ids, tickets, or bodies.
- A 401 from `realtime/` does not sign the user out of the web app.

## Free plan

Nothing in this design needs a paid add-on.

- Render's free web service is one instance with 512 MB. The Dockerfile starts uvicorn with no `--workers`, so there is one process. Subscribers live in that process. A second instance is a paid change, and it would not share these subscribers. Do not scale this service past one instance while realtime is on. `/realtime/health` reports `backplane: memory (single process)`.
- Redis, Azure SignalR, and other brokers are paid services, so they are not used. The 200 subscriber cap is there to protect the free instance's memory. Connection 201 gets 503 and that browser keeps polling.
- A free instance sleeps after about 15 minutes with no requests. Keeping it awake with a ping would spend the free monthly hours and is not done. While it sleeps there is nobody watching. The next visitor's poll (the web client allows 90 seconds for a cold start) wakes it, the stream connects, and the screen refetches. If the connect hangs, the client gives up after 20 seconds and retries.

To scale out later, replace `MemoryHub.publish` with a shared backplane and move tickets to that store. Producers call `publish_public_change` and `publish_login_request_changed` only, so they do not change. That backplane is a paid step and is not part of this deploy.

## Capacity

Measured locally with `python scripts/realtime_load.py --local` (Windows dev machine, one uvicorn process, real routes and middleware). Not measured on Render.

| Clients | Writes | Delivered | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| 200 | 5 | 1000 of 1000 | 20 ms | 34 ms | 43 ms |
| 210 | 2 | 400 of 400 to the 200 admitted; 10 refused with 503 | 23 ms | 35 ms | 38 ms |

Expected production load: one connection per open tab, well under 200 concurrent viewers, a few notices per live match. Each notice causes one refetch per open screen. That replaces a 5 second poll from every tab, so request volume goes down while the stream is open. To measure on Render, run the script with `--base` and `CPL_LOAD_TOKEN` (an admin token allowed to update settings; it re-saves the current settings).

## Tests

- `tests/test_realtime.py`: the event envelope, area mapping, flag behaviour, hub isolation, the subscriber cap, slow-consumer drop, cross-thread publish, ticket single use, expiry, and ceiling. Route tests cover 404 when off, foreign origin 403, missing or forged ticket 401, a ticket requiring a signed-in admin, and publishing only after a successful public write. Live tests run uvicorn and read the stream over HTTP: a write reaches an open public stream, a private notice reaches only its admin, a used ticket cannot open a second stream, and a revoked admin stream is closed.
- Web `src/utils/realtime.test.mjs` and `src/utils/realtimeStream.test.mjs`: dedupe, the bounded seen list, backoff, reconnect with a fresh URL, retry when there is no ticket, revoke stops for good, stop cancels the retry, and the poll slowing and speeding up with the stream state.
- Not covered: a real browser against a deployed API.

## Rollout

1. Deploy with both flags unset. Behaviour is the same as before: screens poll, and the stream routes return 404.
2. On Render, set `CPL_REALTIME_ENABLED=true` on the API service (one instance). Check `GET /realtime/health` shows `enabled: true`.
3. In the web repository, set the Actions variable `VITE_REALTIME_ENABLED` to `true` and redeploy Pages.
4. Watch `/realtime/health`: `rejected` and `dropped` should stay at 0, and `subscribers` should track open tabs.
5. Rollback: set `CPL_REALTIME_ENABLED` to `false` (or remove it) and restart the API. Browsers get 404s, report the stream as down, and poll every 5 seconds. Optionally clear the web variable and redeploy. No schema or data change.

## Not done

- A shared backplane. Realtime stays on the one free API process.
- A load test on Render. The numbers above are from a dev machine.
- A browser end-to-end run against a deployed build.
- The login wait still polls (deliberately, see Consumers).
