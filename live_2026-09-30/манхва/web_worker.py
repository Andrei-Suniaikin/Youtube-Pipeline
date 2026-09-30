"""One checkpointed worker per queue root, including across web processes.

A running job is held by its job lock (web_queue.JobLock) for the whole run and marked alive by a
heartbeat thread. If the heartbeat learns the job is no longer ours, the worker stops at the next
boundary it controls (a step state write or a call inside a step) and changes nothing more.
"""
from __future__ import annotations

import fcntl
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

import channels as CH
import claude_pool as CP
import cleanup as CL
import fastgen_pool as FP
import lanes as LN
import denoise as DN
import publish as PB
import cover as CV
import source as SRC
import watch as WT
from web_queue import (HEARTBEAT_SECONDS, LEASE_SECONDS, QueueDatabaseError, QueueStore, STEPS, _safe_error, archived_refusal,
                       lock_problem, new_owner, queue_problem, read_json)

log = logging.getLogger("manga.worker")
# Сколько упавший шаг ждёт флага остановки службы, прежде чем записать ошибку ролика.
# Разбор 21.09.2026: 65 перезапусков за двое суток, и при каждом systemd убивал CLI моделей,
# их пустой ответ записывался ошибкой ролика раньше, чем поднимался флаг (0,1–0,8 с по журналу).
STOP_GRACE_SECONDS = 3.0

# MANGA_JUDGE=off: судья, пересдача и исправление героев не вызываются (им нужен Codex с картинками).
JUDGE = os.environ.get("MANGA_JUDGE", "on").strip().lower()
if JUDGE not in ("on", "off"):
    raise ValueError("MANGA_JUDGE: on или off")
SRC.max_chars()  # неверный MANGA_SOURCE_MAX_CHARS останавливает запуск службы, а не ролик
SRC.proxies(), SRC.rest_minutes()  # то же для MANGA_SUBS_PROXIES / MANGA_SUBS_REST
CV.enabled()     # то же для MANGA_COVER и MANGA_COVER_VISION: ошибка настройки видна при старте
CV.defer_enabled()
CV.vision_model()
CV.judge_model()       # и выбирающего обложку (MANGA_COVER_JUDGE, MANGA_COVER_JUDGE_FALLBACK, чистка)
CV.judge_fallback()
CV.judge_effort()
CV.clean_mode()


class OwnershipLost(CP.JobGone):
    """The job was taken back from this worker; nothing more may be written for it."""


class _StopWhileClaudeWaits(CP.JobGone):
    """The service stops while a step waits for the Claude limit: the step goes back to «ждёт», the job to the queue."""


class _Paused(Exception):
    """The service stops while a video waits for its lane: the job goes back to the queue as it is."""


def _still_drawing(error) -> bool:
    """Ошибка кадра значит «генератор ещё рисует / номер не получен» (images.PendingImageError), а не отказ."""
    text = str(error or "")
    return any(m in text for m in ("ещё не завершилась", "номер не получен", "Повторная покупка остановлена",
                                   "продолжение скачает"))


def where_it_broke(exc: BaseException) -> str:
    """«текст ошибки (app/sheets.py:219)» — последнее место В НАШЕМ коде.

    20.09 ночью ролик падал с голым «'dict' object has no attribute 'strip'»: ни шага,
    ни файла, ни строки. Пришлось гонять шаги вручную, чтобы найти место, и всё равно
    не нашлось. Одна строка места экономит час поисков.
    """
    import traceback
    text = str(exc) or type(exc).__name__
    try:
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if "/app/" in f.filename.replace("\\", "/")]
        if not frames:
            return text
        last = frames[-1]
        name = last.filename.replace("\\", "/").rsplit("/app/", 1)[-1]
        return f"{text} ({name}:{last.lineno})"
    except Exception:       # место — удобство, а не обязанность
        return text


class Worker:
    def __init__(self, store: QueueStore, project_factory=None, poll_seconds=0.5,
                 heartbeat_seconds=HEARTBEAT_SECONDS, lease_seconds=LEASE_SECONDS, cleanup=None, lanes=None,
                 disk_free=None):
        self.store = store
        self.project_factory = project_factory
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_seconds = lease_seconds
        self.owner = new_owner()
        # Invalid cleanup settings stop the service start instead of deleting by a guess.
        self.cleanup = CL.settings() if cleanup is None else cleanup
        # Cleanup after delivery and the disk guard (MANGA_CLEANUP_DELIVERED_HOURS, MANGA_CLEANUP_DONE_DAYS,
        # MANGA_DISK_MIN_GB); an explicit cleanup dict (tests, tools) carries them itself or leaves them off.
        self.delivery = CL.delivery_settings() if cleanup is None else {
            key: cleanup.get(key) for key in ("done_days", "delivered_hours", "disk_min_gb")}
        # MANGA_DISK_MIN_GB: below this much free space no new video is started (the ones in work go on).
        self.disk_min_gb = self.delivery["disk_min_gb"]
        self._disk_free = disk_free or (lambda: shutil.disk_usage(self.store.root).free)
        self.disk_hold = None   # why new videos wait for disk space; read by the pages and /health
        self._next_sweep = 0.0
        self._stop = threading.Event()
        # Invalid lane settings stop the service start, as invalid cleanup settings do (lanes.settings).
        lanes = LN.settings() if lanes is None else lanes
        # Invalid FastGen budget settings (MANGA_FASTGEN_*) stop the service start too; images.py reads them
        # only when the first video imports it, and then every queued video would fail one by one.
        FP.settings()
        # Автоповтор временных сбоев и Telegram (watch.py); без настроек оба выключены, как в 623ae20.
        self.retry_pause = WT.retry_seconds()
        self.notifier = WT.Notifier()
        # Неверные MANGA_TITLE / MANGA_TITLE_MODEL тоже останавливают запуск, а не каждый ролик.
        PB.enabled()
        PB.model()
        # Неверные MANGA_CLEAN* (denoise.py) — тоже при запуске: esrgan без адреса машины и пути к программе.
        DN.settings()
        self.parallel = lanes["parallel"]
        self.lanes = LN.Lanes(lanes["slots"])
        self._lost = threading.Event()   # one video at a time (MANGA_PARALLEL_VIDEOS=1): this worker lost its job
        self._job_lost = {}    # parallel mode: job id -> Event, one per running video
        self.waits = {}        # project id -> why the video waits (a lane), for the project page
        self._in_lane = {}     # project id -> lane of the step now running
        self._runs = {}        # job id -> thread (parallel mode)
        self._done = []        # (job, ran) of finished threads, tidied by the dispatcher
        self._broken = []      # jobs whose run broke on the queue database, given back by the dispatcher
        self._runs_lock = threading.Lock()
        self._thread = None
        self._lock = None
        self._last_sweep = 0.0
        self._hold_note = None
        # Why the queue loop is paused (queue files or database unusable); read by /health and the project page.
        self.problem = None
        self._interrupted = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="manga-queue-worker", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=5):
        # A provider request cannot be cancelled safely after acceptance. Finish the
        # current stage and keep the flock until then; restart resumes its checkpoint.
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _acquire(self):
        lock = (self.store.root / "worker.lock").open("a+")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return False
        self._lock = lock
        self.store.recover(self.lease_seconds)
        self._last_sweep = time.monotonic()
        return True

    def _loop(self):
        if self.parallel > 1:
            return self._loop_parallel()
        try:
            while not self._stop.is_set():
                try:
                    if self._lock is None and not self._acquire():
                        self._stop.wait(self.poll_seconds)
                        continue
                    if self._interrupted:
                        # Its run broke on the queue itself and the job is still ours in the database: back to the
                        # queue as it is; done steps stay done, the interrupted one starts again.
                        self.store.unclaim(self._interrupted, "")
                        self.store.recover(self.lease_seconds)
                        self._interrupted = None
                    job = self.store.claim(self.owner, new_videos=self._disk_allows_new())
                    self._interrupted = job
                    ran = bool(job) and self.run_job(job)
                    self._interrupted = None
                    if not ran and time.monotonic() - self._last_sweep >= self.heartbeat_seconds:
                        # A job left by a dead owner becomes free only after its lease; look again periodically.
                        self.store.recover(self.lease_seconds)
                        self._last_sweep = time.monotonic()
                    self._queue_works()
                except (OSError, sqlite3.OperationalError) as exc:
                    # Full disk, no descriptors, no rights, database unavailable: the cause is the service, not
                    # a video. Nothing is closed as an error; the loop waits one pass and tries again.
                    note = queue_problem(exc, self.store.root)
                    if note != self.problem:
                        log.error("%s", note, exc_info=exc)
                        self.problem = note
                    self._stop.wait(self.heartbeat_seconds)
                    continue
                if ran:
                    # Cleanup only for a video this worker really ran; a job returned untouched is not tidied.
                    self.tidy([job["project_id"]])
                # The periodic pass must not wait for an empty queue: at full load it never empties.
                if time.time() >= self._next_sweep:
                    self._next_sweep = time.time() + CL.SWEEP_EVERY
                    self.tidy(None)
                if ran:
                    continue
                # A job lock could not be taken: the cause is the folder, rights, disk or file limit, never one
                # job, so dispensing pauses until the next pass instead of failing the queue job after job.
                self._stop.wait(self.heartbeat_seconds if job else self.poll_seconds)
        finally:
            if self._lock:
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
                self._lock.close()
                self._lock = None

    def _loop_parallel(self):
        """MANGA_PARALLEL_VIDEOS > 1: this thread hands out jobs and tidies; each video runs in its own thread.
        Claims and cleanup happen only here, so a video never starts while its folder is being tidied."""
        hold_until = 0.0
        try:
            while not self._stop.is_set():
                started = False
                try:
                    if self._lock is None and not self._acquire():
                        self._stop.wait(self.poll_seconds)
                        continue
                    with self._runs_lock:
                        broken = list(self._broken)
                    for job in broken:
                        # Its run broke on the queue itself: back to the queue as it is (as in the one-video loop).
                        # Removed from the list only once given back: a still broken database keeps it for the next pass.
                        self.store.unclaim(job, "")
                        with self._runs_lock:
                            self._broken.remove(job)
                    if broken:
                        self.store.recover(self.lease_seconds)
                    finished = self._reap()
                    if any(not ran for _, ran in finished):
                        # A job lock could not be taken (or the run broke): the cause is the folder, rights, disk or
                        # file limit, never one job, so dispensing pauses a pass, as in the one-video loop.
                        hold_until = time.monotonic() + self.heartbeat_seconds
                    # +1: задача «Название — сделать заново» идёт сверх лимита роликов (claim следит за обоими).
                    if len(self._runs) < self.parallel + 1 and time.monotonic() >= hold_until:
                        job = self.store.claim(self.owner, limit=self.parallel, new_videos=self._disk_allows_new())
                        if job:
                            thread = threading.Thread(target=self._run_thread, args=(job,), daemon=True,
                                                      name=f"manga-video-{job['project_id'][:8]}")
                            with self._runs_lock:
                                self._runs[job["id"]] = thread
                            thread.start()
                            started = True
                    if not started and time.monotonic() - self._last_sweep >= self.heartbeat_seconds:
                        self.store.recover(self.lease_seconds)
                        self._last_sweep = time.monotonic()
                    if not broken:
                        self._queue_works()
                except (OSError, sqlite3.OperationalError) as exc:
                    note = queue_problem(exc, self.store.root)
                    if note != self.problem:
                        log.error("%s", note, exc_info=exc)
                        self.problem = note
                    self._stop.wait(self.heartbeat_seconds)
                    continue
                for job, ran in finished:
                    if ran:
                        self.tidy([job["project_id"]])
                if time.time() >= self._next_sweep:
                    self._next_sweep = time.time() + CL.SWEEP_EVERY
                    self.tidy(None)
                if not started:
                    self._stop.wait(self.poll_seconds)
        finally:
            # Keep the queue flock until every video thread has stopped at its boundary.
            for thread in list(self._runs.values()):
                thread.join()
            self._reap()
            if self._lock:
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
                self._lock.close()
                self._lock = None

    def _reap(self):
        with self._runs_lock:
            finished, self._done = self._done, []
            for job, _ in finished:
                self._runs.pop(job["id"], None)
        return finished

    def _run_thread(self, job):
        ran = False
        try:
            ran = self.run_job(job)
        except (OSError, sqlite3.OperationalError) as exc:
            # The queue database failed, not the video: the dispatcher pauses and gives the job back.
            note = queue_problem(exc, self.store.root)
            if note != self.problem:
                log.error("%s", note, exc_info=exc)
                self.problem = note
            with self._runs_lock:
                self._broken.append(job)
        except Exception:  # noqa: BLE001 — one video's thread must never take the queue down
            log.exception("Ролик %s: поток обработки упал", job["project_id"])
        finally:
            with self._runs_lock:
                self._done.append((job, ran))

    def _queue_works(self):
        if self.problem and self.parallel > 1:
            with self._runs_lock:
                if self._broken:
                    return   # a video's run just broke on the queue; the dispatcher gives it back first
        if self.problem:
            log.warning("Очередь снова работает, пауза снята. Была причина: %s", self.problem)
            self.problem = None

    def _disk_allows_new(self):
        """False while free space is below MANGA_DISK_MIN_GB; the reason stays in disk_hold for the pages."""
        if self.disk_min_gb is None:
            return True
        free = self._disk_free()
        if free >= self.disk_min_gb * 1024 ** 3:
            if self.disk_hold:
                log.warning("Места на диске снова хватает (свободно %.0f ГБ): новые ролики берутся", free / 1024 ** 3)
                self.disk_hold = None
            return True
        note = (f"Новые ролики не начинаются: на диске свободно {free / 1024 ** 3:.0f} ГБ, "
                f"а нужно не меньше {self.disk_min_gb} ГБ (настройка MANGA_DISK_MIN_GB). Начатые ролики и пересдачи "
                "кадров доделываются. Освободите место"
                + (" (скачайте готовые ролики — их кадры уберутся после выдачи)"
                   if self.delivery.get("delivered_hours") is not None else "")
                + " — очередь продолжит сама.")
        if not self.disk_hold:
            log.error("%s", note)   # once per pause, not every half second
        self.disk_hold = note
        return False

    def tidy(self, project_ids):
        """Cleanup runs only in the queue loop thread, the only one that claims jobs: no video starts while
        its folder is tidied. With parallel videos others may run meanwhile; cleanup skips every video
        that has a job in the queue or in work (cleanup._blocker/_active)."""
        try:
            CL.run_due(self.store, {**self.cleanup, **self.delivery}, project_ids=project_ids, stop=self._stop.is_set)
        except Exception as exc:
            print(f"уборка остановлена с ошибкой: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    def run_job(self, job):
        """Run a claimed job holding its job lock while a side thread keeps its heartbeat fresh.
        False: the lock could not be taken; the job went back to the queue untouched, with the reason."""
        try:
            held = self.store.hold_job(job)
        except OSError as exc:
            note = lock_problem(exc, self.store.job_lock_path(job).parent) + (
                " Ролик не начат и ждёт в очереди, готовые этапы сохранены."
                " Студия повторяет попытку сама и продолжит после устранения причины.")
            if not self.store.unclaim(job, note):
                log.warning("Задача %s уже не у этого исполнителя; причину паузы к ней не записал", job["id"])
            if note != self._hold_note:
                log.error("Ролик %s (проект %s): %s", job["id"], job["project_id"], note)
                self._hold_note = note
            return False
        if self._hold_note:
            log.warning("Замок ролика %s создан: очередь снова выдаёт ролики", job["id"])
            self._hold_note = None
        done = threading.Event()
        if self.parallel > 1:
            self._job_lost[job["id"]] = threading.Event()
        else:
            self._lost.clear()
        beat = threading.Thread(target=self._beat, args=(job, done), name="manga-queue-heartbeat", daemon=True)
        beat.start()
        try:
            self._run_job(job)
        finally:
            done.set()
            beat.join()
            self._job_lost.pop(job["id"], None)
            self.waits.pop(job["project_id"], None)
            try:
                held.release()
            except OSError as exc:
                # The lock itself is free (its descriptor is closed). The empty file under this run's name is
                # read only while the job still runs with this owner, and then it is right: the run is over.
                log.warning("Ролик %s: замок снят, но его файл не удалён: %s", job["id"], exc)
        return True

    def _beat(self, job, done):
        while not done.wait(self.heartbeat_seconds):
            try:
                alive = self.store.heartbeat(job["id"], job["owner"])
            except Exception as exc:  # a dead heartbeat thread would let the lease expire under a live job
                # The next beat retries; the lease spans many beats.
                log.warning("Не удалось отметить задачу %s как живую, повторю через %s с: %s",
                            job["id"], self.heartbeat_seconds, exc)
                continue
            if not alive:
                if not done.is_set():
                    self._lost_event(job).set()
                    log.warning("Задача %s больше не принадлежит этому исполнителю; отметки прекращены", job["id"])
                return

    def _check(self, job):
        """Called between paid calls inside one step: a lost job gets no further call."""
        if self._lost_event(job).is_set():
            raise OwnershipLost(job["id"])

    def _lost_event(self, job):
        return self._job_lost.get(job["id"], self._lost) if self.parallel > 1 else self._lost

    def _step(self, job, key, status, error="", note=""):
        self._check(job)
        if not self.store.step(job["project_id"], key, status, error, note, job=job):
            raise OwnershipLost(job["id"])
        self._queue_works()  # a long run must not show an old pause while its progress is being written

    def _finish(self, job, error="", paused=False, then_retry=False):
        if not self.store.finish(job["id"], error, paused=paused, owner=job["owner"], then_retry=then_retry):
            log.warning("Задача %s уже не у этого исполнителя; итог не записан", job["id"])
            return False
        return True

    def _complete(self, job, then_retry=False):
        """Ролик прошёл все шаги: итог в очередь и, если настроено, «ролик готов» в Telegram.
        then_retry — обложка отложена: её retry ставится в той же записи, что и итог."""
        if self._finish(job, then_retry=then_retry):
            self._tell(job, None, "")

    def _failed(self, job, exc, step):
        """Шаг упал. Временный сбой (сеть, 5xx, таймаут, прокси) — один автоповтор через паузу, если он включён
        и эта задача сама не автоповтор; иначе ошибка остаётся на странице и уходит в Telegram."""
        error = where_it_broke(exc)
        if (self.retry_pause and job["action"] in ("run", "retry") and not job.get("auto_reason")
                and WT.classify(exc) == "temporary"):
            label = dict(STEPS).get(step, "")
            reason = (f"«{label}»: " if label else "") + error.split("\n", 1)[0]
            if self.store.fail_and_retry(job, error, self.retry_pause, reason):
                log.warning("Ролик %s: временный сбой на этапе «%s», повторю сам один раз через %s мин: %s",
                            job["project_id"], label, round(self.retry_pause / 60), reason[:200])
            else:
                log.warning("Задача %s уже не у этого исполнителя; итог не записан", job["id"])
            return
        if self._finish(job, error):
            self._tell(job, step, error)

    def _tell(self, job, step, error):
        if not self.notifier.enabled or job["action"] == "frame_retry":
            return
        try:
            project = self.store.get_project(job["project_id"])
            if error:
                self.notifier.failed(project, dict(STEPS).get(step, ""), project.get("error") or error)
            elif project["status"] == "complete":
                self.notifier.ready(project)
            else:
                self.notifier.failed(project, "", project.get("error") or "готовый ролик не найден")
        except Exception as exc:  # noqa: BLE001 — уведомление вторично, ролик уже закрыт
            log.warning("Ролик %s: сообщение в Telegram не собрано: %s", job["project_id"], WT.mask(exc))

    def _run_job(self, job):
        # Расход Claude пишется в журнал ролика с именем текущего шага; ожидание лимита видно на странице.
        mark = CP.use_scope(None, check=lambda: self._check(job),
                            on_wait=lambda step, msg: self._step(job, step, "running", note=msg) if step in dict(STEPS) else None,
                            stop=lambda: self._stop_claude_wait(job))
        try:
            self._run_job_steps(job)
        finally:
            CP.reset_scope(mark)

    def _stop_claude_wait(self, job):
        if self._stop.is_set():
            raise _StopWhileClaudeWaits(job["id"])

    def _run_job_steps(self, job):
        pid, current = job["project_id"], None
        side = None   # шаг «Название», идущий рядом с цепочкой ролика (_title_beside)
        held = ExitStack()   # the lane of the step now running (lanes.py); freed when the step ends in any way
        cut = threading.Event()   # a frame waiting for FastGen room was cancelled (stop or lost job): nothing bought

        def cancelled():
            if self._stop.is_set() or self._lost_event(job).is_set():
                cut.set()
                return True
            return False

        FP.bind(pid, cancelled)   # frames of this video: their FastGen wait is shown per video and can be cut
        try:
            archive = self.store.archive_info(pid)
            if archive and job["action"] != "title":   # «Название» берёт только тексты — их уборка не трогает
                # enqueue refuses such jobs; this guards a job queued by other means: images are never bought again.
                raise RuntimeError(archived_refusal(archive))
            data = self.store._data(pid)
            root = self.store.project_path(pid)
            CP.current_scope().journal = root / "text" / "llm_usage.jsonl"
            factory = self.project_factory
            if factory is None:
                from pipeline import Project
                factory = Project
            project = factory(str(root), str(root / "text" / "source.txt"),
                              target_minutes=data["minutes"], lang=data["language"], style=data["style"],
                              voice=data["voice"], frame_limit=data.get("frame_limit"), strict_render=True)
            project.image_retry_id = job["id"] if job["action"] == "retry" else None
            if job["action"] == "title":
                # Кнопка «Сделать заново»: только название и описание; ролик и его шаги не трогаются.
                current = "title"
                CP.set_step(current)
                self._step(job, current, "running")
                self._title(job, data, root, force=True)
                self._finish(job)
                return
            if job["action"] == "frame_retry":
                current = "images"
                held.enter_context(self._lane(job, current))
                CP.set_step(current)
                self._step(job, current, "running")
                result = project.retry_frame(job["frame_id"], job["id"], correction=job["correction"])
                self._step(job, current, "complete")
                held.close()
                if result.get("outcome") == "replaced" and DN.enabled():
                    # Чистится только пересданный кадр (и его крупный план); остальные кадры не трогаются.
                    current = "clean"
                    CP.set_step(current)
                    self._step(job, current, "running")
                    self._step(job, current, "complete",
                               note=project.step_clean(only=[int(job["frame_id"])])["note"])
                if result.get("outcome") == "replaced":
                    current = "render"
                    held.enter_context(self._lane(job, current))
                    CP.set_step(current)
                    self._step(job, current, "running")
                    project.step_render()
                    self._step(job, current, "complete")
                    held.close()
                self._finish(job)
                return
            checkpoints = {s["key"]: s["status"] for s in self.store.get_project(pid)["steps"]}
            # A removed result cannot be declared complete by an old DB checkpoint.
            if checkpoints.get("images") == "complete":
                frames = self.store.list_frames(pid)
                import repair as RP
                if not frames or any(not f["image"] or RP._intact(str(root / f["image"])) for f in frames):
                    for key in ("images", "judge", "repair", "fix", "clean", "render"):
                        checkpoints[key] = "pending"
                        self._step(job, key, "pending")
            # MANGA_QUOTE_MODE требует облако в кадре или крупный план говорящего, а на диске
            # их нет (готовый ролик собран в другом режиме): «Картинки» дорисуют только
            # недостающее, «Сборка» пересоберёт ролик. Текст, озвучка и тайминг не трогаются.
            if checkpoints.get("images") == "complete" and callable(getattr(type(project), "quote_missing", None)):
                need = project.quote_missing()
                if need["panel"] or need["closeup"]:
                    log.info("Ролик %s: не хватает облаков в кадре %s и крупных планов говорящего %s — "
                             "возвращаю в работу «Картинки» и «Сборку»", pid, len(need["panel"]), len(need["closeup"]))
                    for key in ("images", "clean", "render"):
                        checkpoints[key] = "pending"
                        self._step(job, key, "pending")
            # Врезки хука (MANGA_EXTRA_FRAMES): у готового ролика их нет на диске — «Картинки»
            # докупят только их, «Сборка» пересоберёт. Раньше нехватку не видел никто, и ролик
            # с готовыми кадрами врезок не получал никогда (аудит F03, 27.09).
            if checkpoints.get("images") == "complete" and callable(getattr(type(project), "extra_missing", None)):
                extra = project.extra_missing()
                if extra:
                    log.info("Ролик %s: не хватает врезок в хуке %s — возвращаю в работу «Картинки» и «Сборку»",
                             pid, len(extra))
                    for key in ("images", "clean", "render"):
                        checkpoints[key] = "pending"
                        self._step(job, key, "pending")
            if checkpoints.get("render") == "complete" and not (root / "out" / "video.mp4").is_file():
                checkpoints["render"] = "pending"
                self._step(job, "render", "pending")
            missing_audio = not (root / "audio/voice.mp3").is_file() or not read_json(root / "audio/blocks.json")
            missing_timing = not read_json(root / "text/timing.json")
            if checkpoints.get("voice") != "complete" or missing_audio:
                project.invalidate_master(timing=True)
                for key in ("voice", "align", "render"):
                    checkpoints[key] = "pending"
                    self._step(job, key, "pending")
            elif checkpoints.get("align") != "complete" or missing_timing:
                project.invalidate_master()
                for key in ("align", "render"):
                    checkpoints[key] = "pending"
                    self._step(job, key, "pending")
            if (checkpoints.get("render") == "complete" and (root / "out" / "video.mp4").is_file()
                    and not read_json(root / "out" / "acceptance.json")
                    and callable(getattr(type(project), "accept_video", None))):
                # Видео на месте, а машинной приёмки нет: её оборвал перезапуск (она идёт минутами).
                # Без отметки ролик вечно «не готов», хотя все шаги закрыты (21.09: b39239ea, 324237b5).
                # Пересобирать не надо — только проверить готовый файл; не вышло — не беда ролика.
                try:
                    log.info("Ролик %s: видео есть, приёмки нет — проверяю готовый файл заново", pid)
                    project.accept_video()
                except (CP.JobGone, QueueDatabaseError):
                    raise
                except Exception as exc:   # noqa: BLE001
                    log.warning("Ролик %s: приёмку готового видео повторить не удалось: %s", pid, exc)
            deferred = False
            for key, _ in STEPS:
                if self._stop.is_set():
                    self._title_leave(job, side)
                    self._finish(job, paused=True)
                    return
                if checkpoints.get(key) == "complete":
                    continue
                current = key
                # Ролик по тексту (ни ссылки, ни превью донора) не откладываем: шаг для него
                # мгновенный («пропущен»), а отложенная задача — лишний прогон очереди.
                has_donor = bool(data.get("source_url")) or (root / "source" / "thumbnail.jpg").is_file()
                if (key == "cover" and job["action"] == "run" and not data.get("frame_limit") and has_donor
                        and CV.enabled() and CV.defer_enabled() and self.store.others_waiting(pid)):
                    # Ролик уже собран, а исполнитель у очереди один: 2-10 минут обложки
                    # держали бы следующий ролик. Обложка уходит отдельной задачей в конец
                    # очереди. Только у первого прогона: отложенная задача — это «retry»,
                    # и второй раз она не откладывается, иначе обложка ждала бы вечно.
                    self._step(job, key, "pending", note="Обложка отложена: в очереди ждут другие ролики. "
                                                         "Студия доделает её отдельной задачей после них.")
                    deferred = True
                    continue
                if data.get("frame_limit") and key == "cover":
                    self._step(job, key, "complete", note="Пропущено в коротком проверочном прогоне: "
                                                          "обложка стоит генераций и здесь не покупалась.")
                    continue
                if data.get("frame_limit") and key in ("repair", "fix"):
                    self._step(job, key, "complete", note="Пропущено в коротком проверочном прогоне: автоматическая пересдача здесь не проверялась.")
                    continue
                if key == "title" and not PB.enabled():
                    self._step(job, key, "complete", note="Пропущено: шаг выключен (MANGA_TITLE=off). "
                                                          "Название и описание можно сделать кнопкой на странице ролика.")
                    continue
                if JUDGE == "off" and key in ("judge", "repair", "fix"):
                    self._step(job, key, "complete", note="Пропущено: судья кадров выключен настройкой MANGA_JUDGE=off — "
                                                          "кадры автоматически не проверялись и не пересдавались.")
                    continue
                if key == "clean" and not DN.enabled():
                    self._step(job, key, "complete", note="Пропущено: чистка кадров выключена (MANGA_CLEAN=off).")
                    continue
                if key == "title":
                    # Ролик не ждёт название: вызов модели идёт в своём потоке, «Паспорта сцен» стартуют сразу.
                    side = self._title_beside(job, data, root)
                    continue
                held.enter_context(self._lane(job, key))
                CP.set_step(key)
                self._step(job, key, "running")
                note = ""
                if key == "source":
                    got = SRC.run_step(root, data, state=SRC.state_path(self.store.root))
                    note = got["note"]
                    if got.get("title") and not self.store.set_source_title(pid, got["title"], job=job):
                        raise OwnershipLost(job["id"])
                elif key == "cover":
                    # Обложка — довесок к ролику, а не ролик. Любая её беда (генератор
                    # отказал, модель не ответила, превью не скачалось) раньше валила всю
                    # задачу: «Картинки», «Озвучка» и «Сборка» не выполнялись вовсе.
                    # Теперь беда закрывает ШАГ ошибкой, а задача идёт дальше и
                    # заканчивается успехом: ролик готов, обложки нет.
                    # Ошибкой, а не «готово»: «Повторить незавершённое» поднимает шаги со
                    # статусом «ошибка», и только так обещание «запустите шаг заново»
                    # выполнимо. Причина проходит через _safe_error: ключи и тела запросов
                    # провайдера на страницу ролика и в журнал не попадают.
                    try:
                        note = CV.run_step(root, data, alive=lambda: self._check(job))["note"]
                    except (CP.JobGone, QueueDatabaseError):
                        # Задачу забрали или служба останавливается, пока шаг ждёт лимита Claude:
                        # не беда обложки — пауза и продолжение после перезапуска (обработчики ниже).
                        raise
                    except Exception as exc:
                        if cut.is_set():
                            raise       # ожидание места FastGen прервано остановкой: шаг продолжится сам
                        reason = _safe_error(exc)
                        self._step(job, key, "error", error=reason,
                                   note="Обложка не сделана: " + reason + " Ролик это не останавливает: "
                                        "нажмите «Повторить незавершённое» — студия доделает одну обложку.")
                        log.warning("Обложка ролика %s не сделана: %s", pid, reason)
                        held.close()
                        continue
                elif key == "storyboard":
                    project.step_storyboard()
                    self._check(job)
                    project.step_refill()
                    self._check(job)
                    project.step_refill_memory()
                elif key == "sheets":
                    project.step_places()
                    self._check(job)
                    project.step_sheets()
                elif key == "images":
                    results = project.step_images(threads=14)
                    failed = [r for r in results if not r or r.get("status") == "ошибка"]
                    reasons = [str((r or {}).get("error") or "") for r in failed]
                    import pipeline as PL     # тот же модуль, что и Project выше
                    if cut.is_set():
                        # Ожидание места FastGen прервано остановкой службы или потерей задачи: недостающие
                        # кадры не брак, а недокупленные. Латать их соседом и закрывать шаг «готово» нельзя
                        # (проверка 21.09) — шаг ставится на паузу и после перезапуска докупит только их.
                        raise FP.Cancelled("шаг «Картинки» прерван остановкой: недостающие кадры докупятся после перезапуска")
                    if failed and results and len(failed) / len(results) <= PL.frames_missing_ok():
                        # Мелкий брак не роняет ролик: пропуски латаются соседом по сцене.
                        # 21.09 чешский ролик встал из-за 17 кадров из 864 и ушёл в конец очереди.
                        # Кадр, который генератор ещё рисует (оплачен, номер есть), — не брак: его
                        # докачает следующий прогон тем же номером, латать нельзя (перепроверка 21.09).
                        waiting = {str((r or {}).get("key") or "") for r in failed if _still_drawing((r or {}).get("error"))}
                        sb = project.step_storyboard()
                        PL.patch_missing_frames(project.p("frames"),
                                                [fd for fd in sb["frames"] if f"frame_{int(fd['idx']):05d}" not in waiting],
                                                log=PL.log)
                        failed = [fd for fd in sb["frames"]
                                  if not PL.frame_ready(project.p("frames", f"{int(fd['idx']):05d}.png"))]
                        # 27.09.2026 ORCH-F03-R2: пересчёт выше видит только кадры раскадровки и стирал ошибки
                        # врезок — ролик молча собирался без них. Отказ фильтра и остановленная квитанцией
                        # покупка — «кадр без врезки» с записью (extra_skip). Прочие — ошибка шага (явный повтор);
                        # на повторе шаг держит только врезка, которую генератор ещё рисует (оплачена), прочие
                        # уходят в заметку шага без записи. Их причины не мешают автоповтору (watch.classify).
                        врезки = [r for r in results if r and r.get("status") == "ошибка"
                                  and str(r.get("key") or "").endswith("_extra")]
                        решено = project.extra_skip(врезки) if врезки else set()
                        for r in врезки:
                            if r["key"] not in решено and (r["key"] in waiting
                                                           or not getattr(project, "image_retry_id", None)):
                                failed.append(r)
                            else:
                                reasons.remove(str(r.get("error") or ""))
                    if failed:
                        problem = RuntimeError(f"Не получены картинки: {len(failed)}. Повторите незавершённое.")
                        # Причины кадров — для решения об автоповторе (watch.classify); текст ошибки прежний.
                        problem.frame_errors = reasons
                        raise problem
                    # 27.09.2026 ORCH-F03-R2: «кадр без врезки» виден при каждом закрытии шага, пока есть запись
                    if callable(getattr(type(project), "extra_note", None)):
                        note = project.extra_note(results)
                elif key == "judge":
                    # Every frame gets a visible verdict; risk selection alone hides
                    # ordinary frames as unknown, which is unsuitable for the grid.
                    sb = project.step_storyboard()
                    project.step_judge(only=[int(f["idx"]) for f in sb["frames"]], workers=5)
                elif key == "repair":
                    project.step_repair(threads=12)
                elif key == "fix":
                    project.step_fix_identity(threads=6)
                elif key == "clean":
                    note = project.step_clean()["note"]
                else:
                    getattr(project, "step_" + key)()
                if cut.is_set():
                    # Любой шаг с покупками (листы, пересдача, герои): ожидание места FastGen прервано —
                    # шаг не доделан, «готово» ему ставить нельзя; общий обработчик ниже вернёт его в очередь.
                    raise FP.Cancelled(f"шаг «{dict(STEPS).get(key, key)}» прерван остановкой: продолжится после перезапуска")
                self._step(job, key, "complete", note=note)
                held.close()
            self._title_wait(job, side)
            # Отложенная обложка ставится в конец очереди в одной транзакции с закрытием задачи.
            self._complete(job, then_retry=deferred)
            if deferred:
                log.info("Обложка ролика %s отложена в конец очереди", pid)
        except _Paused:
            # Stopped while waiting for a lane: the waiting step never started, done steps stay done.
            self._title_leave(job, side)
            self._finish(job, paused=True)
        except OwnershipLost:
            log.warning("Задачу %s забрал другой исполнитель; этот остановился и её состояние не меняет", job["id"])
        except _StopWhileClaudeWaits:
            # Stopped while the step waited for the Claude limit: nothing was bought, the step resumes after restart.
            if current:
                self.store.step(pid, current, "pending", job=job)
            self._title_leave(job, side)
            self._finish(job, paused=True)
        except QueueDatabaseError:
            # The queue database failed, not the video: the loop pauses and gives the job back.
            raise
        except Exception as exc:
            # Остановка службы — не ошибка ролика: вызов модели, оборванный остановкой, отдаёт пустой
            # ответ раньше, чем служба поднимет флаг. Ждём флаг до STOP_GRACE_SECONDS — шаг вернётся в очередь.
            if cut.is_set() or self._stop.wait(STOP_GRACE_SECONDS):
                # Frames stopped waiting for FastGen room because the service stops or the job was taken:
                # not a video error. Taken — write nothing; stopping — back to the queue, the step resumes.
                if self._lost_event(job).is_set():
                    log.warning("Задачу %s забрал другой исполнитель; ожидание квоты FastGen прервано, "
                                "покупок не было", job["id"])
                    return
                log.warning("Ролик %s: шаг «%s» прерван остановкой службы, вернётся в очередь: %s",
                            pid, dict(STEPS).get(current, current), where_it_broke(exc))
                if current:
                    self.store.step(pid, current, "pending", job=job)
                self._title_leave(job, side)
                self._finish(job, paused=True)
                return
            if current:
                self.store.step(pid, current, "error", str(exc), job=job)
            self._title_leave(job, side)
            self._failed(job, exc, current)
        finally:
            FP.bind()
            held.close()

    def _title(self, job, data, root, force=False):
        """Шаг «Название». Сбой — ошибка шага, а не ролика; потеря задачи и сбой очереди идут выше как всегда."""
        told = []

        def waiting():
            # Прошлая попытка «Названия» этого ролика ещё ждёт ответа модели: второй вызов не делается.
            self._check(job)
            self._stop_claude_wait(job)
            if not told:
                told.append(1)
                self._step(job, "title", "running", note="Ждёт ответа модели для прошлой попытки «Названия» — "
                                                         "второй вызов не делается, возьмётся её результат.")

        try:
            got = PB.run_step(root, data, self._channel(data), force=force, on_wait=waiting)
        except (CP.JobGone, QueueDatabaseError):
            raise
        except Exception as exc:  # noqa: BLE001 — название не должно останавливать ролик
            log.warning("Ролик %s: шаг «Название» не удался: %s", job["project_id"], exc)
            self._step(job, "title", "error", f"{exc} Ролик собирается дальше; название можно сделать заново "
                                               "кнопкой на странице ролика.")
            return
        self._step(job, "title", "complete", note=got["note"])

    def _title_beside(self, job, data, root):
        """Шаг «Название» в цепочке ролика: свой поток со своим журналом Claude; ролик идёт дальше.
        Поток сам пишет итог шага (готово / ошибка), пока задача ролика идёт; ждут его только в конце ролика."""
        self._step(job, "title", "running", note="Идёт рядом с роликом: следующие шаги его не ждут.")
        done = threading.Event()
        journal = Path(root) / "text" / "llm_usage.jsonl"

        def run():
            mark = CP.use_scope(journal, step="title", check=lambda: self._check(job),
                                on_wait=lambda step, msg: self._step(job, "title", "running", note=msg),
                                stop=lambda: self._stop_claude_wait(job))
            try:
                self._title(job, data, root)
            except _StopWhileClaudeWaits:
                try:
                    self.store.step(job["project_id"], "title", "pending", job=job)
                except Exception:  # noqa: BLE001 — задача ролика сама вернётся в очередь
                    pass
            except (OwnershipLost, CP.JobGone):
                log.warning("Ролик %s: задачу забрали, итог шага «Название» не записан", job["project_id"])
            except Exception:  # noqa: BLE001 — поток названия не должен ронять службу
                log.exception("Ролик %s: поток шага «Название» упал", job["project_id"])
            finally:
                CP.reset_scope(mark)
                done.set()

        threading.Thread(target=run, daemon=True, name=f"manga-title-{job['project_id'][:8]}").start()
        return done

    def _title_wait(self, job, done):
        """Конец ролика: дождаться шага «Название» (обычно он давно готов), чтобы задача закрылась с его итогом."""
        if done is None:
            return
        while not done.wait(self.heartbeat_seconds):
            self._check(job)

    def _title_leave(self, job, done):
        """Ролик остановлен или упал, а название ещё идёт: шаг — «ждёт», повтор ролика возьмёт готовый файл
        без нового вызова модели (publish.json), если поток успеет его записать."""
        if done is None or done.is_set():
            return
        try:
            self.store.step(job["project_id"], "title", "pending", job=job)
        except Exception as exc:  # noqa: BLE001 — итог ролика важнее строки шага
            log.warning("Ролик %s: шаг «Название» не отмечен как ждущий: %s", job["project_id"], exc)

    def _channel(self, data):
        """Строка канала ролика из channels.json (описание и ключевые слова); нет — None."""
        if not data.get("channel"):
            return None
        try:
            return CH.find(CH.load(self.store.root), data["channel"])
        except CH.ChannelsError as exc:
            log.warning("Таблица каналов не читается, название без блока канала: %s", exc)
            return None

    @contextmanager
    def _lane(self, job, key):
        """Hold the lane of step key (lanes.py) while it runs. A video waiting here holds no other lane."""
        pid = job["project_id"]

        def waiting(text):
            self.waits[pid] = "Ролик " + text
            log.warning("Ролик %s %s", pid, text)

        try:
            with self.lanes.hold(key, on_wait=waiting,
                                 cancelled=lambda: self._stop.is_set() or self._lost_event(job).is_set()) as lane:
                if self.waits.pop(pid, None):
                    log.warning("Ролик %s дождался этапа «%s»", pid, dict(STEPS).get(key, key))
                self._in_lane[pid] = lane
                try:
                    yield lane
                finally:
                    self._in_lane.pop(pid, None)
        except LN.Cancelled:
            self.waits.pop(pid, None)
            self._check(job)   # taken by another owner: stop without writing anything
            raise _Paused()

    def wait_note(self, project_id):
        """Why a running video stands still now: its lane is busy or the FastGen hour is spent. None otherwise."""
        note = self.waits.get(project_id)
        if note or self._in_lane.get(project_id) != "images":
            return note
        try:
            import fastgen_pool
        except Exception:  # noqa: BLE001 — the page must open even if the pool cannot be read
            return None
        return fastgen_pool.wait_note(project_id)


if __name__ == "__main__":
    import argparse
    import signal
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.environ.get("MANGA_ROOT", str(Path(__file__).resolve().parents[1] / "projects_web")))
    args = parser.parse_args()
    worker = Worker(QueueStore(args.root)).start()
    signal.signal(signal.SIGTERM, lambda *_: worker._stop.set())
    signal.signal(signal.SIGINT, lambda *_: worker._stop.set())
    worker._thread.join()
