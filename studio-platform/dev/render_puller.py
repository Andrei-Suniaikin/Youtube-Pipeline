#!/usr/bin/env python3
"""
render_puller — агент рендера на GPU-боксе.

Цикл: claim задание с ген-сервера → скачать ассеты (tar.gz) → запустить render_worker
с конфигом задания → слать прогресс → отдать готовый mp4 (или ошибку) → повторить.

Только ИСХОДЯЩИЕ HTTP к ген-серверу (за NAT работает, портов открывать не надо).
Запускать на боксе (там же render_worker.py + ffmpeg + whisper). Env:
  VSGEN_SERVER   базовый URL ген-сервера (https://gen.13.140.129.66.sslip.io)
  PULLER_TOKEN   общий секрет (совпадает с серверным)
  RENDER_WORKER  путь к render_worker.py (default /workspace/render_worker.py)
  PULLER_WORK    рабочая папка (default /workspace/render_jobs)
  PULLER_POLL    интервал опроса сек (default 8)
"""
import os, sys, io, time, json, tarfile, subprocess, shutil, re, signal, threading
from collections import deque
import requests

SERVER = os.environ.get("VSGEN_SERVER", "https://gen.13.140.129.66.sslip.io").rstrip("/")
TOKEN = os.environ.get("PULLER_TOKEN", "")
RENDER_WORKER = os.environ.get("RENDER_WORKER", "/workspace/render_worker.py")
WORK = os.environ.get("PULLER_WORK", "/workspace/render_jobs")
POLL = int(os.environ.get("PULLER_POLL", "8"))
# Параллельных рендеров на ноде. x264-энкод CPU-bound (20 потоков делятся), но whisper-фаза
# (GPU, ~105с/джоб) и скачивание/заливка (сеть) у параллельных джобов перекрываются →
# на батчах ~1.5-2x throughput при N=3 (i5-14600KF/31GB тянет 3 слайдшоу-рендера).
CONCURRENCY = max(1, int(os.environ.get("PULLER_CONCURRENCY", "1")))
H = {"X-Puller-Token": TOKEN}


class RenderCancelled(Exception):
    pass


def _job_headers(claim_token):
    return {**H, "X-Render-Claim": claim_token}


def _log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def claim():
    r = requests.post(f"{SERVER}/api/render/claim", headers=H, timeout=30, verify=True)
    r.raise_for_status()
    return r.json().get("job")


def download_assets(jid, projdir, claim_token):
    """Распаковать материалы проекта ПОТОКОМ, не приземляя архив на диск.

    Раньше архив писался рядом с projdir (projdir + ".tar.gz") и удалялся только
    после успешной распаковки. Обрыв скачивания или ошибка extractall — и файл
    оставался навсегда: finally сносит только projdir, а архив лежал СНАРУЖИ него
    (нашли хвост 3273.tar.gz на 2.4 ГБ от 28.07). Теперь архива нет вообще, и это
    заодно минус его полный размер записи на SSD на каждый джоб (0.5–2.4 ГБ).

    decode_content=True обязателен: перед ген-сервером стоит Caddy со сжатием, и
    при Content-Encoding: gzip сырой поток был бы упакован дважды — tarfile снял бы
    только внешний слой и упал на каждом джобе. Это ровно то, что requests делает
    внутри iter_content, поэтому поведение то же, что и раньше.

    Обрыв на середине оставляет частично распакованный projdir — это безопасно:
    его сносит rmtree и в finally run_job, и перед следующим клеймом того же jid.
    """
    with requests.get(f"{SERVER}/api/render/{jid}/assets", headers=_job_headers(claim_token),
                      stream=True, timeout=600) as r:
        r.raise_for_status()
        r.raw.decode_content = True
        # tarfile в потоковом режиме читает мелкими блоками — буфер снимает
        # накладные расходы на тысячи мелких чтений из сокета на гигабайтном архиве
        with tarfile.open(fileobj=io.BufferedReader(r.raw, buffer_size=1 << 20),
                          mode="r|gz") as t:
            t.extractall(projdir)


def post_progress(jid, claim_token, pct, stage):
    try:
        r = requests.post(f"{SERVER}/api/render/{jid}/progress", headers=_job_headers(claim_token),
                          data={"pct": int(pct), "stage": stage[:120]}, timeout=15)
        if r.status_code == 409:
            return False
        r.raise_for_status()
    except Exception:
        return True   # сеть/5xx не должны убивать локальный рендер
    return True


def post_transcript(jid, claim_token, projdir):
    """Отдаёт chapters/transcript.json (сегменты whisper от рендера) — до mp4.

    Порядок важен: mp4 переводит задачу в done, и сервер там же решает, ставить ли
    главы. Прилетевший позже транскрипт этого решения уже не изменит.
    Не критично: нет файла или сеть отвалилась → рендер всё равно засчитывается.
    """
    path = os.path.join(projdir, "chapters", "transcript.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            r = requests.post(f"{SERVER}/api/render/{jid}/transcript",
                              headers={**_job_headers(claim_token), "Content-Type": "application/json"},
                              data=f, timeout=180)
        if r.status_code == 409:
            raise RenderCancelled()
        r.raise_for_status()
        _log(f"транскрипт отправлен: {os.path.getsize(path) // 1024} КБ")
        return True
    except RenderCancelled:
        raise
    except Exception as e:
        _log(f"транскрипт не отправлен ({e}) — главы посчитать будет не из чего")
        return False


def post_done(jid, claim_token, mp4, projdir=None):
    if projdir:
        post_transcript(jid, claim_token, projdir)
    with open(mp4, "rb") as f:
        r = requests.post(f"{SERVER}/api/render/{jid}/result?status=done",
                          headers={**_job_headers(claim_token), "Content-Type": "video/mp4"}, data=f, timeout=1200)
    if r.status_code == 409:
        raise RenderCancelled()
    r.raise_for_status()


def post_error(jid, claim_token, msg):
    try:
        r = requests.post(f"{SERVER}/api/render/{jid}/result?status=error",
                          headers=_job_headers(claim_token),
                          params={"error": (msg or "")[:480]}, timeout=30)
        if r.status_code == 409:
            _log(f"job {jid}: сервер уже закрыл задачу; локальная ошибка не принята")
            return False
        r.raise_for_status()
        return True
    except Exception as e:
        # Ошибка публикации раньше проглатывалась: в кабинете оставался running,
        # а единственная настоящая причина исчезала вместе с временной папкой.
        _log(f"job {jid}: не удалось передать ошибку серверу: {e}")
        return False


def _parse_pct(line):
    m = re.search(r"·\s*(\d+)%", line)                    # "Рендер: ...· 84%" → финальный энкод
    if m:
        return min(99, 60 + int(int(m.group(1)) * 0.39))
    m = re.search(r"пачек:\s*(\d+)/(\d+)", line)          # батч-энкод
    if m and int(m.group(2)) > 0:
        return 15 + int(int(m.group(1)) / int(m.group(2)) * 40)
    m = re.search(r"картинок:\s*(\d+)/(\d+)", line)       # подготовка кадров
    if m and int(m.group(2)) > 0:
        return int(int(m.group(1)) / int(m.group(2)) * 12)
    return None


def _stage_of(line):
    low = line.lower()
    if "картинок" in line:
        return "Подготовка кадров"
    if "пачек" in line or "кодиров" in low:
        return "Кодирование"
    if "whisper" in low or "синхрон" in low:
        return "Синхронизация по озвучке"
    if "Рендер" in line or "склейк" in low:
        return "Финальная сборка"
    return "Рендер"


def _stop_process_group(proc):
    """Остановить render_worker вместе со всеми порождёнными ffmpeg."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass


def run_job(job, widx=0):
    jid = job["job_id"]
    claim_token = job["claim_token"]
    cfg = job["config"]
    projdir = os.path.join(WORK, str(jid))
    proc = None
    finished = None
    output_tail = deque(maxlen=50)
    shutil.rmtree(projdir, ignore_errors=True)
    os.makedirs(projdir, exist_ok=True)
    try:
        if not post_progress(jid, claim_token, 1, "Скачиваю материалы"):
            raise RenderCancelled()
        download_assets(jid, projdir, claim_token)
        nimg = len([1 for _ in os.scandir(os.path.join(projdir, "googler"))]) if os.path.isdir(os.path.join(projdir, "googler")) else 0
        _log(f"job {jid}: ассеты ок ({nimg} файлов в googler), старт рендера")
        if not post_progress(jid, claim_token, 5, "Рендер"):
            raise RenderCancelled()
        proc = subprocess.Popen(
            [sys.executable, RENDER_WORKER, "--project_dir", projdir, "--config", json.dumps(cfg)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True)
        out_path = None
        state = {"pct": 5, "stage": "Рендер"}
        state_lock = threading.Lock()
        finished = threading.Event()
        cancelled = threading.Event()

        def cancel_watchdog():
            while not finished.wait(5):
                with state_lock:
                    pct, stage = state["pct"], state["stage"]
                if not post_progress(jid, claim_token, pct, stage):
                    cancelled.set()
                    _stop_process_group(proc)
                    return

        watcher = threading.Thread(target=cancel_watchdog, daemon=True)
        watcher.start()
        for line in proc.stdout:
            clean_line = line.rstrip()
            if clean_line:
                output_tail.append(clean_line)
            sm = re.search(r"__RENDER_SUCCESS__\|(.+)", line)
            if sm:
                out_path = sm.group(1).strip()
            pct = _parse_pct(line)
            with state_lock:
                state["stage"] = _stage_of(line)
                if pct is not None:
                    state["pct"] = pct
        proc.wait()
        finished.set()
        watcher.join(timeout=1)
        if cancelled.is_set():
            _log(f"job {jid}: отменён сервером, процесс остановлен")
            return
        if proc.returncode == 0 and out_path and os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            if not post_progress(jid, claim_token, 99, "Заливаю результат"):
                _log(f"job {jid}: отменён перед загрузкой результата")
                return
            _log(f"job {jid}: готово {out_path} ({os.path.getsize(out_path)//1024//1024} МБ), заливаю")
            post_done(jid, claim_token, out_path, projdir)
            _log(f"job {jid}: ✅ отдан")
        else:
            tail_text = "\n".join(output_tail)
            detail = f"render rc={proc.returncode}, out={out_path}"
            if tail_text:
                # Сервер хранит до 500 символов. Нужен именно хвост: traceback/ffmpeg
                # почти всегда объясняет падение последними строками.
                detail += "\n" + tail_text[-420:]
            post_error(jid, claim_token, detail)
            _log(f"job {jid}: ❌ {detail}")
    except RenderCancelled:
        _log(f"job {jid}: отменён сервером")
    except Exception as e:
        tail_text = "\n".join(output_tail)
        detail = str(e)
        if tail_text:
            detail += "\n" + tail_text[-420:]
        _log(f"job {jid}: исключение {detail}")
        post_error(jid, claim_token, detail)
    finally:
        if finished is not None:
            finished.set()
        if proc is not None and proc.poll() is None:
            _stop_process_group(proc)
        shutil.rmtree(projdir, ignore_errors=True)


_claim_lock = threading.Lock()


def worker_loop(widx):
    """Один слот рендера; лок сериализует claim, сами рендеры идут параллельно."""
    while True:
        job = None
        try:
            with _claim_lock:
                job = claim()
        except Exception as e:
            _log(f"[w{widx}] claim err:", e)
        if not job:
            time.sleep(POLL)
            continue
        _log(f"[w{widx}] взял job {job['job_id']} '{job.get('name')}' cfg={job['config'].get('quality')}/{job['config'].get('enable_fade')}/{job['config']['render_settings'].get('fast_batch_stitch')}")
        run_job(job, widx)


def main():
    if not TOKEN:
        _log("FATAL: PULLER_TOKEN не задан"); sys.exit(1)
    os.makedirs(WORK, exist_ok=True)
    _log(f"puller старт: server={SERVER} worker={RENDER_WORKER} poll={POLL}s concurrency={CONCURRENCY}")
    threads = [threading.Thread(target=worker_loop, args=(i,), daemon=True) for i in range(CONCURRENCY)]
    for t in threads:
        t.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
