# Match alerts and calendar sync

This server can tell people about matches in two ways. Both are off until an operator turns them on.

- Web Push sends a browser notification when a single match is created, moved, cancelled, started, finished, or has its result changed, and on the morning of a scheduled match. Building or rebuilding a season's league schedule sends one "Schedule updated" alert per device rather than one per match.
- A calendar feed is a private `https` link. Apple Calendar, Google Calendar, and Outlook can subscribe to it and pick up later changes. A file someone saves once does not.

Google Calendar and Microsoft 365 can also be connected by an admin, but only after OAuth client credentials exist. Until then those two buttons stay off. The feed does not need them.

## What the league can and cannot say

A match row has a date, two teams, a status, and a result. It does not have a start time or a venue.

- Calendar events are all-day events on the match date.
- The reminder is 09:00 IST on that date, because there is no kickoff to count back from.
- Moving the date is the postpone. There is no separate postponed status.
- A voided or deleted match is the cancellation.
- A venue change cannot be synced, because no venue is stored.
- Renaming a team or a season changes no match row, so it sends no notice. Connected calendars and later alerts show the new name the next time that match changes. The subscription feed shows it on its next fetch.
- A device that follows every season also gets alerts for seasons hidden from the public site (`tblSeasons.Show = 0`). The same matches are already public through `GET /matches`.

Public visitors do not have accounts. They subscribe a device, or a calendar link. Connecting Google or Outlook requires an admin sign-in. Refresh tokens are encrypted with `CPL_CALENDAR_TOKEN_KEY` before they are stored.

## Flags

Each flag is independent and defaults off. Set them on the API host. Restart the API after a change. The public site reads `GET /notifications/capabilities` and hides anything that is not ready.

| Variable | Off means |
| --- | --- |
| `CPL_PUSH_ENABLED` | No browser alerts. Also needs `CPL_VAPID_PUBLIC_KEY`, `CPL_VAPID_PRIVATE_KEY`, and `CPL_VAPID_SUBJECT`. |
| `CPL_CALENDAR_FEED_ENABLED` | No subscription links and no one-time match file. |
| `CPL_GOOGLE_CALENDAR_ENABLED` | No Google account connection. Also needs the Google client id, secret, `CPL_OAUTH_REDIRECT_BASE`, and `CPL_CALENDAR_TOKEN_KEY`. |
| `CPL_MICROSOFT_CALENDAR_ENABLED` | No Microsoft account connection. Same extra values, with the Microsoft client id and secret. |
| `CPL_PUSH_LIVE_ENABLED` | No "Match is live" alert, even with push on. Every other alert still sends. |
| `CPL_CALENDAR_LIVE_UPDATES_ENABLED` | A match going live, finishing, or having its result changed does not trigger a write to connected Google and Outlook calendars. Schedule changes and cancellations still do, and each write carries the match's current title, which can include the live state or the result. The subscription feed always shows the current state. |
| `CPL_NOTIFY_WORKER` | `false` stops the in-process sender. The feed still works, because calendars pull it. |

`CPL_OAUTH_REDIRECT_BASE` is the public API origin, for example `https://api.example.com`. The redirect URLs to register are:

- `{origin}/calendar/google/callback`
- `{origin}/calendar/microsoft/callback`

Generate a VAPID pair with `npx web-push generate-vapid-keys`. Generate the token key with:

```text
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Do not commit either secret. Do not put them in the web build.

## Order of deployment

1. Create the subscription, feed, connection, outbox, and delivery tables. The block is in both `Setup-Database.ps1` and `setup.sql`, and it is safe to run again. Re-running it on a database built from an earlier draft drops `FK_tblCalendarEventMaps_tblMatches`, which would otherwise block a schedule rebuild once a match has synced. On a fresh database, run `setup.sql` with `sqlcmd -b`. SQL generated from `Setup-Database.ps1` currently stops in `usp_GetSeasonTopCoinPotters`, a pre-existing gap, and the `.ps1` overwrites `setup.sql` when it runs.
2. Deploy the API with the flags still false.
3. Deploy the web. The new controls stay hidden while the flags are false.
4. Turn on `CPL_CALENDAR_FEED_ENABLED` first. It needs no third-party account.
5. Turn on push only after the VAPID keys are present.
6. Turn on Google or Microsoft only after that provider's OAuth client exists.
7. Turn on `CPL_PUSH_LIVE_ENABLED` and `CPL_CALENDAR_LIVE_UPDATES_ENABLED` last, once the scheduled and changed alerts have run cleanly.

Match create, update, reset, and delete keep working if the new tables are missing or the flags are off. A notice that cannot be queued is logged and does not fail the match.

## How a change reaches someone

The match request writes and commits the match, then inserts one outbox row per change. It does not call a push service or a calendar API. The two writes are not one transaction. If the API process stops between them, the match change is saved and that one notice is never queued. The feed still shows the change, and a connected calendar catches up the next time that match changes. The API process reads pending rows about every 20 seconds. A row is leased for five minutes before it is sent. If the process stops mid-send, the lease expires and a later pass picks the row up again. A duplicate change uses the same idempotency key and is stored once.

Failures are sorted before anything retries:

- Rate limits (HTTP 429) and provider outages (5xx) retry with exponential backoff from one minute to one hour, about 20% jitter, and the provider's `Retry-After` when it asks for longer.
- A rejected request (other 4xx) is recorded and not retried for that channel.
- A revoked or expired Google or Microsoft grant is recorded as `reauthorization_required`, and the connection stops receiving writes. Nothing retries it.
- After eight failed attempts a row is dead. Its `LastError` says why.

Each delivery attempt is stored in `tblNotificationDeliveries` with a sanitized category such as `rate_limited` or `reauthorization_required`. An unexpected error is stored as its type only, for example `unexpected: ValueError`. Provider response bodies and push endpoints are never stored or logged. A failure while sending is logged by its type only. A database error while queueing or reading notices is logged with its traceback, which can include SQL text but not tokens or endpoints.

A notice that is retried skips every device already recorded as `sent` for it, so a partial failure does not alert the same phone twice.

How each provider write lands on exactly one event:

- **Google Calendar.** The event id is fixed per match (`cplmatch` and the zero-padded match id). The worker updates that id. If Google answers 404 because the event does not exist yet, it creates the event under the same id. If that create answers 409, another attempt already created it, and the worker updates it. A cancelled match is written with `status: cancelled`.
- **Microsoft Graph.** Graph picks its own ids, so the worker stores the id it returns in `tblCalendarEventMaps`. Updates go to that id. Only a 404, meaning the user deleted the event, creates a new one. A rate limit, an outage, or an expired token is retried and never becomes a second event. A retried create carries the same `transactionId`, so Graph returns the first event instead of a duplicate. A cancelled match deletes the event, because `isCancelled` cannot be set on an event the user owns.

Building a season's schedule (`POST /seasons/{id}/schedule`) deletes every unplayed match in the season and inserts the new pairings with new ids. Before the rebuild, the API reads the matches it is about to delete. After it:

- Each deleted match is withdrawn from every connected calendar: Google marks the event cancelled, and Outlook deletes it. The worker works from the stored copy, because the row is gone. Any notice for a match whose row is gone, including a late retry of an older one, can only withdraw its event, and only a cancellation alert is sent for it.
- Each new match is written to every connected calendar.
- Devices get one "Schedule updated" alert with the number of new matches. It honours the "scheduled" choice and the season filter, and it ignores the team filter, because the whole season moved.

If the rebuild fails, nothing is queued. If queueing fails, the rebuild still succeeds and the failure is logged.

Web Push details:

- The alert links to `/schedule#match-<id>`, and that match's card scrolls into view. A click reuses an open schedule tab. The season alert links to `/schedule`.
- Each alert has the tag `match-<id>-<event>`, so a repeat of the same notice replaces the earlier alert instead of stacking. The season alert uses `season-<id>-schedule`.
- The payload carries `noticeId` (the outbox row) and `version` (the same number as the calendar `SEQUENCE`), plus the title, body, link, match id, season id, and event. It carries no personal data.
- When the browser rotates a subscription, the service worker registers the new endpoint with `replaces_endpoint`. The new row keeps the old row's choices, and the old row is turned off. On the next visit the page swaps its saved endpoint for the browser's current one and reports the rotation too, so it is registered even when the worker's event never fired. A second report of the same rotation keeps the row and its choices as they are. If the browser no longer has a subscription, the page shows alerts as off.

Browser support:

| Browser | Alerts | Notes |
| --- | --- | --- |
| Chrome, Edge, Opera, Samsung Internet (desktop and Android) | Yes | Blocked permission shows the "allow in site settings" message. |
| Firefox (desktop and Android) | Yes | Same. |
| Safari 16.1 or later on macOS 13 or later | Yes | |
| Safari and every browser on iPhone and iPad | Only from the Home Screen, on iOS 16.4 or later | In a normal tab the page shows the Home Screen fallback. |
| Browsers without service workers or the Push API, and private windows that disable them | No | The page says alerts are not available and offers the calendar options. |

This table comes from the browsers' documented support. None of these browsers was tested in a real run here.

Subscribe and feed-creation requests are rate limited per visitor. Behind the host's proxy, the visitor is the first `X-Forwarded-For` address, the same rule the assistant limiter uses.

A feed link is a bearer secret, because anyone with the URL can read that feed. Its token has 256 random bits. The database stores only the token's SHA-256, so a copy of the table cannot open a feed. The API's access log writes `/calendar/feeds/[redacted]` in place of the token. A proxy or host log in front of the API is outside this code, so check that it does not keep full request paths. Revoking a link voids it at once.

The calendar feed does not use the outbox. The next time a calendar app fetches the link, it receives the current matches. `SEQUENCE` follows the match's update time, and a cancelled match is `STATUS:CANCELLED`. Apps choose their own refresh interval. The feed asks for 15 minutes. Google's "add by URL" often refreshes about once a day. That limit belongs to Google, not to this feed. The Google Calendar API path updates the event from the worker when credentials exist, which is the faster account sync.

There is one API process. A second process could deliver the same row twice. Push notifications reuse a tag so the alert is replaced. Calendar writes are skipped when the event body has not changed.

## Connections and reauthorization

An admin connects Google Calendar or Outlook from the match page or the admin settings page. Each connection belongs to the admin who made it. An admin sees and disconnects only their own connections.

The OAuth request carries two protections:

- **State.** A `state` value is signed with the API's JWT secret. It names the provider and the admin, and it expires after ten minutes. A callback whose state is missing, altered, expired, or meant for the other provider is refused.
- **PKCE.** The request also carries a PKCE (RFC 7636) S256 challenge. The verifier is derived from that state with the same secret, so nothing has to be stored between the two requests. Someone who captures the authorization code and the state still cannot redeem the code without the server's secret. Google and Microsoft both accept PKCE alongside the client secret this confidential server already sends.

Connecting fills the calendar. Right after the callback, every match still to be played, from today on and up to 200, is queued for that calendar only. Later changes reach it as they happen. Connecting covers the whole schedule; to add a single match, use its one-time file.

States shown for a connection:

- **Connected**: nothing has been written to the calendar yet.
- **Update pending**: notices are still queued.
- **Synced**: the last write succeeded or was already up to date. The admin page shows when.
- **Sync failed. It will retry.**: the last write failed with a retryable error.
- **Reauthorization required**: Google or Microsoft refused the stored grant. Click **Connect again**. Nothing else is needed.
- **Disconnect**: the connection is voided and its stored token is blanked. Google's grant is also revoked, best effort. Microsoft has no revoke endpoint in this integration, so the stored token is deleted. The grant can be removed from the Microsoft account's app permissions. Events already on the calendar stay until someone deletes them.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Alerts never arrive | `GET /notifications/capabilities`. The `reason` names the missing flag or key. Then check that the device has a row in `tblPushSubscriptions` with `Void = 0`, and that the browser permission is granted. |
| iPhone or iPad gets no alerts | The site must be added to the Home Screen, on iOS 16.4 or later, and opened from there. |
| A calendar app shows an old match time | Calendar apps decide when to refresh a subscription. Google's "add by URL" often refreshes about once a day. Connecting Google or Outlook updates sooner. |
| Connection shows "Reauthorization required" | Click Connect again. The stored grant was revoked or expired. |
| Matches not reaching a connected calendar | `GET /notifications/telemetry` for the backlog, or `SELECT Status, COUNT(*) FROM tblNotificationOutbox GROUP BY Status`. Rows marked `dead` have a `LastError`. Deliveries are in `tblNotificationDeliveries`. |
| A phone stopped getting alerts after weeks | The browser may have rotated its subscription. The service worker re-registers it; if the row is `Void = 1`, turn alerts on again on that device. |
| Feed link returns 404 | The `CPL_CALENDAR_FEED_ENABLED` flag is off, or the link was revoked. |
| No Connect button | The admin is not signed in, or the provider's flag and credentials are not set. |

## Rollback

Set the relevant flag to `false` and restart the API.

- Push stops sending. Devices stay subscribed and can be used again when the flag is turned on.
- Feed URLs start returning 404, so calendars stop picking up changes. They keep the last copy they stored. Responses use `Cache-Control: private`, so shared caches do not keep the secret link.
- Google and Microsoft stop receiving updates. The stored connection remains until it is replaced.

No code deploy is required for that rollback. Dropping the new tables is only for removing stored subscriptions and queued notices.

## Telemetry

`GET /notifications/telemetry` (admin sign-in) returns two parts:

- `process_counts`: counters for this API process, such as `delivery.sent.push`, `delivery.failed.google.rate_limited`, `connection.revoked.microsoft`, `outbox.retry`, and `push.subscription_gone`. They reset when the process restarts.
- `delivery_health`: read from the database. It holds `backlog` (pending or leased notices), `dead`, `oldest_pending_seconds`, `active_subscriptions`, `active_connections`, `connections_needing_reauth`, and `failed_last_24h` and `sent_last_24h`.

There is no dashboard or alerting service in this deployment. Two things are worth watching: `oldest_pending_seconds` above a few minutes means the worker is not draining, and a rising `dead` count means a provider keeps rejecting writes.

## Cost and quotas

- **Web Push** uses each browser's own push service and has no per-message fee. A payload is a few hundred bytes, well under the 4 KB limit. One notice sends one message per subscribed device that wants that kind of alert.
- **Calendar feed**: one database read per fetch, cached privately for 60 seconds.
- **Google Calendar API**: free within the project's quotas. Check them in Google Cloud Console under APIs & Services, Quotas. There is a per-project daily limit and a per-user per-minute limit. The worker writes once per connected admin per match change, sends at most 20 notices per pass, and backs off on 429.
- **Microsoft Graph**: free. Outlook limits each app to about 10,000 requests per mailbox per 10 minutes and a few concurrent requests. A 429 with `Retry-After` is honoured.

Volume for a typical season, assuming 60 matches, about 5 changes each, and 3 connected admins: about 900 calendar writes per season, plus up to 200 per new connection for the backfill. Unchanged events are skipped by content hash. No extra host is required.

## Tests

- `python -m unittest tests.test_notify`: flags (including the two live switches), event classification, ICS text, provider write behaviour (Google create-then-update, Microsoft no-duplicate and delete-on-cancel), retries, rotation, backfill, sync states, time zones, PKCE, feed-token hashing and log redaction, the schedule rebuild, and the match routes queueing a notice after each write without failing when the queue does. With `pip install icalendar`, the feed is also parsed by a real iCalendar parser; without it, those tests are skipped.
- `tests.test_notify_sql`: the repository SQL against a disposable SQL Server database. Set `CPL_NOTIFY_SQL_TEST_CONNECTION` to its ODBC connection string. Never point it at a live database.
- Web: `node --test src/utils/matchFollow.test.mjs src/utils/serviceWorker.test.mjs` covers the alert choices, feed links, sync-state labels, the page adopting a rotated endpoint, and the service worker's push, click, and subscription-rotation handlers.

## What was not verified live

VAPID keys, a Google OAuth client, and a Microsoft OAuth client are not in this repository, so a real push and a real account sync were not sent. Google and Microsoft behaviour is tested against their documented responses, not against the live services. iOS Web Push needs the site added to the Home Screen on iOS 16.4 or later. That install was not exercised on a device. No browser end-to-end test runs in this repository.

## Status checklist

| Item | Status |
| --- | --- |
| Outbox, lease, retry, dead state, dedupe | Validated: unit tests and SQL tests on SQL Server |
| Schema block, safe to re-run | Validated: `setup.sql` run twice on a fresh database |
| Calendar subscription feed and one-time file | Validated by tests; behind `CPL_CALENDAR_FEED_ENABLED` |
| ICS format (UID, SEQUENCE, LAST-MODIFIED, folding, cancel) | Validated by a real iCalendar parser |
| Push preferences, per-device rows, rotation, gone endpoints | Validated by tests; behind `CPL_PUSH_ENABLED` |
| Sending a real push | Needs VAPID keys; not sent |
| Service worker push, click, rotation | Validated in a simulated worker; not run in a real browser |
| Google and Microsoft OAuth (state, PKCE), writes, backfill | Tested against documented responses; needs OAuth clients; behind flags |
| Season schedule rebuild reaches calendars and devices | Validated: unit tests, plus a SQL test that deletes a synced match and queues its withdrawal |
| Match create, update, reset, and delete queue notices | Validated by route-level tests |
| Live alerts and live calendar updates | Validated by tests; behind `CPL_PUSH_LIVE_ENABLED` and `CPL_CALENDAR_LIVE_UPDATES_ENABLED` |
| Feed token hashed at rest, redacted in API access logs | Validated by unit and SQL tests; proxy logs are the host's to check |
| iOS Home Screen push | Fallback message shown; not tried on a device |
| Unsupported or blocked browsers | Fallback messages; logic covered by tests, not by a browser run |
| Dashboards and alerting | Not built; counts are available from `GET /notifications/telemetry` |
