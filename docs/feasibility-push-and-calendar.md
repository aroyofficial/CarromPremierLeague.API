# Feasibility: web push and calendar sync

Date of assessment: 2026-11-18. Scope: the three repositories `CarromPremierLeague.API`, `CarromPremierLeague.Web`, and `CarromPremierLeague.Database`. The decision record is [ADR 0001](adr/0001-web-push-and-calendar-feeds.md). Operating details are in [notifications-and-calendars.md](notifications-and-calendars.md).

## 1. Repository evidence

| Area | What the repository shows | Source |
| --- | --- | --- |
| Backend | Python FastAPI, one process, `pyodbc` to SQL Server | `CarromPremierLeague.API` |
| Hosting | Render, deployed from `deploy-render.yml`; a Dockerfile is present | `.github/workflows/deploy-render.yml`, `Dockerfile` |
| Frontend | Vue 3 with Vite, Pinia, Element Plus; static hosting on GitHub Pages | `CarromPremierLeague.Web` |
| Database | SQL Server, schema in `setup.sql` and `Setup-Database.ps1` | `CarromPremierLeague.Database` |
| Match model | Date, two teams, status, result, category. No start time, no venue, no postponed status | `tblMatches` |
| Authentication | Admin accounts only. Public visitors have no accounts | `routes/auth_routes.py` |
| Realtime | An in-process hub (`CPL_REALTIME_ENABLED`) updates open pages. It does not reach closed devices | `docs/realtime-hub.md` |
| Background work | No queue service. The API process can run timed work in-process | `services/notify/worker.py` |
| Existing notifications | None for members. The site has toast messages only | `utils/notify.js` |
| Service worker or PWA | None before this work. Added: `public/sw.js`, `manifest.webmanifest` | `CarromPremierLeague.Web/public` |
| Firebase, OneSignal, Azure Notification Hubs | Not present | Repository-wide search |
| Google or Microsoft OAuth | Not present before this work | Repository-wide search |
| Calendar export | None before this work | Repository-wide search |

These facts decide the architecture: there is no vendor push account, no queue, and no account-level identity for the public site.

## 2. Web Push

### Verdict

Feasible and chosen. Standards-based Web Push (RFC 8030, VAPID RFC 8292, message encryption RFC 8291) works with this stack. It needs one server key pair, a service worker in the existing Vue app, and a subscription table in SQL Server.

### Why not a vendor SDK

| Option | Assessment | Decision |
| --- | --- | --- |
| Web Push with VAPID, native | No vendor account. Each browser's own push service delivers the message. The server sends it with `pywebpush`. Works on HTTPS sites. | **Chosen** |
| Firebase Cloud Messaging (web) | Needs a Firebase project and its SDK in the bundle. Adds a Google dependency to the public site. Its web path still uses Web Push underneath. Nothing else in the stack needs Firebase. | Rejected: extra vendor, no added capability |
| OneSignal | Hosted audience model, SDK in the bundle, data processed by a third party. Price and terms change by plan. Privacy review needed for every device identifier. | Rejected: privacy and vendor cost not justified for one league |
| Azure Notification Hubs | The stack is not on Azure. Adds an Azure account and a second deployment target. | Rejected: not in use |

Cost note: the browser push services (Chrome and Android via FCM, Apple for Safari, Mozilla for Firefox) do not charge per message at the time of writing. Their terms can change, so the cost table in `notifications-and-calendars.md` says so. The server pays nothing per message. The only operator cost is the VAPID key pair.

### Platform and browser support

| Platform | Support | Requirement | Verified here |
| --- | --- | --- | --- |
| Chrome, Edge, Opera, Samsung Internet (desktop and Android) | Yes | HTTPS, user permission | Not tested live |
| Firefox (desktop and Android) | Yes | HTTPS, user permission | Not tested live |
| Safari 16.1+ on macOS 13+ | Yes | HTTPS, user permission | Not tested live |
| Safari and every iPhone or iPad browser | Only from the Home Screen, iOS 16.4+ | Installed web app, user gesture | Not tested on a device |
| Browsers without service workers or Push API, private windows that disable them | No | Fallback message | Covered by the browser check: push API removed, fallback message shown |

Permission is requested only from the explicit "Notify this device" button. The page never asks on load. A denied permission shows how to re-enable it in site settings and does not prompt again.

### Delivery semantics

- Push services accept a message. They do not confirm that the device displayed it. Telemetry counts "accepted by push service", never "shown".
- A 404 or 410 from the push service means the subscription is gone. The row is voided at once.
- A 429 or 5xx is retried with backoff. The subscription is kept.

## 3. Calendar capabilities

Two capabilities are kept apart, as the prompt requires.

### Capability A: one-time file and subscription, no account

| Method | Evaluated | Decision | Limit |
| --- | --- | --- | --- |
| `.ics` download | RFC 5545, one event per match | Implemented. Labelled "One-time file. It will not update when the match changes." | A saved file never updates |
| `https` subscription feed (season or team filter, token in the path) | Implemented behind `CPL_CALENDAR_FEED_ENABLED`. Stored as a SHA-256 hash, token is 256 bits | Implemented | The calendar app decides when to fetch. Google's add-by-URL often refreshes about once a day |
| `webcal:` form of the same link | Same feed; the scheme makes Apple Calendar and Outlook offer to subscribe | Implemented | Same refresh limit |
| Google Calendar add-event link (one-time) | Evaluated | Not implemented: it is another one-time path, and the one-time file already covers that need without a third-party link | One-time |
| Outlook add-event link (one-time) | Evaluated | Not implemented, for the same reason | One-time |

Apple Calendar: no direct write API exists for iCloud calendars that fits a public site. Apple Calendar is supported through the subscription feed and the one-time file. This is the standards path, not an invented API.

### Capability B: provider API with server-driven updates

| Provider | Feasibility | Scope | Decision |
| --- | --- | --- | --- |
| Google Calendar | Official API, OAuth 2.0, PKCE, refresh tokens | `calendar.events` | Implemented behind `CPL_GOOGLE_CALENDAR_ENABLED`. Live consent not run (needs a client) |
| Microsoft Graph | Official API, OAuth 2.0, PKCE, refresh tokens | `offline_access Calendars.ReadWrite` | Implemented behind `CPL_MICROSOFT_CALENDAR_ENABLED`. Live consent not run (needs a client) |
| Apple iCloud | No public write API suited to this site | None | Not implemented. Use the feed |

### What each method can and cannot do

| Event in the league | One-time file | Subscription feed | Google or Outlook API |
| --- | --- | --- | --- |
| Match scheduled | Yes | Yes, on the app's next fetch | Yes, on the next worker pass |
| Match moved (date) | No | Yes, on the next fetch | Yes, updates the same event |
| Match cancelled or voided | No | Yes, `STATUS:CANCELLED` | Google: `status: cancelled`. Outlook: event deleted |
| Match live | No | Only the event text changes, on fetch | Only if `CPL_CALENDAR_LIVE_UPDATES_ENABLED` is on |
| Match ended, result | No | On fetch | Only if live updates are on |
| Team renamed | No | On fetch | Yes, one write per live match |
| Venue changed | Not possible: no venue is stored | Not possible | Not possible |
| Player changes | Not shown in events | Not shown | Not shown |

## 4. Architecture chosen

- Domain change commits in the match transaction. The notice is then inserted into `tblNotificationOutbox`. The API does not call push or calendar providers in the request.
- The outbox is drained by the API process every 20 seconds, with a 5-minute lease. This reuses the process that already runs. No second service is added.
- Notifications and calendar writes go through one provider-neutral delivery plan. Push, Google, Microsoft, and the feed are separate channels behind separate flags.
- Every flag defaults off. Turning a flag off stops one channel without a deploy.

Rejected alternatives: a message broker (no broker exists, and the outbox already gives at-least-once delivery at this volume), and a separate worker service (adds a deployment unit for work the API process can do).

## 5. Volume and cost assumptions

No production figures are in this repository, so these are assumptions, not measurements.

| Quantity | Assumed | Basis |
| --- | --- | --- |
| Matches per season | 60 | Assumption: no production count is in the repository |
| Changes per match | about 5 | Assumption: schedule, live, ended, and result edits |
| Connected admins with a calendar | 3 | Admin-only connections |
| Push subscribers | Not known | Public site, no accounts |
| Peak concurrent live matches | 1 to 2 | Assumption |

Estimated writes: about 900 calendar writes per season for three connected admins, plus up to 200 per new connection for the backfill. Unchanged events are skipped by content hash. A season rename writes once per live match per connected admin.

Google Calendar API and Microsoft Graph have per-user and per-project limits. Check the current numbers in each provider's console. The worker backs off on 429 and honours `Retry-After`.

## 6. Open items

- Live Web Push send was verified on desktop Chrome through Google's push service (FCM), with a key pair generated for the test run. Three paths passed: the in-page notice for an open page, a system notification with tag `match-<id>-<event>` when no page was open, and a 410 (gone) once the browser unsubscribed. Production still needs its own key pair in `CPL_VAPID_*`. Desktop Edge (Windows push service, `*.notify.windows.com`) passed the same open-page and no-page checks. After an Edge unsubscribe, the Windows push service still accepted the message rather than answering 410. An Edge device that drops its subscription outside the page is therefore not voided at once; turning alerts off on the page voids it, and so does the page's check on the next visit. Playwright's Firefox build (Mozilla push service, `updates.push.services.mozilla.com`) passed the system-notification and 410 checks. Outside Chromium the service worker always shows the system alert, even with the page in front, because only Chromium documents the focused-tab exemption and Safari drops a push that shows nothing. So Firefox shows a system notification on the open-page path rather than the in-page notice. Branded Firefox, Safari, Android Chrome, and iOS Home Screen push were not sent to.
- The whole push pipeline was also run live once, on a scratch database: the subscribe route, a match set to live, `publish_match_change` called twice for the same change, one `drain_once` pass, and Google's push service to a real Chrome page. One outbox row was queued, the browser showed "Match is live", the row ended `done` after one attempt with a `sent` delivery, the subscription recorded its success, and a second worker pass sent nothing.
- Live Google and Microsoft consent: needs OAuth clients. Google's Testing status expires refresh tokens after seven days.
- iOS Home Screen push and Android Chrome: not tested on devices.
- Per-match removal from a connected Google or Outlook calendar is built, both server and Web row action. Removing every match for a team or season at once is not built. Apple has no write API, so Apple removal is by unsubscribing from the feed only.
- Player names are in calendar event descriptions, from each season's roster. A rename or roster change rewrites the affected events. Push alerts do not list players. Per-match player stats (`tblMatchStats`) are not used, so the lines do not change after a match is played.
- Venue is not stored, so a venue change cannot be synced. Documented as not applicable.
- Calendar-needs-attention alerts go only to a signed-in admin's own devices. Public visitors do not get them.
- Push endpoints are limited to the four browser push services. A browser outside that list cannot subscribe.
- Dashboards and alerting are not built. The deployment has no observability stack in this repo. `GET /notifications/telemetry` exposes the counters and latency that an external tool can scrape.
- The Syncing and Update pending states describe the shared worker queue, not a single connection's backlog.
- Microsoft Graph quotas are not quoted as a fixed number here. Check Microsoft's current limits before relying on one.
- The feed token is a bearer secret. A proxy or host log outside this code must not keep full request paths.
