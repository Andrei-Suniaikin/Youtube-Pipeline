# -*- coding: utf-8 -*-
"""Чистка кадров от ИИ-зерна (шаг «Чистка», настройка MANGA_CLEAN).

Замер 19.09 (tasks/anime_clean): слепые судьи — Real-ESRGAN аниме (realesrgan-x4plus-anime, x4 и обратно
до исходного размера INTER_AREA) 7,0 против 5,5 «как есть»; сглаживание fastNlMeansDenoisingColored h=3 — 6,6.

  MANGA_CLEAN=off      — как раньше: чистых копий нет, сборка берёт кадры как есть (по умолчанию).
  MANGA_CLEAN=nlmeans  — сглаживание h=3 на месте, в пуле потоков MANGA_CLEAN_THREADS.
  MANGA_CLEAN=esrgan   — кадры пачками по ssh едут на машину с видеокартой (MANGA_CLEAN_HOST) и обратно.
                         Машина недоступна или упала посреди пачки — остаток сглаживается на месте (nlmeans).
                         MANGA_CLEAN_FALLBACK=none — вместо этого остаток идёт в сборку как есть.

Оригинал frames/NNNNN.png не меняется никогда (нужен пересдаче и судье). Чистая копия —
frames/clean/NNNNN.png и рядом NNNNN.png.clean.json с отпечатком (sha256) исходника: сборка берёт
копию, только если отпечаток совпадает с кадром на диске сейчас; пересданный кадр чистится заново.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODES = ("off", "nlmeans", "esrgan")
NLM = dict(h=3, hColor=3, template=7, search=21)
_GRACE = 30   # сек сверх MANGA_CLEAN_TIMEOUT на передачу пачки, пока ssh не оборван
# Одна видеокарта на все ролики процесса: одновременно на ней не больше MANGA_CLEAN_PARALLEL пачек.
_SLOTS, _SLOTS_LOCK = {}, threading.Lock()


def _slots(cfg):
    with _SLOTS_LOCK:
        return _SLOTS.setdefault((cfg["host"], cfg["parallel"]), threading.BoundedSemaphore(cfg["parallel"]))


def _int(env, name, default, lo, hi):
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    if not raw.isdigit() or not lo <= int(raw) <= hi:
        raise ValueError(f"{name}: укажите целое число от {lo} до {hi}")
    return int(raw)


def settings(env=None) -> dict:
    """Настройки чистки из окружения; неверное значение — громкая ошибка."""
    env = os.environ if env is None else env
    mode = str(env.get("MANGA_CLEAN", "off")).strip().lower() or "off"
    if mode not in MODES:
        raise ValueError("MANGA_CLEAN: укажите off, nlmeans или esrgan")
    cpu = os.cpu_count() or 2
    binary = str(env.get("MANGA_CLEAN_BIN", "")).strip()
    cfg = dict(
        mode=mode,
        threads=_int(env, "MANGA_CLEAN_THREADS", max(1, min(4, cpu // 2)), 1, 64),
        host=str(env.get("MANGA_CLEAN_HOST", "")).strip(),
        label=str(env.get("MANGA_CLEAN_HOST_LABEL", "Минск")).strip() or "Минск",
        bin=binary,
        model=str(env.get("MANGA_CLEAN_MODEL", "realesrgan-x4plus-anime")).strip(),
        models=str(env.get("MANGA_CLEAN_MODELS", "")).strip() or (os.path.join(os.path.dirname(binary), "models")
                                                               if binary else ""),
        port=_int(env, "MANGA_CLEAN_PORT", 0, 0, 65535),
        key=str(env.get("MANGA_CLEAN_SSH_KEY", "")).strip(),
        ssh=shlex.split(str(env.get("MANGA_CLEAN_SSH", "ssh")).strip() or "ssh"),
        batch=_int(env, "MANGA_CLEAN_BATCH", 10, 1, 1000),
        parallel=_int(env, "MANGA_CLEAN_PARALLEL", 2, 1, 8),
        # Пусто — на пачку 60 с + 10 с на кадр (зависшая видеокарта не держит ролик 15 минут).
        timeout=_int(env, "MANGA_CLEAN_TIMEOUT", 0, 5, 86400) or None,
        connect_timeout=_int(env, "MANGA_CLEAN_CONNECT_TIMEOUT", 15, 1, 600),
        remote_tmp=str(env.get("MANGA_CLEAN_REMOTE_TMP", "/tmp")).strip() or "/tmp",
        remote_python=str(env.get("MANGA_CLEAN_REMOTE_PYTHON", "python3")).strip() or "python3",
        # что делать с кадрами, если машина с видеокартой недоступна: nlmeans — сгладить на месте (как было),
        # none — оставить как есть (без пятен сглаживания на крупной коже; при следующем запуске esrgan пробуется снова)
        fallback=str(env.get("MANGA_CLEAN_FALLBACK", "nlmeans")).strip().lower() or "nlmeans",
    )
    if cfg["fallback"] not in ("nlmeans", "none"):
        raise ValueError("MANGA_CLEAN_FALLBACK: укажите nlmeans или none")
    if mode == "esrgan" and not (cfg["host"] and cfg["bin"]):
        raise ValueError("MANGA_CLEAN=esrgan: нужны MANGA_CLEAN_HOST (например vova@<адрес машины>) "
                         "и MANGA_CLEAN_BIN (путь к realesrgan-ncnn-vulkan на той машине)")
    return cfg


def enabled(env=None) -> bool:
    return settings(env)["mode"] != "off"


# ---------- отпечаток и чистая копия ----------
def sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def clean_path(src) -> str:
    """frames/NNNNN.png -> frames/clean/NNNNN.png (крупный план говорящего — так же)."""
    return os.path.join(os.path.dirname(str(src)), "clean", os.path.basename(str(src)))


def _meta(dst):
    try:
        return json.loads(Path(str(dst) + ".clean.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def usable(src, sha=None, dst=None):
    """Чистая копия кадра, если она сделана из ЭТОГО кадра (отпечаток совпадает); иначе None.
    dst — своя копия (обложка); по умолчанию frames/clean/<имя>."""
    dst = str(dst or clean_path(src))
    meta = _meta(dst)
    if not meta or not os.path.isfile(dst):
        return None
    try:
        return dst if meta.get("source_sha256") == (sha or sha256(src)) else None
    except OSError:
        return None


def fresh(src, mode, sha=None, dst=None) -> bool:
    """Кадр чистить не надо: копия из этого же кадра и способ не хуже настроенного.
    При esrgan кадры, которые в прошлый раз ушли в запасное сглаживание, пробуются снова
    (шаг «Чистка» делает это только когда машина отвечает — см. upgradable/reachable)."""
    dst = usable(src, sha, dst)
    return bool(dst) and not upgradable(src, mode, sha, dst)


def upgradable(src, mode, sha=None, dst=None) -> bool:
    """Копия из этого кадра есть, но сделана запасным сглаживанием, а настроен esrgan."""
    dst = dst or usable(src, sha)
    return bool(dst) and mode == "esrgan" and (_meta(dst) or {}).get("method") != "esrgan"


def pack_timeout(cfg, n) -> int:
    """Сколько ждать одну пачку на той машине: MANGA_CLEAN_TIMEOUT или 60 с + 10 с на кадр."""
    return int(cfg["timeout"] or (60 + 10 * max(1, n)))


_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# IPv6 (у Tailscale вида fd7a:115c:a1e0::1): группы hex через двоеточие
_IP6 = re.compile(r"(?<![\w:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?:%\w+)?(?![\w:])")


def _is_ip6(text) -> bool:
    """Похоже на IPv6 (есть «::» или 6+ двоеточий), а не на время 12:30:45."""
    t = text.split("%")[0]
    return ("::" in t or t.count(":") >= 6) and bool(t.strip(":"))


def scrub(text, cfg) -> str:
    """Текст ошибки ssh без адреса машины: хост и IP заменяются подписью (для журнала и отпечатков)."""
    text = str(text or "")
    host = str(cfg.get("host") or "")
    for part in sorted({host, host.rsplit("@", 1)[-1]} - {""}, key=len, reverse=True):
        text = text.replace(part, cfg["label"])
    text = _IP.sub(cfg["label"], text)
    text = _IP6.sub(lambda m: cfg["label"] if _is_ip6(m.group(0)) else m.group(0), text)
    return re.sub(r"\bport \d+", "port …", text)


def reachable(cfg) -> bool:
    """Короткая проверка связи (ssh … true): отвечает ли машина прямо сейчас."""
    try:
        r = subprocess.run(ssh_command(cfg, "true"), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=cfg["connect_timeout"] + 10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _write(dst, image, meta):
    import cv2
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = f"{dst}.{os.getpid()}.{threading.get_ident()}.tmp.png"
    if not cv2.imwrite(tmp, image):
        raise OSError(f"не записан {tmp}")
    os.replace(tmp, dst)
    mtmp = f"{dst}.clean.json.{os.getpid()}.{threading.get_ident()}.tmp"
    Path(mtmp).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os.replace(mtmp, dst + ".clean.json")


# ---------- nlmeans ----------
def nlmeans(image):
    import cv2
    return cv2.fastNlMeansDenoisingColored(image, None, NLM["h"], NLM["hColor"], NLM["template"], NLM["search"])


def _nlmeans_one(item, note=""):
    import cv2
    im = cv2.imread(item["input"], cv2.IMREAD_COLOR)
    if im is None:
        raise OSError(f"не читается {item['input']}")
    t = time.time()
    out = nlmeans(im)
    dt = time.time() - t
    _write(item["dst"], out, {"source_sha256": item["sha"], "method": "nlmeans", "h": NLM["h"],
                              "seconds": round(dt, 3), **({"fallback": note} if note else {})})
    return dt


def _nlmeans_many(items, threads, log, note=""):
    """-> (сделано, [(item, ошибка)], секунды чистки по кадрам)"""
    done, failed, times = 0, [], []
    if not items:
        return done, failed, times
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        futures = [(it, pool.submit(_nlmeans_one, it, note)) for it in items]
        for it, fut in futures:
            try:
                times.append(fut.result())
                done += 1
            except Exception as exc:  # noqa: BLE001 — кадр без чистой копии уходит в сборку как есть
                failed.append((it, str(exc)))
                log(f"  ! чистка {os.path.basename(it['dst'])}: {exc} — в ролик пойдёт кадр как есть")
    return done, failed, times


# ---------- esrgan на удалённой машине ----------
_SHRINK = r'''
import os, sys
d = sys.argv[1]
try:
    import cv2
except Exception:
    cv2 = None
    from PIL import Image
for n in sorted(os.listdir(d + "/x4")):
    src, up, out = d + "/in/" + n, d + "/x4/" + n, d + "/out/" + n
    if not os.path.exists(src):
        continue
    if cv2 is not None:
        a, b = cv2.imread(src), cv2.imread(up)
        if a is None or b is None:
            continue
        if b.shape[:2] != a.shape[:2]:
            b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
        cv2.imwrite(out, b)
    else:
        a, b = Image.open(src), Image.open(up).convert("RGB")
        if b.size != a.size:
            b = b.resize(a.size, Image.BOX)
        b.save(out)
'''


def _rq(path) -> str:
    """Путь для команды на той машине: «~/…» раскрывается там же (через $HOME), остальное в кавычках."""
    path = str(path)
    if path == "~" or path.startswith("~/"):
        return '"$HOME"' + (shlex.quote(path[1:]) if len(path) > 1 else "")
    return shlex.quote(path)


def remote_script(cfg, timeout=None) -> str:
    """Команда для ssh: своя временная папка, распаковка кадров из stdin, x4, уменьшение до исходного
    размера на той же машине (передаётся назад исходный размер, а не x4), архив результатов в stdout.
    Папка удаляется при любом выходе; забытые папки этого же вида старше 12 ч убираются."""
    q = shlex.quote
    base = cfg["remote_tmp"]
    bq = _rq(base)
    lim = int(timeout or pack_timeout(cfg, cfg["batch"]))   # отдельно: f-строка должна собираться на Python 3.10
    return "\n".join([
        "set -e",
        f"find {bq} -maxdepth 1 -name 'manga-clean-*' -mmin +720 -exec rm -rf {{}} + 2>/dev/null || true",
        f"d=$(mktemp -d {bq}/manga-clean-XXXXXX)",
        "trap 'rm -rf \"$d\"' EXIT HUP INT TERM",
        'mkdir "$d/in" "$d/x4" "$d/out"',
        'tar -C "$d/in" -xf -',
        f'TO=""; command -v timeout >/dev/null 2>&1 && TO="timeout {lim}"',
        f'$TO {_rq(cfg["bin"])} -i "$d/in" -o "$d/x4" -n {q(cfg["model"])} -m {_rq(cfg["models"])} -f png >&2',
        f'{q(cfg["remote_python"])} -c {q(_SHRINK)} "$d" >&2 || cp "$d"/x4/*.png "$d/out/"',
        'tar -C "$d/out" -cf - .',
    ])


def ssh_command(cfg, script) -> list:
    cmd = list(cfg["ssh"]) + ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={cfg['connect_timeout']}",
                              "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    if cfg["port"]:
        cmd += ["-p", str(cfg["port"])]
    if cfg["key"]:
        cmd += ["-i", cfg["key"]]
    return cmd + [cfg["host"], script]


def _pack(items, cfg, work, log):
    """Одна пачка туда и обратно. -> (сделано: [item], сек, байт туда, байт обратно). Сбой — исключение."""
    import cv2
    up_tar, down_tar = os.path.join(work, "up.tar"), os.path.join(work, "down.tar")
    with tarfile.open(up_tar, "w") as tf:
        for it in items:
            tf.add(it["input"], arcname=it["name"])
    sent = os.path.getsize(up_tar)
    limit = pack_timeout(cfg, len(items))
    t = time.time()
    with open(up_tar, "rb") as fin, open(down_tar, "wb") as fout:
        try:
            r = subprocess.run(ssh_command(cfg, remote_script(cfg, limit)), stdin=fin, stdout=fout,
                               stderr=subprocess.PIPE, timeout=limit + cfg["connect_timeout"] + _GRACE)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"нет ответа за {limit} с")
    dt = time.time() - t
    got = os.path.getsize(down_tar)
    if r.returncode != 0:
        tail = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"код {r.returncode}: {scrub(tail[0], cfg)[:200]}")
    out_dir = os.path.join(work, "down")
    os.makedirs(out_dir, exist_ok=True)
    with tarfile.open(down_tar, "r") as tf:
        wanted = {it["name"] for it in items}
        for m in tf.getmembers():
            name = os.path.basename(m.name)
            if m.isfile() and name in wanted:
                m.name = name
                tf.extract(m, out_dir)
    done = []
    for it in items:
        path = os.path.join(out_dir, it["name"])
        im0 = cv2.imread(it["input"], cv2.IMREAD_COLOR)
        im = cv2.imread(path, cv2.IMREAD_COLOR) if os.path.isfile(path) else None
        if im is None or im0 is None:
            continue
        h, w = im0.shape[:2]
        if im.shape[:2] != (h, w):
            if im.shape[:2] != (h * 4, w * 4):
                continue
            im = cv2.resize(im, (w, h), interpolation=cv2.INTER_AREA)   # как в опыте anime_clean
        _write(it["dst"], im, {"source_sha256": it["sha"], "method": "esrgan", "model": cfg["model"],
                               "seconds": round(dt / max(1, len(items)), 3)})
        done.append(it)
    return done, dt, sent, got


def _esrgan_many(items, cfg, log):
    """-> (сделано, оставшиеся [item], причина сбоя или "", замер).
    Пачки идут по MANGA_CLEAN_PARALLEL одновременно: пока одна едет по сети, другая на видеокарте.
    Первый сбой (ssh, таймаут, ненулевой код) — новые пачки на ту машину не отправляются, их кадры
    и кадры упавшей пачки сглаживаются на месте. Уже начатые пачки доезжают.
    Пачка вернулась неполной — на месте сглаживаются только недостающие кадры, машина остаётся в строю."""
    stats = dict(seconds=0.0, sent=0, got=0, frames=0, wall=0.0)
    packs = [items[i:i + cfg["batch"]] for i in range(0, len(items), cfg["batch"])]
    broken, short, lock = [], [], threading.Lock()
    t0 = time.time()

    def run(n, pack, work):
        if broken:
            return pack, None
        sub = os.path.join(work, f"p{n:05d}")
        os.makedirs(sub)
        try:
            with _slots(cfg):
                if broken:
                    return pack, None
                ok, dt, sent, got = _pack(pack, cfg, sub, log)
        except Exception as exc:  # noqa: BLE001 — машина недоступна: остаток сглаживается на месте
            why = scrub(exc, cfg)
            with lock:
                broken.append(why)
            return pack, why
        with lock:
            stats["seconds"] += dt
            stats["sent"] += sent
            stats["got"] += got
            stats["frames"] += len(ok)
        log(f"  {cfg['label']}: пачка {n + 1}/{len(packs)} — {len(ok)} из {len(pack)} кадров за {dt:.1f} с "
            f"(туда {sent / 1e6:.1f} МБ, обратно {got / 1e6:.1f} МБ)")
        bad = [it for it in pack if not any(it is o for o in ok)]
        if bad:
            why = f"вернулось {len(ok)} из {len(pack)} кадров пачки"
            log(f"  ! {cfg['label']}: {why} — недостающие сгладятся на сервере")
            with lock:
                short.append(why)
            return bad, why
        return [], None

    rest = []
    with tempfile.TemporaryDirectory(prefix="manga-clean-") as work:
        with ThreadPoolExecutor(max_workers=cfg["parallel"]) as pool:
            for left, _ in [f.result() for f in [pool.submit(run, n, p, work) for n, p in enumerate(packs)]]:
                rest += left
    stats["wall"] = round(time.time() - t0, 1)
    return len(items) - len(rest), rest, (broken or short or [""])[0], stats


# ---------- общий вход ----------
def clean_many(items, cfg=None, log=print) -> dict:
    """items: [{src, input, dst}] — src даёт отпечаток, input — пиксели (кадр без звёздочки), dst — копия.
    Ролик не падает: сбой кадра — кадр идёт в сборку как есть."""
    cfg = cfg or settings()
    res = dict(mode=cfg["mode"], total=len(items), esrgan=0, nlmeans=0, fallback=0, failed=0, note="",
               remote_error="", seconds=0.0)
    if cfg["mode"] == "off" or not items:
        return res
    for it in items:
        it.setdefault("sha", sha256(it["src"]))
        it.setdefault("name", os.path.basename(it["dst"]))
    t0 = time.time()
    rest = items
    if cfg["mode"] == "esrgan":
        n, rest, why, stats = _esrgan_many(items, cfg, log)
        res["esrgan"], res["remote"] = n, stats
        if rest:
            res["remote_error"] = why
            res["fallback"] = len(rest)
            if cfg["fallback"] == "none":
                res["skipped"] = len(rest)
                log(f"  ! {cfg['label']} недоступен ({why}) — {len(rest)} кадров пойдут как есть")
                rest = []
            else:
                log(f"  ! {cfg['label']} недоступен ({why}) — {len(rest)} кадров сглажены на сервере")
    done, failed, times = _nlmeans_many(rest, cfg["threads"], log, note=res["remote_error"])
    res["nlmeans"], res["failed"] = done, len(failed)
    if times:
        res["nlmeans_per_frame"] = round(sum(times) / len(times), 3)
    res["seconds"] = round(time.time() - t0, 1)
    res["note"] = note(res, cfg)
    return res


def note(res, cfg) -> str:
    parts = []
    if res["esrgan"]:
        parts.append(f"Real-ESRGAN на машине «{cfg['label']}»: {res['esrgan']}")
    if res.get("skipped"):
        parts.append(f"{cfg['label']} недоступен — кадры пойдут как есть: {res['skipped']}")
    elif res["fallback"]:
        parts.append(f"{cfg['label']} недоступен — кадры сглажены на сервере: {res['nlmeans']}")
    elif res["nlmeans"]:
        parts.append(f"сглажено на сервере: {res['nlmeans']}")
    if res["failed"]:
        parts.append(f"не почищено (пойдут как есть): {res['failed']}")
    if not parts:
        return "Все кадры уже почищены."
    return "Чистка кадров — " + "; ".join(parts) + f". За {res['seconds']:.0f} с."


def clean_image(path, dst=None, cfg=None, log=print) -> str:
    """Почистить одну картинку (для будущей обложки). Возвращает путь чистой копии или исходник при off/сбое."""
    cfg = cfg or settings()
    if cfg["mode"] == "off":
        return str(path)
    dst = dst or clean_path(path)
    if fresh(path, cfg["mode"], dst=dst):
        return dst
    res = clean_many([dict(src=str(path), input=str(path), dst=str(dst))], cfg, log=log)
    return str(dst) if res["esrgan"] + res["nlmeans"] else str(path)
