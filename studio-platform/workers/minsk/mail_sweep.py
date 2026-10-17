#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обход почты каналов: все профили с привязками, по одному, список писем → студия.

Зачем (17.10.2026). Пересылку на приёмник Google этим аккаунтам не даёт, поэтому письма
YouTube/Google/AdSense читаются прямо в Gmail профиля AdsPower (только список, без
открытия писем) и отправляются в студию: `POST /api/mail/inbox` под токеном ноды.
В студии они становятся событиями у канала (вкладка «Статистика» → «Почта канала»).

Правила обхода:
  * один профиль за раз, под бронью координатора — как заливка и сборщик часов;
  * профиль, занятый заливкой или открытый, пропускается и повторяется позже;
  * один проход — не дольше `--max-minutes` (по умолчанию 120): что не успели — завтра;
  * между профилями пауза, чтобы не дёргать AdsPower очередью стартов;
  * второй обход одновременно не стартует (замок в ~/mail_sweep/.lock);
  * `--dry` — ничего не отправлять, сложить JSON в ~/mail_sweep/dry/<дата>/;
  * журнал ~/mail_sweep/<дата>.log; полных адресов почты в нём нет.

    ~/mail_sweep.sh                      # все профили с привязками, отправка в студию
    ~/mail_sweep.sh --profiles 30 --dry  # один профиль, без отправки
"""
import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/vova")
import gmail_inbox_reader as reader           # noqa: E402
import upload_puller as P                     # noqa: E402

HOME = Path.home()
ROOT = HOME / "mail_sweep"
HOST = os.environ.get("UPLOAD_HOST", "minsk")


class Log:
    def __init__(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        os.chmod(ROOT, 0o700)
        self.path = ROOT / (time.strftime("%Y-%m-%d") + ".log")

    def __call__(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def send(payload, dry, dry_dir, log):
    """Отправить строки одной почты в студию (или сложить рядом при --dry)."""
    tag = f"{payload['profile']}_{payload['login_masked'].split('@')[0].rstrip('…')}"
    if dry:
        p = dry_dir / f"{tag}.json"
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(p, 0o600)
        log(f"  [dry] {tag}: строк {len(payload['rows'])} → {p}")
        return {"dry": True, "rows": len(payload["rows"])}
    if not P.TOKEN:
        raise RuntimeError("нет UPLOAD_TOKEN — отправлять нечем")
    out = P.call("/api/mail/inbox", payload)
    log(f"  {tag}: строк {out.get('received')}, новых {out.get('new')}, повторов {out.get('dup')}, "
        f"на проверку {out.get('review')}, события {out.get('events')}")
    return out


def process(profile, a, dry_dir, log):
    """Один профиль: прочитать все его почты и отправить. Возвращает 'ok' | 'busy' | 'fail'."""
    logins = reader.login_for(profile)
    log(f"профиль {profile}: почт в реестре {len(logins)}")
    try:
        rec = reader.read_profile_inbox(profile, logins, evidence=ROOT / "evidence",
                                        max_authuser=a.max_authuser)
    except reader.ProfileBusy as exc:
        log(f"  занят: {exc}")
        return "busy"
    except Exception as exc:                                       # noqa: BLE001
        log(f"  ошибка чтения: {type(exc).__name__}: {str(exc)[:160]}")
        return "fail"
    if rec.get("error"):
        log(f"  ошибка в браузере: {rec['error']}")
    if rec.get("missing"):
        log(f"  в браузере не нашли почты: {', '.join(rec['missing'])}")
    if not rec.get("closed"):
        log("  ВНИМАНИЕ: браузер профиля мог остаться открытым")
    status = "ok" if rec.get("results") and not rec.get("error") else "fail"
    for res in rec.get("results") or []:
        if res.get("error"):
            log(f"  {res['login_masked']}: список не прочитан ({res['error']})")
            status = "fail"
            continue
        payload = {"host": HOST, "profile": str(profile),
                   "login_masked": res["login_masked"], "login_sha256": res["login_sha256"],
                   "login_ucs": reader.ucs_for_login(_login_by_sha(logins, res["login_sha256"])),
                   "rows": res["rows"]}
        try:
            send(payload, a.dry, dry_dir, log)
        except Exception as exc:                                   # noqa: BLE001
            log(f"  {res['login_masked']}: отправка не удалась: {type(exc).__name__}: {str(exc)[:160]}")
            status = "fail"
    return status


def _load_last_read():
    try:
        return json.loads((ROOT / "last_read.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _mark_read(profile):
    data = _load_last_read()
    data[str(profile)] = time.time()
    (ROOT / "last_read.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


def _login_by_sha(logins, sha):
    for e in logins:
        if reader.login_sha(e) == sha:
            return e
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles", default="", help="через запятую; пусто — все с привязками")
    ap.add_argument("--dry", action="store_true", help="не отправлять, сложить JSON")
    ap.add_argument("--max-minutes", type=int, default=120)
    ap.add_argument("--pause", type=int, default=20, help="секунд между профилями")
    ap.add_argument("--retry-wait", type=int, default=600, help="секунд до повтора занятых")
    ap.add_argument("--max-authuser", type=int, default=0)
    a = ap.parse_args()
    log = Log()

    lock = open(ROOT / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("обход уже идёт — второй не стартую")
        return 2

    profiles = ([p.strip() for p in a.profiles.split(",") if p.strip()]
                if a.profiles else reader.profiles_with_bindings())
    # Порядок — кого дольше всех не читали, тот первым. Один профиль занимает 2–4 минуты,
    # сорок шесть профилей в два часа не укладываются, и при постоянном порядке хвост
    # списка не дошёл бы никогда. Время последнего чтения — в ~/mail_sweep/last_read.json.
    last_read = _load_last_read()
    profiles.sort(key=lambda p: (last_read.get(p, 0), int(p) if p.isdigit() else 0))
    dry_dir = ROOT / "dry" / time.strftime("%Y%m%dT%H%M%S")
    if a.dry:
        dry_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(dry_dir, 0o700)
    deadline = time.time() + a.max_minutes * 60
    log(f"старт: профилей {len(profiles)}, {'сухой прогон' if a.dry else 'отправка в ' + P.SERVER}, "
        f"лимит {a.max_minutes} мин")
    if not a.dry and not P.TOKEN:
        log("нет UPLOAD_TOKEN — останавливаюсь")
        return 2

    done, busy, failed = [], [], []
    queue = list(profiles)
    rounds = 0
    while queue and time.time() < deadline:
        rounds += 1
        deferred = []
        for p in queue:
            if time.time() >= deadline:
                deferred.extend(queue[queue.index(p):])
                break
            st = process(p, a, dry_dir, log)
            (done if st == "ok" else deferred if st == "busy" else failed).append(p)
            if st == "ok" and not a.dry:
                _mark_read(p)
            time.sleep(a.pause)
        queue = deferred
        if queue and time.time() + a.retry_wait < deadline:
            log(f"занятых {len(queue)}: {', '.join(queue)} — повтор через {a.retry_wait // 60} мин")
            time.sleep(a.retry_wait)
        elif queue:
            break
    busy = queue
    summary = {"finished": time.strftime("%Y-%m-%dT%H:%M:%S"), "dry": a.dry, "rounds": rounds,
               "done": done, "busy_left": busy, "failed": failed}
    log(f"итог: прочитано {len(done)}, не дождались (заняты) {len(busy)}, с ошибкой {len(failed)}")
    if busy:
        log(f"  остались занятыми: {', '.join(busy)}")
    if failed:
        log(f"  с ошибкой: {', '.join(failed)}")
    (ROOT / "last_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
