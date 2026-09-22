# Render cycle2 candidate handoff

Production unchanged. Fresh source from Minsk `/opt/video_agent`, hashes in `render_cycle2_baseline.json`. Apply three reviewed files, never the backup files: `render_puller.py`, `render_worker.py`, new `render_delivery.py`. Service `vsrender-puller.service`. Exact current candidate hashes/diff: `render_cycle2_manifest.json`, `render_cycle2.diff`.

Changes: claim-specific work directories; private durable outbox manifests and completion marker; fsync and atomic writes; result SHA256/size and verified ACK; idempotent receipt lookup before retry; HTTP failure/backoff retains MP4; separate heartbeat for all pending deliveries; stale claims quarantined; cross-process file locks; bounded download/render/upload/overall/Whisper/no-progress execution and kill of process groups; disk admission stops new work before outbox/free-space limit. Missing required transcript is a terminal reported error with MP4 retained. A restart before completion marker retains uncertain media even after error ACK. No completed/uncertain media is silently removed merely because the process restarted.

Default caps:3 concurrent renders maximum;32GiB minimum free disk,64GiB retained outbox budget; heartbeat5s, download/upload1800s, render10800s, Whisper1200s, no observable progress900s, overall job14400s. Quiet CPU work or growing files counts as progress; the total/stage deadline still bounds CPU spins. These are initial protective thresholds, not a claim that a90-minute Ken Burns render fits throughput. Final sizing belongs to exact-profile cycle4.

Network contract is coordinated with root: GET `/api/render/{jid}/receipt`; POST result uses X-Render-Claim plus X-Render-SHA256 or X-Render-Error-SHA256 (UTF-8 error[:480]). Accepted receipt fields: accepted=true, job_id, status plus sha256/size for done or error_sha256 for error. Server must preserve claim token on terminal, deduplicate repeated submissions, atomically fence staged file publish, never let stale upload delete current final. Config must include auto_chapters to require transcript delivery before MP4. This client must NOT be deployed before that server contract is ready; baseline server has no receipt endpoint.

Verification:

```sh
python3 research/launch_hardening_20260914/render_verify_cycle2.py
python3 -m py_compile research/launch_hardening_20260914/render_candidate/render_puller.py research/launch_hardening_20260914/render_candidate/render_delivery.py research/launch_hardening_20260914/render_candidate/render_worker.py
```

`render_verify_cycle2_result.json`: all checks PASS. Actual candidate render_worker produced a3-second256×144 MP4 with audio and video, parsed by ffprobe;20+ heartbeats while downloading/rendering/delivering. Real requests used a local fault-injecting HTTP server; real files, durable store, OS locks, child processes and process groups. Verified: lost response after commit (one upload only), dropped upload then retry,500 then replay from a new store, stale claim quarantine, mismatched digest refusal, marker-based crash recovery, error receipt replay, outbox/disk limits, wall/Whisper/no-progress process termination, cross-process lock, heartbeat during backoff, missing transcript and uncertain pre-marker media retention. This is orchestration evidence, not a100/day benchmark or a live Vulkan/CUDA/Whisper capacity test.

Before deploy: independent critic GO, then joint integration against root's exact new server API. Drain Minsk current renders before restarting its service; keep all current work and uploads. Backup current two files with timestamp and snapshot any new outbox state; new module has no previous production version. Stage exact payload and py_compile with service Python, atomic replacements, one restart, confirm main PID/module hashes and health/claim polling. Root controls deployment and the canary window. Do not start target load while production jobs occupy the agreed slots.

Rollback: drain both active renders and delivery requests; preserve work/outbox files; restore only prior two source files after CAS and restart. Baseline puller does not understand outbox: stop fresh claims until retained deliveries are reconciled with the new receipt API, or run the new outbox handler separately before rolling back. Do not delete render_delivery.py or manifests to make rollback appear clean. Keep the server idempotent receipt extension available for pending delivery reconciliation. Quarantine needs operator/retention treatment in cycle3; its bytes count toward admission and unacknowledged files are never auto-expired.

Remaining acceptance: root server atomic result/receipt path and stale-uploader race tests; exact applied code and live tiny canary; intended90-minute300–500 actual images/fade/KB profile with≥3 parallel renders; auto_sound parity and storage policy in later cycles. No production readiness score is claimed by this candidate alone.

## Critic corrections within cycle2

Both blockers are fixed and reproduced on the final candidate. Worker and replay outer loops catch filesystem failures, record failure counters/last tick/last error, back off, and continue; the main process logs loop health every30s. A real lock-path directory caused IsADirectoryError, then was repaired; the same worker rendered the next actual MP4, and the same outbox thread delivered the waiting file.

Each manifest now stores deadline_at at initial acceptance (default created+4h) and persisted retry count (default20 attempts). A fresh process cannot renew this budget. Expired/exhausted deliveries make one bounded final receipt/terminal-error reconciliation and then quarantine without heartbeat or deleting unacknowledged media. An exact already-committed MP4 receipt still authorizes cleanup even at the deadline. An endlessly wrong receipt reaches quarantine after finite attempts. Deadline/retry count, HTTP500/unknown outcome and no false cleanup are separately tested.

Final result artifact includes25 passing cases; see exact manifest for hashes. The server must still be integrated and independently reviewed before node deployment.
