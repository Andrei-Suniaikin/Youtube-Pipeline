# Render cycle 2: durable delivery and bounded execution

Fresh Minsk baseline (14 September 2026): `/opt/video_agent/render_puller.py` and `render_worker.py` copied to `render_candidate/`, hashes in `render_cycle2_baseline.json`. No node mutation. Service: `vsrender-puller.service`.

## Confirmed data-loss path

`render_puller.py:117` makes one MP4 POST, while `run_job:269–274` always deletes project files in finally. Heartbeat exists only between process start and process finish, leaving download and upload outside coverage. A Whisper process can keep the render lease alive forever without output. On the VPS, result writes final `render/{jid}.mp4` before checking claim ownership at commit; stale upload can overwrite or remove a newer result despite the existing X-Render-Claim header.

## Puller changes

1. Token-specific working directories: job ID plus hash of claim token. Never delete another claim's work. State manifests use private new files and atomic replace; never print claim tokens.
2. Before upload, atomically record a durable delivery manifest with job/claim, source path, SHA256, size, config fingerprint, transcript path and phase. Fsync completed files and manifest. An unacknowledged MP4 is not removed in finally.
3. Upload only after the manifest is durable. Retry transport failures, timeout and 5xx with bounded backoff. After an uncertain result, query receipt before retransmitting. Only an ACK matching job/claim/digest/size permits deletion. A same-claim duplicate server result must be idempotent.
4. A separate outbox loop resumes manifests after restart and continues heartbeat during delivery. Each manifest has a process lock so live upload and outbox replay cannot race. Stale/cancelled claims are quarantined with files retained for reconciliation, not relabelled successful.
5. One heartbeat/watchdog spans download, render and upload. Download/upload deadlines apply to wall time, not only socket inactivity. Render process group is killed at configurable total render deadline, Whisper stage deadline, or no-progress limit. Heartbeat by itself is not progress. Progress signals are subprocess log advance, output/temp file growth or advancing CPU work; thresholds must allow quiet but productive encoding.
6. Dedicated finite outbox budget/free-space admission pauses new claims when undelivered files threaten storage. A full outbox is visible as blocked delivery, not a deletion policy for unacknowledged media.

## Server contract requested from root

Already present: X-Puller-Token authentication and X-Render-Claim fencing. Extend:

- POST `/api/render/{jid}/result?status=done` includes `X-Render-SHA256` and exact content length. Stream to a unique staging filename, compute SHA256 and size, fsync; reject corrupt/empty data. Immediately before atomic publish, acquire DB write lock and re-check matching running claim. Stale uploads must never change/remove current final.
- Store accepted digest/size alongside queue job. Successful ACK: `{ok:true, accepted:true, job_id, status:'done', sha256, size}`. Same claim + accepted digest + size returns the same receipt without another chapters enqueue. Different claim/digest is409.
- GET `/api/render/{jid}/receipt` with authentication and claim header returns accepted matching receipt, matching live status, or409 stale/cancelled. Missing job404 is unknown, not permission to delete outbox.
- Error ACK should also be idempotent for the same claim/error identity so locally recorded execution failure can be replayed after a network outage.

## Verification boundary

Use real puller orchestration, filesystem manifests, HTTP requests against a local fault-injecting HTTP server, real subprocess lifecycle/process groups. Scenarios: HTTP response lost after committed result; upload dropped halfway; server5xx; restart between render completion/manifest/upload/ACK; two retries of one manifest; changed claim; corrupted size/hash; stale uploader vs current final; download >heartbeat interval; quiet/hung child; Whisper deadline; full outbox. Tiny local renderer/process fixtures cover orchestration, not production capacity. Cycle4 requires the real agreed90-minute/300–500-image profile on Minsk and exact deployed hashes.
