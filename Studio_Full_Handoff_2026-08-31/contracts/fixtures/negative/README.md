# Expected negative cases

- `frozen_manifest.path_escape.json`: reject the `../outside.mp4` asset path.
- `studio_scheduled_row.wrong_epoch.json`: preserve the known video identity but reject exact scheduled success.
- `attempt_journal.upload_started.json`: classify as `blocked`, never as retry-safe.
- `receipt_v2.tampered.json`: reject because the canonical SHA-256 no longer matches.
- `watchdog_config.duplicate_channel.json`: reject the duplicated channel identity.
