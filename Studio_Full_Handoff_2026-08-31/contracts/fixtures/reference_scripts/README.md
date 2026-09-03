# Sanitized operational references

These files preserve two useful production patterns without production data:

- `buffer3_runner_sanitized.py` shows per-item checkpointing, bounded retry, quarantine, and durable event append.
- `wave20_upload_reference.py` shows frozen-manifest identity checks and the retry-safe versus reconcile-only upload boundary.

They are references, not ready-to-run production launchers. They contain no real channel IDs, profile IDs, machine names, network addresses, session material, credentials, or asset paths.
