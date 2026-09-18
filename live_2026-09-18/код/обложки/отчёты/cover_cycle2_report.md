# Cover C2 — frozen candidate, predeploy evidence

Date: 2026-09-14. Production is still C1; this report does not claim applied C2.

Candidate: `cover_candidate2/`. Baseline: exact deployed C1 `cover_candidate/`.
Manifest SHA256 `8b633f65d6e89730c07566207cc30feadfc33de900e2488efbf6d0347472b3b8` (`cover_cycle2_final_sha.json`).
Patch SHA256 `2a3f1e6fc5cae7b4db6811e7909f65b59c621ec4865d8e60961f5a85e5d4256b` (`cover_cycle2.patch`).
Seven production files: api_cover.py, api_cover_helpers.py, cover_server.py, cover_core.py, job_queue_cover.py, static/index.html, new cover_recovery.py. C1 cover_credentials.py unchanged.

## Behavior

- Settings loading starts with Save and generation disabled. Both remain disabled after GET failure. User mode cannot be overwritten by a default Save before the initial GET. C1 saved secrets still return counts only.
- Optional `Idempotency-Key` on both web and API create is durable in SQLite, scoped by authenticated owner. Same key/body returns the same job even at a full queue; same key/different body or deleted tombstone returns 409. UI persists the request identity across a lost response/reload and removes it only after a confirmed job ID.
- New jobs use per-job output directories. Legacy jobs retain their existing directory.
- Remote generation journal records a submitting intent before POST, a FastGen remote ID before polling, the provider family and SHA256 credential identity, a ready image cache, and each published artifact's SHA256. Keys themselves are absent. Restart uses the original credential; unavailable original credentials fail explicitly. It does not round-robin an existing ID into another account.
- A request whose result is unknown and has no remote ID is quarantined. Synchronous adapters get one request; an ambiguous lost reply is not retried automatically. Known IDs are polled rather than recreated. This provides duplicate-payment protection, not automatic reconciliation of providers that expose no identity lookup.
- Publication is atomic and fenced against queue status/run token/cancellation. Late remote IDs can still be recorded after cancellation, but stale workers cannot publish output.
- Remote records also persist exact semantic request, request fingerprint, source-job fingerprint and Flow seed. Title scene, URL thumbnail/analysis/style, per-item adaptation and QC results are checkpointed; a changed source job is refused. Completed state is monotonic: late old-worker updates cannot downgrade ready or replace a known remote ID.
- The API retry route shares the same recovery budget as web retry/resume. `full=true` is a compatibility alias that preserves partial outputs.
- Item identities and ordinals are stable across retries; partial progress keeps the original expected item count and valid files. Three recoveries/resumes maximum, then explicit 409/quarantine; independent jobs remain runnable.
- Explicit fallback regeneration archives old bytes and remote IDs, preserving existing metadata through the filesystem stage. Default seven-day destructive retention is disabled pending acknowledged archive policy. Authenticated `/jobs/{id}/recovery` and `/api/v1/jobs/{id}/recovery` expose a secret-free recovery manifest.
- UI missing/forbidden detail response clears selection and stops detail polling rather than repeating 404 indefinitely.

## Reproducible verification

From `cover_candidate2/`:

```sh
PYTHONPATH=. python3 -m pytest tests/test_cover_recovery.py tests/test_cover_byok_pool.py tests/test_cover_byok_failure.py tests/test_cover_fair_queue.py tests/test_cover_server_keys.py tests/test_cover_atomic_http_cap.py tests/test_cover_billed_provider_gate.py tests/test_cover_validation_sanitization.py tests/test_cover_http_flow.py -q -s
```

The final allowlist run before the last additive retention test: **62 passed, 110.96 seconds**, `cover_cycle2_verified_tests.log`. Additional new retention/archive test: **1 passed, 0.70 seconds**, `cover_cycle2_retention_tests.log`. Compilation passed for all changed Python files. Two already-documented pre-C1 log sanitizer tests were excluded because their failures are baseline behavior, not new regressions.

Concrete scenarios:

1. Real queue/core/allocator; 100 own jobs, 3 own keys, 2 shared-neighbor jobs. All 102 completed. Own key distribution 34/33/33, per-key peak1, machine peak4. Same physical key across two owners plus shared neighbor: running/pending/pending, physical peak1, all3 completed without busy timeout failure.
2. Actual subprocess killed after transport POST and durable remote-ID commit, before poll. New process with reversed own-key order completed using **1 total POST and 1 GET with the original key**. Queue/journal/allocator are not monkeypatched.
3. Upstream503 ambiguous submit with requested max_retries20: **one POST**, repeated recovery refuses new paid submit.
4. 100 concurrent authenticated HTTP creates with cap1 and same idempotency key: **one job**. Changed body409; different request while full429.
5. Two-item actual worker: ready1 + uncertain1. Three HTTP resumes retain1/2 partial count and the first file checksum. **Two POSTs total**. Fourth resume409.
6. Cancel during poll preserves known remote ID and blocks publication; restart normalizes cancellation. Old run token cannot replace a valid file after recovery.
7. Forty-day-old result survives default retention. Fallback regeneration archives identical bytes and retains its remote ID receipt.
8. Browser on current static SHA: real Save click blocked during8-second settings delay; generation disabled. Same after503; after200 own mode and count restored. Dev console error/warning list empty. Actual disappeared fixture row produced a single404, heading became “Задача недоступна”, no further detail polling. Receipt `cover_cycle2_ui_receipt.json` and boundary fixture `tests/ui_cover_c2_app.py`. The fixture job had0 generation tasks; no vendor calls.

## Deploy and rollback

Root must authorize deployment after independent critic GO. Deploy only seven production manifest paths; never tests, fixture DBs, `.bak`, credentials, or candidate environment. Fresh live SHA must equal each baseline hash; new module must be absent. Consistent SQLite backup plus copies of changed live files before replacement. Existing unit `cover-server.service`, cwd `/root/cover_tool`. Drain running/resetting to0 without cancelling pending jobs; hold SQLite writer fence through service stop; atomic replacement; compile/start; verify exact hashes, health and journal. Run isolated network-boundary matrix from downloaded deployed bytes, then deployed UI normal/save/reload/delete and delayed/failure settings checks with a disposable own-only account.

Additive tables in existing queue DB: `job_requests`, `cover_remote`, `cover_artifacts`, `cover_stages`. Job JSON gains artifact_namespace and recovery_attempts. No destructive migration.

Rollback before any C2 work is admitted can restore exact C1 files. After C2 accepted/generations exist, **do not restart C1 workers against C2 recovery jobs**, since they ignore durable submit identities and could repay. Keep service stopped, preserve C2 DB/receipts/files, roll forward or explicitly quarantine those jobs before any C1 restart. Do not replace live DB with backup after new jobs arrived.

## Limits / not yet claimed

S02 and S05/S07 have candidate evidence; only the critic can score and production applied proof is still required. No live paid call, no 100/day throughput claim. External provider outage itself is not a defect; an ambiguous paid request needs reconciliation before further paid generation. Recovery budget is intentionally finite. A retry after a definitive pre-submit rejection also remains quarantined in this version; a new, explicitly submitted job is needed after credentials are repaired.

S09/C3 remains mandatory: disk-budget admission plus verified transfer/archive acknowledgement. Retention-off preserves user results but grows disk; image caches and archived fallbacks also consume space. Shared VPS free-space policy must be applied before the 100/day rollout.

## Additional critic-requested acceptance cases (author-run, not critic-run)

- Kill/restart tests now cover seed, title, URL. Title and URL LLM boundary changes its answer each call: after restart the saved source and adaptation are reused; only the previously unperformed final QC calls LLM. Each finishes with1 total image POST and1 GET using the original key.
- Title QC case: first generated image fails QC; second generation accepted; process killed before second poll. Restart reuses stored failed QC for ordinal1 and polls ordinal2, ending with2 total POST/2 GET; no orphan pending remote rows. Log `cover_cycle2_qc_ordinal_tests.log`1PASS6.48s.
- API attempts2→3; both `full=true` and false thereafter409 with unchanged partial output/fallback counts.
- Actual two-thread old/new run race preserves ready state and rejects different remote ID.
- Source change after stage checkpoint rejects replay; a changed LLM answer with unchanged source returns the old checkpoint.
- `cover_cycle2_stages_verified_tests.log`:16PASS23.19s before added QC/source cases. Latest complete allowlist rerun is `cover_cycle2_final_complete_tests.log`; count and duration recorded after it finishes. Production code is frozen while this run executes.

Final frozen allowlist: **70 passed in127.80 seconds** (`cover_cycle2_final_complete_tests.log`). Independent critic targeted run: **18 passed in29.61 seconds** (`critic_cover_cycle2_final.log`), executed by critic, not this author. Manifest remains8b633f65d6e89730c07566207cc30feadfc33de900e2488efbf6d0347472b3b8.

Historical C1 jobs that already lost a vendor ID cannot reconstruct that ID from C2. New durability guarantees apply to submissions performed by C2; legacy manual resumes should be treated separately from demonstrated crash recovery.
