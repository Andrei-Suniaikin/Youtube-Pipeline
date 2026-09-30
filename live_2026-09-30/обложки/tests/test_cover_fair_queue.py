import json
import sqlite3
import threading

import pytest

from job_queue_cover import (
    Job,
    JobQueue,
    JobStatus,
    JobWorker,
    PendingJobLimitExceeded,
)


class _PendingFuture:
    def done(self):
        return False


class _RecordingPool:
    def __init__(self):
        self.jobs = []

    def submit(self, _fn, job):
        self.jobs.append(job)
        return _PendingFuture()


def _queue_with_jobs(tmp_path, users, providers=None):
    queue = JobQueue(str(tmp_path / "jobs.db"))
    for index, user_id in enumerate(users):
        provider = providers[index] if providers else "flow"
        queue.add_job(
            Job(
                user_id=user_id,
                urls=json.dumps([f"https://example.test/{index}"]),
                parallel=bool(index % 2),
                generation_provider=provider,
                preset_data=json.dumps({"generation_provider": provider}),
            )
        )
    return queue


def _claim_order(queue, slots, provider_limits=None):
    worker = JobWorker(
        queue,
        executor=lambda _job: None,
        max_concurrent_jobs=slots,
        provider_limits=provider_limits,
    )
    pool = _RecordingPool()
    worker._pool = pool
    assert worker._dispatch_available() == slots
    return worker, pool.jobs


def test_three_users_round_robin_three_slots(tmp_path):
    queue = _queue_with_jobs(tmp_path, ["A", "A", "A", "B", "B", "C"])
    _worker, jobs = _claim_order(queue, 3)
    assert [job.user_id for job in jobs] == ["A", "B", "C"]


def test_four_slots_wrap_to_first_user(tmp_path):
    queue = _queue_with_jobs(tmp_path, ["A", "A", "A", "B", "B", "C"])
    _worker, jobs = _claim_order(queue, 4)
    assert [job.user_id for job in jobs] == ["A", "B", "C", "A"]


def test_cursor_survives_last_user_disappearing(tmp_path):
    queue = _queue_with_jobs(tmp_path, ["A"] * 10 + ["B", "C"])
    _worker, jobs = _claim_order(queue, 3)
    assert [job.user_id for job in jobs] == ["A", "B", "C"]


def test_one_user_fills_all_slots_and_parallel_is_ignored(tmp_path):
    queue = _queue_with_jobs(tmp_path, ["A", "A", "A", "A"])
    _worker, jobs = _claim_order(queue, 4)
    assert [job.user_id for job in jobs] == ["A", "A", "A", "A"]
    assert {job.parallel for job in jobs} == {False, True}


def test_new_user_gets_next_released_slot(tmp_path):
    queue = _queue_with_jobs(tmp_path, ["A", "A", "A", "A"])
    worker, jobs = _claim_order(queue, 2)
    assert [job.user_id for job in jobs] == ["A", "A"]

    queue.add_job(Job(user_id="B"))
    # Имитируем освобождение одного worker slot. Уже запущенная A-job не прерывается.
    with worker._running_lock:
        worker._running.pop(next(iter(worker._running)))
    assert worker._dispatch_available() == 1
    assert worker._pool.jobs[-1].user_id == "B"


def test_saturated_provider_does_not_consume_other_provider_slots(tmp_path):
    queue = _queue_with_jobs(
        tmp_path,
        ["A", "A", "A", "B", "C"],
        ["veo", "veo", "veo", "flow", "gpt_image2"],
    )
    _worker, jobs = _claim_order(
        queue,
        3,
        provider_limits={"veo": 1, "fastgen": 1, "gpt_image2": 1},
    )
    assert [job.generation_provider for job in jobs] == [
        "veo",
        "flow",
        "gpt_image2",
    ]
    assert [job.user_id for job in jobs] == ["A", "B", "C"]


def test_fifo_inside_user(tmp_path):
    queue = _queue_with_jobs(tmp_path, ["A", "A", "B"])
    first_a = queue.claim_next_for_user("A")
    second_a = queue.claim_next_for_user("A")
    assert first_a.created_at <= second_a.created_at
    assert json.loads(first_a.urls)[0].endswith("/0")
    assert json.loads(second_a.urls)[0].endswith("/1")


def test_concurrent_claim_never_duplicates_job(tmp_path):
    db_path = str(tmp_path / "jobs.db")
    q1 = JobQueue(db_path)
    q2 = JobQueue(db_path)
    job_id = q1.add_job(Job(user_id="A"))
    barrier = threading.Barrier(2)
    results = []

    def claim(queue):
        barrier.wait()
        job = queue.claim_next_for_user("A")
        results.append(job.id if job else None)

    threads = [threading.Thread(target=claim, args=(queue,)) for queue in (q1, q2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert results.count(job_id) == 1
    assert results.count(None) == 1


def test_running_cancel_persists_and_restart_does_not_requeue(tmp_path):
    db_path = str(tmp_path / "jobs.db")
    queue = JobQueue(db_path)
    job_id = queue.add_job(Job(user_id="A"))
    running = queue.claim_next_for_user("A")
    assert running and running.status == JobStatus.RUNNING.value
    assert queue.cancel_job(job_id)

    restarted = JobQueue(db_path)
    recovery = restarted.recover_interrupted_jobs()
    stored = restarted.get_job(job_id)
    assert recovery == {"requeued": 0, "cancelled": 1}
    assert stored.status == JobStatus.CANCELLED.value
    assert stored.cancel_requested is True
    assert restarted.claim_next_for_user("A") is None


def test_stale_worker_cannot_overwrite_persistent_cancel(tmp_path):
    queue = _queue_with_jobs(tmp_path, ["A"])
    running = queue.claim_next_for_user("A")
    assert queue.cancel_job(running.id)
    running.status = JobStatus.COMPLETED.value
    running.finished_at = 123.0
    assert queue.update_job(
        running,
        expected_status=JobStatus.RUNNING.value,
    )
    stored = queue.get_job(running.id)
    assert stored.status == JobStatus.CANCELLED.value
    assert stored.cancel_requested is True


def test_legacy_schema_is_backfilled_and_secrets_are_scrubbed(tmp_path):
    db_path = tmp_path / "legacy.db"
    marker = "legacy-migration-secret-9f4f8b12"
    payload = {
        "id": "legacy-job",
        "status": "pending",
        "created_at": 1.0,
        "user_id": "partner-7",
        "preset_data": json.dumps({"generation_provider": "veo"}),
        "openai_api_key": marker,
        "fast_gen_api_key": marker,
        "veo_api_key": marker,
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA secure_delete=OFF")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO jobs(id, data, status, created_at) VALUES (?, ?, ?, ?)",
            ("legacy-job", json.dumps(payload), "pending", 1.0),
        )

    active_paths = [
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ]
    marker_bytes = marker.encode()
    before_hits = sum(
        path.read_bytes().count(marker_bytes)
        for path in active_paths
        if path.exists()
    )
    assert before_hits > 0

    queue = JobQueue(str(db_path))
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT data, user_id, provider, cancel_requested
            FROM jobs WHERE id='legacy-job'
            """
        ).fetchone()
        columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}

    stored = json.loads(row[0])
    assert "user_id" in columns
    assert row[1] == "partner-7"
    assert row[2] == "veo"
    assert row[3] == 0
    assert {"provider", "cancel_requested"} <= columns
    assert all(name not in stored for name in (
        "openai_api_key", "fast_gen_api_key", "veo_api_key"
    ))
    assert queue.get_job("legacy-job").user_id == "partner-7"
    assert queue.get_job("legacy-job").generation_provider == "veo"
    after_hits = sum(
        path.read_bytes().count(marker_bytes)
        for path in active_paths
        if path.exists()
    )
    assert after_hits == 0
    wal_path = db_path.with_name(db_path.name + "-wal")
    assert not wal_path.exists() or wal_path.stat().st_size == 0


def test_retry_and_recovery_return_to_same_fair_queue(tmp_path):
    queue = _queue_with_jobs(tmp_path, ["A", "B"])
    failed = queue.claim_next_for_user("A")
    failed.status = JobStatus.FAILED.value
    queue.update_job(failed)
    failed.status = JobStatus.PENDING.value
    failed.started_at = 0
    queue.update_job(failed)

    worker, jobs = _claim_order(queue, 2)
    assert [job.user_id for job in jobs] == ["A", "B"]
    assert worker._last_user_id == "B"


def test_reclaimed_run_rejects_old_worker_token(tmp_path):
    queue = _queue_with_jobs(tmp_path, ["A"])
    old_run = queue.claim_next_for_user("A")
    old_token = old_run.run_token
    assert old_token

    assert queue.recover_interrupted_jobs() == {"requeued": 1, "cancelled": 0}
    recovered = queue.get_job(old_run.id)
    assert recovered.status == JobStatus.PENDING.value
    assert recovered.run_token == ""

    new_run = queue.claim_next_for_user("A")
    assert new_run.run_token
    assert new_run.run_token != old_token

    old_run.status = JobStatus.COMPLETED.value
    old_run.progress_percent = 100
    assert not queue.update_job(
        old_run,
        expected_status=JobStatus.RUNNING.value,
    )
    stored = queue.get_job(old_run.id)
    assert stored.status == JobStatus.RUNNING.value
    assert stored.run_token == new_run.run_token
    assert stored.progress_percent != 100


def test_resetting_recovery_clears_maintenance_lease(tmp_path):
    queue = _queue_with_jobs(tmp_path, ["A"])
    terminal = queue.claim_next_for_user("A")
    terminal.status = JobStatus.COMPLETED_PARTIAL.value
    assert queue.update_job(
        terminal,
        expected_status=JobStatus.RUNNING.value,
    )

    previous_token = terminal.run_token
    terminal.status = JobStatus.RESETTING.value
    terminal.run_token = "reset-lease"
    assert queue.update_job(
        terminal,
        expected_status=JobStatus.COMPLETED_PARTIAL.value,
        expected_run_token=previous_token,
    )
    assert queue.recover_interrupted_jobs() == {"requeued": 1, "cancelled": 0}
    recovered = queue.get_job(terminal.id)
    assert recovered.status == JobStatus.PENDING.value
    assert recovered.run_token == ""

    terminal.progress_percent = 99
    assert not queue.update_job(
        terminal,
        expected_status=JobStatus.RESETTING.value,
        expected_run_token="reset-lease",
    )


def test_pending_cap_count_and_insert_are_one_transaction(tmp_path):
    db_path = str(tmp_path / "jobs.db")
    q1 = JobQueue(db_path)
    q2 = JobQueue(db_path)
    for _ in range(99):
        q1.add_job(Job(user_id="A"), max_pending_for_user=100)

    barrier = threading.Barrier(2)
    accepted = []
    rejected = []

    def enqueue(queue):
        barrier.wait()
        try:
            accepted.append(
                queue.add_job(Job(user_id="A"), max_pending_for_user=100)
            )
        except PendingJobLimitExceeded as exc:
            rejected.append((exc.pending, exc.limit))

    threads = [
        threading.Thread(target=enqueue, args=(queue,))
        for queue in (q1, q2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(accepted) == 1
    assert rejected == [(100, 100)]
    assert q1.count_pending_for_user("A") == 100


def test_provider_groups_have_independent_rr_cursors(tmp_path):
    queue = _queue_with_jobs(
        tmp_path,
        ["A", "A", "B", "B", "C"],
        ["flow", "veo", "flow", "veo", "gpt_image2"],
    )
    _worker, jobs = _claim_order(
        queue,
        5,
        provider_limits={"fastgen": 5, "veo": 5, "gpt_image2": 5},
    )
    assert [
        (job.generation_provider, job.user_id)
        for job in jobs
    ] == [
        ("flow", "A"),
        ("veo", "A"),
        ("gpt_image2", "C"),
        ("flow", "B"),
        ("veo", "B"),
    ]


def test_pending_and_resetting_share_one_reserved_cap(tmp_path):
    queue = JobQueue(str(tmp_path / "jobs.db"))
    terminal_id = queue.add_job(Job(user_id="A"))
    assert queue.cancel_job(terminal_id)
    for _ in range(99):
        queue.add_job(Job(user_id="A"), max_pending_for_user=100)

    terminal = queue.get_job(terminal_id)
    old_token = terminal.run_token
    terminal.status = JobStatus.RESETTING.value
    terminal.run_token = "reset-token"
    assert queue.update_job(
        terminal,
        expected_status=JobStatus.CANCELLED.value,
        expected_run_token=old_token,
        reset_cancel_requested=True,
        max_pending_for_user=100,
    )
    assert queue.count_pending_for_user("A") == 99
    assert queue.count_reserved_for_user("A") == 100
    with pytest.raises(PendingJobLimitExceeded):
        queue.add_job(Job(user_id="A"), max_pending_for_user=100)

    terminal.status = JobStatus.PENDING.value
    terminal.run_token = ""
    assert queue.update_job(
        terminal,
        expected_status=JobStatus.RESETTING.value,
        expected_run_token="reset-token",
        reset_cancel_requested=True,
        max_pending_for_user=100,
    )
    assert queue.count_pending_for_user("A") == 100
    assert queue.count_reserved_for_user("A") == 100


def test_full_cap_rejects_terminal_to_pending_without_state_change(tmp_path):
    queue = JobQueue(str(tmp_path / "jobs.db"))
    terminal_id = queue.add_job(Job(user_id="A"))
    assert queue.cancel_job(terminal_id)
    for _ in range(100):
        queue.add_job(Job(user_id="A"))

    terminal = queue.get_job(terminal_id)
    terminal.status = JobStatus.PENDING.value
    terminal.cancel_requested = False
    with pytest.raises(PendingJobLimitExceeded) as exc:
        queue.update_job(
            terminal,
            expected_status=JobStatus.CANCELLED.value,
            reset_cancel_requested=True,
            max_pending_for_user=100,
        )
    assert (exc.value.pending, exc.value.limit) == (100, 100)
    assert queue.get_job(terminal_id).status == JobStatus.CANCELLED.value
    assert queue.count_reserved_for_user("A") == 100


def test_concurrent_add_and_retry_at_99_accept_exactly_one(tmp_path):
    db_path = str(tmp_path / "jobs.db")
    q1 = JobQueue(db_path)
    q2 = JobQueue(db_path)
    terminal_id = q1.add_job(Job(user_id="A"))
    assert q1.cancel_job(terminal_id)
    for _ in range(99):
        q1.add_job(Job(user_id="A"), max_pending_for_user=100)

    barrier = threading.Barrier(2)
    accepted = []
    rejected = []

    def add():
        barrier.wait()
        try:
            accepted.append(
                ("add", q1.add_job(
                    Job(user_id="A"),
                    max_pending_for_user=100,
                ))
            )
        except PendingJobLimitExceeded:
            rejected.append("add")

    def retry():
        job = q2.get_job(terminal_id)
        job.status = JobStatus.PENDING.value
        job.cancel_requested = False
        barrier.wait()
        try:
            ok = q2.update_job(
                job,
                expected_status=JobStatus.CANCELLED.value,
                reset_cancel_requested=True,
                max_pending_for_user=100,
            )
            accepted.append(("retry", ok))
        except PendingJobLimitExceeded:
            rejected.append("retry")

    threads = [threading.Thread(target=add), threading.Thread(target=retry)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert accepted[0][1]
    assert q1.count_reserved_for_user("A") == 100


def test_recovery_never_turns_overflow_resetting_into_101st_pending(tmp_path):
    queue = JobQueue(str(tmp_path / "jobs.db"))
    resetting_id = queue.add_job(Job(user_id="A"))
    assert queue.cancel_job(resetting_id)
    resetting = queue.get_job(resetting_id)
    resetting.status = JobStatus.RESETTING.value
    resetting.run_token = "legacy-reset"
    assert queue.update_job(
        resetting,
        expected_status=JobStatus.CANCELLED.value,
        expected_run_token="",
        reset_cancel_requested=True,
    )
    for _ in range(100):
        queue.add_job(Job(user_id="A"))
    assert queue.count_reserved_for_user("A") == 101

    recovery = queue.recover_interrupted_jobs(max_pending_for_user=100)
    assert recovery["queue_full_failed"] == 1
    stored = queue.get_job(resetting_id)
    assert stored.status == JobStatus.FAILED.value
    assert stored.run_token == ""
    assert queue.count_pending_for_user("A") == 100
    assert queue.count_reserved_for_user("A") == 100
