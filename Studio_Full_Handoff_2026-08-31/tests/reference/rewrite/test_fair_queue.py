import json
import threading
import time

from job_queue import Job, JobQueue, JobStatus, JobType, JobWorker


MODELS = {
    "openai": "gpt-4.1-nano",
    "claude": "claude-sonnet-4-6",
    "codex": "codex:gpt-5.6-terra",
}


def _add(
    queue: JobQueue,
    uid: str,
    label: str,
    *,
    provider: str = "openai",
    parallel: bool = False,
) -> str:
    return queue.add_job(
        Job(
            job_type=JobType.REWRITE.value,
            user_id=uid,
            provider=provider,
            model=MODELS[provider],
            parallel=parallel,
            rewrite_files=json.dumps([label]),
        )
    )


def _label(job: Job) -> str:
    return json.loads(job.rewrite_files)[0]


def _finish(queue: JobQueue, job: Job, status: str = "completed") -> None:
    job.status = status
    assert queue.finish_job(job, job.run_token)


def test_strict_round_robin_fifo_is_independent_per_provider(tmp_path):
    queue = JobQueue(str(tmp_path / "jobs.db"))
    for label in ("A1", "A2", "A3"):
        _add(queue, "A", label)
    for label in ("B1", "B2"):
        _add(queue, "B", label)
    _add(queue, "A", "CA1", provider="claude")
    _add(queue, "C", "CC1", provider="claude")

    openai = [queue.claim_next("openai") for _ in range(5)]
    claude = [queue.claim_next("claude") for _ in range(2)]

    assert [_label(job) for job in openai] == ["A1", "B1", "A2", "B2", "A3"]
    assert [_label(job) for job in claude] == ["CA1", "CC1"]


def test_round_robin_cursor_survives_reopen(tmp_path):
    path = tmp_path / "jobs.db"
    first_queue = JobQueue(str(path))
    _add(first_queue, "A", "A1")
    _add(first_queue, "B", "B1")
    first = first_queue.claim_next("openai")
    assert _label(first) == "A1"
    _finish(first_queue, first)
    first_queue.close()

    reopened = JobQueue(str(path))
    second = reopened.claim_next("openai")
    assert _label(second) == "B1"


def test_legacy_parallel_flag_is_ignored_by_scheduler(tmp_path):
    queue = JobQueue(str(tmp_path / "jobs.db"))
    first_id = _add(queue, "A", "first", parallel=False)
    second_id = _add(queue, "B", "second", parallel=True)

    assert [queue.claim_next("openai").id, queue.claim_next("openai").id] == [
        first_id,
        second_id,
    ]
    assert queue.mark_job_parallel(first_id) is False


def test_cancel_blocks_stale_progress_and_wins_completion_race(tmp_path):
    queue = JobQueue(str(tmp_path / "jobs.db"))
    job_id = _add(queue, "A", "race")
    claimed = queue.claim_next("openai")
    token = claimed.run_token

    result = queue.request_cancel(job_id)
    assert result.changed and result.status == "cancelling"
    claimed.progress_percent = 90
    assert queue.update_running(claimed) is False
    claimed.status = JobStatus.COMPLETED.value
    assert queue.finish_job(claimed, token) is True
    assert queue.get_job(job_id).status == JobStatus.CANCELLED.value
    assert queue.finish_job(claimed, token) is False


def test_persistent_cancel_prevents_executor_start(tmp_path):
    queue = JobQueue(str(tmp_path / "jobs.db"))
    job_id = _add(queue, "A", "cancel-before-provider")
    claimed = queue.claim_next("openai")
    assert queue.request_cancel(job_id).changed
    calls = []
    worker = JobWorker(queue, rewrite_executor=lambda _job: calls.append("called"))

    worker._execute_job(claimed)

    assert calls == []
    assert queue.get_job(job_id).status == JobStatus.CANCELLED.value


def test_full_retry_crash_recovery_keeps_manifest_and_is_retryable(tmp_path):
    queue = JobQueue(str(tmp_path / "jobs.db"))
    output = tmp_path / "result.txt"
    output.write_text("old", encoding="utf-8")
    job_id = _add(queue, "A", "source")
    running = queue.claim_next("openai")
    running.output_dir = str(tmp_path)
    running.output_files = json.dumps([output.name])
    _finish(queue, running)

    ticket = queue.begin_retry(job_id, full=True)
    assert ticket is not None
    resetting = queue.get_job(job_id)
    assert resetting.status == JobStatus.RESETTING.value
    assert json.loads(resetting.output_files) == []
    assert json.loads(resetting.reset_manifest) == [output.name]

    # Forced crash after CAS but before unlink.
    assert queue.reap_stale_running() == 1
    recovered = queue.get_job(job_id)
    assert recovered.status == JobStatus.FAILED.value
    assert output.exists()

    retry = queue.begin_retry(job_id, full=True)
    for filename in json.loads(retry.job.reset_manifest):
        (tmp_path / filename).unlink(missing_ok=True)
    assert queue.complete_retry(retry)
    pending = queue.get_job(job_id)
    assert pending.status == JobStatus.PENDING.value
    assert json.loads(pending.output_files) == []
    assert json.loads(pending.reset_manifest) == []
    assert not output.exists()


def test_delete_is_terminal_only_and_retry_noops_are_explicit(tmp_path):
    queue = JobQueue(str(tmp_path / "jobs.db"))
    job_id = _add(queue, "A", "active")
    assert queue.delete_terminal(job_id).code == "not_terminal"
    assert queue.retry_job(job_id) is False
    assert queue.request_cancel(job_id).changed
    assert queue.delete_terminal(job_id).changed
    assert queue.delete_terminal(job_id).code == "not_found"


def test_single_dispatcher_enforces_exact_global_and_provider_caps(tmp_path):
    queue = JobQueue(str(tmp_path / "jobs.db"))
    job_ids = []
    for provider in ("openai", "claude", "codex"):
        for index in range(2):
            job_ids.append(
                _add(queue, f"{provider}-{index}", f"{provider}-{index}", provider=provider)
            )

    release = threading.Event()
    state_lock = threading.Lock()
    active = {"total": 0, "openai": 0, "claude": 0, "codex": 0}
    maximum = dict(active)

    def executor(job):
        with state_lock:
            active["total"] += 1
            active[job.provider] += 1
            for key, value in active.items():
                maximum[key] = max(maximum[key], value)
        release.wait(3)
        with state_lock:
            active["total"] -= 1
            active[job.provider] -= 1

    worker = JobWorker(
        queue,
        generation_executor=executor,
        rewrite_executor=executor,
        max_concurrent_jobs=2,
        provider_caps={"openai": 1, "claude": 1, "codex": 1},
    )
    worker.start()
    deadline = time.time() + 3
    while time.time() < deadline:
        if worker.running_counts()["total"] == 2:
            break
        time.sleep(0.02)
    assert worker.running_counts()["total"] == 2
    time.sleep(0.15)
    assert maximum["total"] == 2
    assert maximum["openai"] <= 1
    assert maximum["claude"] <= 1
    assert maximum["codex"] <= 1

    release.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        if all(queue.get_job(job_id).status == "completed" for job_id in job_ids):
            break
        time.sleep(0.03)
    worker.stop()
    assert all(queue.get_job(job_id).status == "completed" for job_id in job_ids)
