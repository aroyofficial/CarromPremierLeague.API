# ADR: Web Push and a subscribable match calendar

## Status

Accepted for implementation. Google Calendar API and Microsoft Graph stay disabled until credentials exist.

## Context

The API is one FastAPI process on Render, backed by SQL Server. The public site is a Vue app on GitHub Pages. Admins are the only accounts. There is no Firebase project, no OneSignal app, no job queue, and no calendar client. Matches store a date, two teams, a status, and a result. They do not store a start time or a venue. An in-process realtime hub already tells open pages about changes. It does not wake a closed phone, and it does not update a calendar.

## Decision

Use the standards that this stack can host.

- Web Push with VAPID keys from the environment. A service worker in the web app shows the notification. Subscriptions live in SQL Server. The public site has no fan accounts, so a device subscribes itself. The endpoint is the secret that later changes preferences or turns alerts off. Admin connections are optional.
- A secret `https` calendar feed, plus the `webcal:` form of the same link, is the auto-sync that works without a vendor account. Calendar apps poll it. One-time download remains available and is labeled as a file that will not stay up to date.
- Google Calendar and Microsoft Graph are implemented behind their own flags. The connect flow refuses to start when the client id, secret, redirect base, or encryption key is missing. Refresh tokens are encrypted. The worker upserts events and skips an upsert when the body hash has not changed.
- Match requests insert an outbox row after the match write. They do not call a push service or a calendar API. The same API process drains the outbox. There is no second service to deploy.
- Each flag defaults off. Turning a flag off stops that channel without a code deploy.

## Consequences

- Closed browsers can be notified only when push is configured and the person opted in.
- Calendar freshness follows the app that subscribed. The feed itself is current on every fetch.
- Venue changes and "one hour before the start" cannot be honest until the match row has a venue and a start time.
- A second API process could deliver one outbox row twice. Push tags and calendar hashes make that a replacement, not a second different event.
- Live account sync with Google or Microsoft is blocked until an operator creates those OAuth clients and sets the environment.
