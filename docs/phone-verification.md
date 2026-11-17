# Phone numbers and verification codes

This page covers how phone numbers are stored and checked, how the Indian (+91) verification
code works, how the provider flag is set, and how to roll back the database migration.

## Storage and uniqueness

- Every saved number is canonical E.164 (for example `+919876543210`). The rules are in
  `core/phone_numbers.py` (`canonicalize`). Digits only, leading zeros dropped, a repeated
  country code removed, then 8 to 15 digits. An Indian number must be a 10-digit mobile that
  starts with 6 to 9.
- `tblAdmins.PhoneE164` and `tblPlayers.PhoneE164` have filtered unique indexes
  (`UX_tblAdmins_PhoneE164`, `UX_tblPlayers_PhoneE164`). Voided admins are excluded.
- `tblContactPhones` claims each number once across admins and players (primary key on
  `PhoneE164`). The API claims a number before it saves a profile, so two requests cannot both win.
- Accounts without a phone are valid. The phone is optional for admins and players. The email
  is required for admins.
- `tblAdmins.PhoneVerifiedAt` is set only after a code is confirmed. Nothing else trusts a phone.
  Player phones are stored and made unique, but there is no player verification flow yet.

## Verification codes (dev provider only)

Codes are issued by `services/phone_otp.py`.

| Rule | Value |
| --- | --- |
| Code lifetime | 10 minutes |
| Wrong attempts before a new code is needed | 5 |
| Cooldown between requests | 60 seconds (HTTP 429 with `Retry-After`) |
| Requests per owner per hour | 5 |
| Storage | HMAC-SHA256 of the code with a per-code salt, keyed by `CPL_AUTH_JWT_SECRET`. The plain code is never stored. |
| Logging | Codes and hashes are never logged or returned in responses. |

The error messages are the same for every account. Asking for a code for a number another
profile holds returns "already used", so that answer is rate-limited like a real request: the
refusal counts toward the cooldown and the hourly cap. Using a code and counting a wrong attempt
are single conditional updates, so two parallel requests cannot both use one code or push the
attempts past the limit. The emailed admin codes (activation, sign-in, password) work the same way. Each failure state has its own message: not right, expired,
already used, too many attempts, and request a code first.

### Provider flag

`CPL_PHONE_OTP_PROVIDER` selects the sender:

| Value | Behaviour |
| --- | --- |
| `off` (default) | No code is sent. Verification requests return HTTP 503, so nothing is trusted. |
| `dev` | Only when `CPL_APP_ENV=dev`. Nothing leaves the process. The code is printed to the API's console as `[dev phone code] ...3210: 123456` so you can enter it, and kept in memory for tests. It is never written to the application log or a response. Any other environment falls back to `off`. |

**Production SMS is not enabled.** This project has no production SMS provider configured, and no
free production provider was chosen. Turning on SMS means adding a sender class that implements
`send(e164, code)` and choosing a provider and its per-message cost. Check the provider's
current pricing and DLT registration rules for India before you enable it. Do not set
`CPL_PHONE_OTP_PROVIDER` to a new value until that sender exists.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `CPL_APP_ENV` | `dev`, `stage` or `prod`. `dev` is required for the dev provider. |
| `CPL_AUTH_JWT_SECRET` | Also keys the code HMAC. Use a long random value. Changing it invalidates codes that are in flight. |
| `CPL_PHONE_OTP_PROVIDER` | `off` or `dev`. See above. |

`.env.example` in the API repo lists these with safe defaults.

## Database migration

The change is in `CarromPremierLeague.Database/Setup-Database.ps1`, and `setup.sql` is generated
from it. The deploy workflow runs the whole script on every database deploy, so the backfill is
written to be run again while the API is live. It runs as one transaction: a failure leaves the
data as it was.

1. Player claims whose player was deleted or changed number are released.
2. Admin numbers are normalised with the same rules as `canonicalize`. Each row gets a reason:
   - `normalized`: stored in canonical form. A number that changes loses its verification.
   - `malformed`: cannot be read. It is kept as typed but is not trusted (`PhoneE164` stays NULL).
     The next save through the API asks for a valid number.
   - `duplicate`: another admin keeps the number. The one that already claims it wins, then the
     one that verified it, then the lowest Id. This admin's number is cleared.
   - `conflict`: a player already claims the number. The admin's number is cleared.
   - `removed`: the number was removed by an older API but `PhoneE164` was left behind. Cleared.
3. Every changed row is written to `dbo.tblPhoneMigrationLog` first, with the old number, old
   `PhoneE164`, old verification time, the new values, and the reason.
4. Admin claims in `tblContactPhones` are reconciled with the profiles (stale claims removed,
   missing ones added). No claim is ever deleted and re-added.
5. The unique indexes are created, `tblPhoneOtps` is created, and the old `Verified` column on
   `tblContactPhones` is removed, if an earlier build left it behind.

This was checked on scratch databases: an upgrade from the previous schema with messy, duplicate
and unreadable numbers; a re-run after simulated writes from the old API (a number taken from a
player, a removed number, a changed number, a lower Id taking a verified number) and a deleted
player; a further re-run that changed nothing; and a fresh install.

The migration log holds old phone numbers. Only database owners should read it. Purge it once the
migration has been checked (for example after 90 days).

Deleting a player releases their number, the same as deleting an admin.

Rollback notes (each is also in the script beside its change):

- Phone normalisation: copy `OldPhoneCode`, `OldPhoneNumber`, `OldPhoneE164` and
  `OldPhoneVerifiedAt` back onto `dbo.tblAdmins` for the logged rows. Restoring a `duplicate` or
  `conflict` row recreates a shared number, so decide per row. Then
  `DROP TABLE dbo.tblPhoneMigrationLog` if the log is no longer needed.
- Unique indexes: `DROP INDEX UX_tblAdmins_PhoneE164 ON dbo.tblAdmins;` and the matching player index.
- `PhoneE164` columns: drop the index first, then `ALTER TABLE dbo.tblAdmins DROP COLUMN PhoneE164;`.
  Player phone columns: drop the player index, then
  `ALTER TABLE dbo.tblPlayers DROP COLUMN PhoneE164, PhoneNumber, PhoneCode;`.
- `tblPhoneOtps`: `DROP TABLE dbo.tblPhoneOtps;` (nothing else references it).
- `PhoneVerifiedAt`: `ALTER TABLE dbo.tblAdmins DROP COLUMN PhoneVerifiedAt;` (no other columns are affected).
- `tblContactPhones`: the table is only read and written by the phone code. Dropping it turns off
  uniqueness across owners, so restore the API version that does not use it first.

Rolling the API back to a version without phone codes: that version can change an admin's number
without clearing `PhoneVerifiedAt`. The next database deploy clears it, or run
`UPDATE dbo.tblAdmins SET PhoneVerifiedAt = NULL WHERE PhoneVerifiedAt IS NOT NULL;` before the new
API comes back.

Restore a backup before any rollback that drops a column or table. Take one first.

## Manual QA

1. Start the API with `CPL_APP_ENV=dev` and `CPL_PHONE_OTP_PROVIDER=dev`.
2. Sign in as an admin without a verified phone. Request a code. Confirm there is a 60-second cooldown.
3. Enter a wrong code five times. Confirm the message changes to "too many" and a new code is needed.
4. Request a code, wait 10 minutes (or change the clock in a test), and confirm "expired".
5. Use a correct code twice. Confirm "already used" on the second try.
6. With `CPL_PHONE_OTP_PROVIDER=off`, request a code and confirm HTTP 503. Nothing is sent.
7. Create two admins or players with the same number written differently (for example
   `9876543210` and `09876 543210`, both +91). Confirm the second is refused.
8. Create an admin with no phone. Confirm it saves.
