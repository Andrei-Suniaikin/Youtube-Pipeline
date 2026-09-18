# Video Ops watchdog: systemd deployment

Production layout:

- application and virtualenv: `/opt/video-ops-watchdog`;
- configuration: `/etc/video-ops-watchdog/config.json`;
- optional environment file: `/etc/video-ops-watchdog/environment`;
- persistent state: `/var/lib/video-ops-watchdog/state.db`.

The units do not send startup or test messages. `video-ops-watchdog.timer` runs the
lightweight check every 15 minutes. `video-ops-review.timer` runs the review at 10:10,
16:10 and 22:10 in `Asia/Ho_Chi_Minh`. Both timers use `Persistent=true`, so systemd runs
one missed activation after the host comes back.

## Current schedule and semantics

The explicit config contains 44 rows, of which 41 are enabled: 17 active WW2 channels
and 24 rewrite channels. `Dosarele Reichului`, `Tajemnice Trzeciej Rzeszy` and
`Stín Třetí Říše` remain as disabled audit rows after the owner's 2026-08-13 decision;
the dead `Spisy` channel is excluded. Other inventory channels are not silently treated
as active: absence from this config means "schedule not established".

The operational publication day is 23:00–23:00 in `Asia/Ho_Chi_Minh`. This intentionally
keeps one rewrite batch together when some videos become public shortly before midnight
and others shortly after it. Current public deadlines are:

- rewrite: 01:30;
- WW2 core: 10:30;
- WW2 HR/EL/HU: 15:00.

The deterministic notifications are a 10:00 daily summary, one immediate grouped alert
after any missed channel deadline, an 18:00 reminder when a public release is still
missing, and a 22:30 final result. Infrastructure/RSS failures alert only after three
consecutive checks and send one recovery after the component is healthy again. Codex
receives the fresh machine report three times per day in read-only/ephemeral mode and
sends only a new actionable judgment.

Public RSS proves that a release is public. Its absence cannot prove that a video was
not uploaded, is not private, or is not scheduled in Studio; all user-facing wording
preserves that distinction. Exact future private titles require an authenticated upload
manifest and are deliberately not inferred from RSS. Canonical uploader receipts add a
separate `upload_status`; they never change RSS-backed `publication_status` or
`fulfilled_publications`.

## Exact Studio receipts

An uploader may emit a receipt only after the exact final `ytcp-video-row` proves all of
the following: the verified UC, the 11-character video ID, machine state `SCHEDULED`,
`visibility.userSetVisibility=VIDEO_USER_SET_VISIBILITY_SCHEDULED`, the actual
`scheduledTimeSeconds`, and exact requested-vs-row title equality after NFC, whitespace
collapse and case-folding. The receipt stores the normalized title hash and frozen
manifest hash, not upload intent or the legacy shared upload log.

Producer order is fixed:

1. atomically write `<canonical-sha256>.json` to the local retry outbox using a unique
   temporary file, fsync, a process lock and no-replace publication;
2. send that complete file on stdin over a dedicated SSH key;
3. move it to local `sent/` only after an exact `OK <sha256> ...` acknowledgement.

An SSH/network failure leaves the receipt in the outbox and must not change the successful
upload result or cause a re-upload. A syntactically exact permanent VPS `ERROR <code>`
moves that receipt to local `deadletter/`. Transport failures, future skew, VPS
inbox/config/lock failures, and unknown server error codes stay pending (fail-safe);
only an explicit semantic-validation allowlist is permanent. `sent/` and `deadletter/`
payloads are retained for
30 days by default, while the VPS SQLite audit retains outcomes for the configured
`audit_retention_days`. Retry pending files with:

```bash
python3 tools/video_ops_watchdog/receipt_io.py push \
  --outbox /absolute/private/video-ops-receipts/outbox \
  --target video-ops-receipts-vps
```

Run the same command every five minutes (systemd timer on Minsk, launchd on Mac) in
addition to the uploader's immediate attempt. The SSH alias must use a dedicated key.
Ready-to-install producer definitions are in `deploy/producer/`. On Minsk install the
two units under `~/.config/systemd/user/`, then run `systemctl --user daemon-reload` and
`systemctl --user enable --now video-ops-receipt-push.timer`. Make that user timer survive
logout and reboot with `sudo loginctl enable-linger vova`, then verify
`loginctl show-user vova -p Linger` prints `Linger=yes`. On Mac install the plist
as `~/Library/LaunchAgents/com.videostudio.receipt-push.plist` and load it with
`launchctl bootstrap gui/$(id -u) ...`. Do this only after the SSH alias and dedicated
forced-command key have been verified.

Put one forced-command entry per producer in root's `authorized_keys`; the pinned
`--source-host` must match that producer's receipt:

```text
restrict,command="/opt/video-ops-watchdog/.venv/bin/python /opt/video-ops-watchdog/receipt_io.py ingest --config /etc/video-ops-watchdog/config.json --inbox /var/lib/video-ops-watchdog/receipts/inbox --source-host minsk" ssh-ed25519 <MINSK_PUBLIC_KEY>
restrict,command="/opt/video-ops-watchdog/.venv/bin/python /opt/video-ops-watchdog/receipt_io.py ingest --config /etc/video-ops-watchdog/config.json --inbox /var/lib/video-ops-watchdog/receipts/inbox --source-host mac" ssh-ed25519 <MAC_PUBLIC_KEY>
```

Do not add an HMAC secret. Authenticity is the scoped SSH key plus forced command;
canonical SHA-256 detects incomplete or changed files. Ingest validates schema/hash,
the key-pinned source, exact Studio evidence, an enabled known UC and freshness before an
atomic inbox write. It acknowledges semantic conflicts as delivered so they do not retry
forever; the watchdog exposes them as `upload_status=conflict`.

For a scheduled upload, pass the per-batch frozen manifest hash and producer identity:

```bash
python3 src/upload_worker.py ... \
  --receipt-outbox /absolute/private/video-ops-receipts/outbox \
  --receipt-manifest-sha256 <64-lowercase-hex> \
  --receipt-manifest-name <frozen-manifest-basename.json> \
  --receipt-operational-date <YYYY-MM-DD> \
  --receipt-source-host minsk \
  --receipt-ssh-target video-ops-receipts-vps
```

Receipt options are all-or-none: any partial receipt flag fails before AdsPower opens.
The outbox and an explicit `--receipt-io-path` must be absolute paths.

The worker also emits `__UPLOAD_RESULT__|<json>` for exact audit evidence. A title
mismatch returns failure with the known `video_id` and `do_not_reupload=true`: repair the
same Studio video, never upload a duplicate.

The watchdog rotates stale inbox payloads to `archive/expired`, rejected payloads to
`archive/rejected`, caps report diagnostics, and keeps archived rejects unhealthy long
enough for the configured multi-strike infrastructure alert. Configure
`archive_retention_days`, `audit_retention_days`, and `diagnostic_cap` under `receipts`.
An enabled missing/non-directory inbox is unhealthy; an existing empty inbox is healthy
but leaves channel uploads unconfirmed. With `receipts.enabled=false`, the inbox is not
created or scanned.

## Install

Run as root after the application, virtualenv and production config are in place:

```bash
cd /opt/video-ops-watchdog
install -d -o root -g root -m 0700 /etc/video-ops-watchdog
install -d -o root -g root -m 0700 /var/lib/video-ops-watchdog/receipts/inbox
chown root:root /etc/video-ops-watchdog/config.json
chmod 0600 /etc/video-ops-watchdog/config.json

install -o root -g root -m 0644 deploy/video-ops-watchdog.service /etc/systemd/system/
install -o root -g root -m 0644 deploy/video-ops-watchdog.timer /etc/systemd/system/
install -o root -g root -m 0644 deploy/video-ops-review.service /etc/systemd/system/
install -o root -g root -m 0644 deploy/video-ops-review.timer /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now video-ops-watchdog.timer video-ops-review.timer
```

If `/etc/video-ops-watchdog/environment` is needed, create it separately with mode
`0600` and ownership `root:root`. Do not put secret values in units, this README,
command history or source control.

The services intentionally run as root because their read-only inputs include the
VideoStudio production database, the adjacent YTCortiez configuration and Codex auth.
`ProtectSystem=strict` keeps those inputs read-only; only
`/var/lib/video-ops-watchdog` is writable during checks. The review additionally has
write access to `/root/.codex`, because Codex CLI may refresh its OAuth file. Their
capability set is limited to `CAP_DAC_READ_SEARCH`, which is needed to read
service-owned files without granting write or service-management capabilities.

## Verify

```bash
systemd-analyze verify \
  /etc/systemd/system/video-ops-watchdog.service \
  /etc/systemd/system/video-ops-watchdog.timer \
  /etc/systemd/system/video-ops-review.service \
  /etc/systemd/system/video-ops-review.timer

systemctl list-timers --all 'video-ops-*'
systemctl start video-ops-watchdog.service
systemctl status video-ops-watchdog.service --no-pager
journalctl -u video-ops-watchdog.service -n 50 --no-pager

python3 - <<'PY'
import json
report = json.load(open('/var/lib/video-ops-watchdog/latest_report.json'))
print(report['generated_at'], report['summary'])
print({name: value['ok'] for name, value in report['infrastructure'].items()})
print(report['upload_receipts'])
PY
```

Do not run Telegram test commands during deployment. A manual service start performs a
real check and must rely on the application's normal deduplication rules.
