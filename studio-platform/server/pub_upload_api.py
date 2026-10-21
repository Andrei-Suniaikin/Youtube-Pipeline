#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обмен с машиной, которая заливает: кабинет отдаёт задание, нода приносит вердикт.

Сделано по образцу рендер-ноды (исходящий опрос, свой токен, клейм с проверкой), но с
четырьмя отличиями — каждое закрывает способ получить второй ролик на канале:

1. **Истёкшая аренда ведёт в карантин, а не назад в очередь.** У рендера молчание ноды
   возвращает задачу в очередь: для рендера это безопасно, для заливки — второй ролик.
2. **Один профиль — одна заливка. Разные профили — параллельно.** Задание по профилю не
   выдаётся, пока по нему идёт заливка: два мастера Studio в одном профиле ломают друг
   друга (диалог отдаёт поля нулевого размера, воркер падает с пустой ошибкой), а тот же
   профиль на второй машине рвёт куки. Но профили независимы, поэтому свободные выдаются
   одновременно — сколько у ноды рабочих мест, столько роликов и льётся.
   Тот же канал тоже исключается: два мастера на одном канале путают Studio.
3. **Повторный запрос возвращает ТОТ ЖЕ токен.** Если ответ не доехал до ноды (перезапуск
   кабинета, 502), нода спросит снова и получит своё же задание, а не второе.
4. **Вердикт разбирается по evidence, а не по коду возврата.** «Стоит в расписании» ставится
   только при совпадении канала, video ID, времени и признака SCHEDULED.
5. **Профиль, открытый ПОСТОРОННИМ, тоже занят.** Пункт 2 знает только про наши заливки,
   а на той же минской машине живут старый конвейер gen, конвейер манхвы и человек,
   который открывает профиль руками. Перед выдачей спрашиваем у AdsPower этой машины,
   кто открыт прямо сейчас (`pub_adspower.active_profiles`), и чужой профиль пропускаем:
   задание остаётся в очереди, профиль не трогаем. Молчание AdsPower — это НЕ занятость:
   при нём выдаём как раньше и говорим об этом в журнале, иначе один обрыв сети
   остановил бы заливку сразу на всех каналах.
6. **Залитый ролик с номером, но без расписания, доводится машиной по номеру.** Задание
   в `repair` с `video_id` выдаётся той же ручкой клейма как ЗАДАЧА ДОВЕДЕНИЯ
   (`finalize_video_id` в теле): файл второй раз не отдаётся и не нужен, заливщик
   находит черновик по номеру в Studio и дописывает расписание. Неудача возвращает
   задание в `repair` (не в `error` — номер нельзя потерять), следующая попытка не
   раньше чем через 6 часов, всего три; дальше — человеку словами (18.10.2026).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

import pub_adspower
import pub_artifact
import pub_upload_census
from pub_engine import (FINALIZE_GAP_SEC, FINALIZE_MAX_TRIES, finalize_due,
                        finalize_exhausted_reason, finalize_tries, pause_clear, pause_get,
                        pause_scope, pause_set, pause_text, reslot_upload_job)
from pub_engine import revive as engine_revive
from pub_log import add as log_line, add_lines

# Сколько ждать вестей от ноды, прежде чем считать заливку зависшей.
# С запасом на самый долгий наблюдавшийся мастер Studio (~9 минут на больном прокси).
UPLOAD_LEASE_SEC = 45 * 60

# Успешные исходы Studio. Всё остальное — не успех, даже если video_id получен.
OK_STATES = ("SCHEDULED",)
PUBLISHED_STATES = ("PUBLIC",)


# Флаг «новых заданий не выдавать»: его ставит выкатка (deploy/update.sh) рядом с базой.
# Старше DRAIN_MAX_SEC — не верим: упавшая выкатка не должна останавливать заливку навсегда.
DRAIN_MAX_SEC = 1800


def _drain_file() -> str:
    explicit = os.environ.get("VS_UPLOAD_DRAIN_FILE")
    if explicit:
        return explicit
    db_path = os.environ.get("VS_SERVER_DB") or os.environ.get("VS_DEV_DB") or ""
    return os.path.join(os.path.dirname(db_path), "upload-drain") if db_path else ""


def _draining(now: float = None) -> bool:
    path = _drain_file()
    if not path:
        return False
    try:
        age = (time.time() if now is None else now) - os.path.getmtime(path)
    except OSError:
        return False
    return 0 <= age < DRAIN_MAX_SEC


UC_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


def _agent_profile(serial):
    """Серийный номер AdsPower в том виде, в каком его ждёт нода, или None."""
    text = str(serial or "").strip()
    if not text:
        return None
    return int(text) if text.isdigit() else text


def _finalizing(row) -> bool:
    """Машина держит это задание ради ДОВЕДЕНИЯ черновика по номеру, а не заливки файла.

    Признак — номер ролика уже известен, а клейм выдан в момент начала попытки
    доведения (`finalize_last`). У обычной заливки номер в задании появляется только
    вместе с вердиктом или от удержанного окна — то есть никогда у задания `running`.
    """
    if row is None or (row["status"] or "") != "running" or not (row["video_id"] or ""):
        return False
    try:
        last = row["finalize_last"]
    except (KeyError, IndexError):
        return False
    return last is not None and float(last) >= float(row["claimed"] or 0)


# Чем держится задание на доведение, когда профиль свободен, а выдавать всё равно нельзя.
FINALIZE_HOLD_SLOT = ("доведение черновика по номеру ждёт: время выхода прошло, а свободного "
                      "выхода на 30 дней вперёд нет — добавьте времена выхода каналу")


def register_upload_routes(app, ctx):
    db = ctx["db"]
    _now = ctx["_now"]
    token_of = ctx["upload_token"]          # общий секрет ноды заливки
    log = ctx.get("log") or (lambda *a: None)

    def _same_secret(got: str, want: str) -> bool:
        """Сравнение секретов постоянного времени, устойчивое к мусору в заголовке.

        23.09.2026, найдено на боевой выкатке: `secrets.compare_digest` на строках с
        не-ASCII бросает TypeError, и нода, приславшая мятый заголовок, получала 500
        «внутренняя ошибка» вместо честного «не пущу». Пятисотка тут вреднее отказа:
        она выглядит как поломка сервера и уводит разбор не туда.
        """
        if not want or not got:
            return False
        try:
            return secrets.compare_digest(got.encode("utf-8"), want.encode("utf-8"))
        except (UnicodeError, TypeError):                          # noqa: BLE001
            return False

    def _auth(request: Request) -> None:
        if not _same_secret(request.headers.get("X-Upload-Token", ""), token_of()):
            raise HTTPException(401, "нужен токен ноды заливки")

    def _own(request: Request, rel) -> None:
        if not _same_secret(request.headers.get("X-Upload-Claim", ""),
                            rel["claim_token"] or ""):
            raise HTTPException(403, "чужой или устаревший клейм")

    def _release(c, release_id: str):
        return c.execute("SELECT * FROM pub_upload_jobs WHERE release_id=?",
                         (release_id,)).fetchone()

    # ── ворота: профиль, открытый посторонним, не выдаём ──

    def _host_targets(c) -> list:
        """Машины, чьи задания ждут в очереди: [(id, адрес AdsPower, имя для человека)].

        Только чтение базы, без сети — чтобы спрашивать машины можно было отдельно и в
        другом потоке (см. `_ask_hosts`).

        Машина берётся от ПРОФИЛЯ (`pub_profiles.default_host_id`), а не от имени, которое
        нода назвала о себе в запросе: имя в запросе — свободная строка из переменной
        окружения ноды, она ни с чем не связана, а профиль физически живёт там, где его
        завели, и спрашивать надо именно тот AdsPower.
        """
        rows = c.execute(
            "SELECT DISTINCT p.default_host_id hid, h.adspower_url url, h.title htitle, "
            "h.code hcode FROM pub_upload_jobs u JOIN pub_profiles p ON p.id=u.profile_id "
            "LEFT JOIN pub_hosts h ON h.id=p.default_host_id "
            "WHERE (u.status='queued' OR (u.status='repair' AND COALESCE(u.video_id,'')!='')) "
            "AND p.default_host_id IS NOT NULL").fetchall()
        return [(r["hid"], r["url"] or "",
                 (r["htitle"] or r["hcode"] or "").strip() or f"№{r['hid']}") for r in rows]

    def _ask_hosts(targets) -> dict:
        """Спросить машины про занятость: id машины → (имя, ответ). Только сеть, без базы.

        Зовётся ДО `BEGIN IMMEDIATE` и в отдельном потоке. Два запрета сразу: внутри
        транзакции сеть трогать нельзя (`BEGIN IMMEDIATE` держит запись всей базы, и один
        молчащий AdsPower заморозил бы на свой таймаут весь кабинет), и в потоке цикла
        событий тоже нельзя — там то же ожидание встало бы поперёк всех остальных ручек.
        Сами запросы вдобавок кэшируются на секунды (`pub_adspower.active_profiles`):
        нода опрашивает кабинет чаще, чем меняется ответ, а предел запросов у машины
        общий с идущими заливками.
        """
        return {hid: (name, pub_adspower.active_profiles(url))
                for hid, url, name in targets}

    def _profile_verdict(r, hosts) -> tuple:
        """Одно задание глазами AdsPower: (занят посторонним — почему, не знаем — почему).

        Обе строки пустые — профиль точно свободен. Вторая заполнена — мы НЕ знаем, и
        это не то же самое, что «свободен»: по решению владельца незнание заливку не
        останавливает, но и молчать о нём нельзя.
        """
        if r["profile_id"] is None:
            return "", "у канала не выбран профиль AdsPower"
        shown = r["ads_serial"] or r["profile_id"]
        if r["ads_host"] is None:
            return "", f"профиль {shown} не привязан к машине — спрашивать некого"
        uid = (r["ads_uid"] or "").strip()
        if not uid:
            return "", (f"профиль {shown} ещё не сверен с AdsPower — нажмите "
                        f"«Обновить список профилей» у машины")
        known = hosts.get(r["ads_host"])
        if known is None:
            # Задание завелось между опросом машин и началом транзакции. Следующий
            # опрос ноды (через считанные секунды) спросит уже и про него.
            return "", f"профиль {shown}: занятость этой машины в этот раз не спрашивали"
        hname, act = known
        if not act.known:
            return "", f"AdsPower машины «{hname}» не ответил: {act.why}"
        if uid in act.user_ids:
            return (f"профиль {shown} сейчас открыт на машине «{hname}» не нами: "
                    f"жду, пока освободится, профиль не трогаю"), ""
        return "", ""

    def _profile_for_agent(c, profile_id):
        """Номер профиля, который понимает нода: серийный номер AdsPower.

        04.10.2026: из 10 заданий студии ни одно не залилось. Нода открывает AdsPower по
        полю `profile` как по серийному номеру (`upload_worker._ads_param`: цифры —
        serial_number), а студия отдавала туда СВОЙ номер строки `pub_profiles.id`. В
        старом кабинете таблицы профилей нет, и номер канала там и есть серийный; в
        студии они разошлись (канал «Tichá Obloha»: профиль 43 = AdsPower №24, а нода
        открыла №43 — чужой аккаунт, в переключателе другие каналы). Заливщик честно
        не находил канал и останавливался — ни один ролик не ушёл не туда.

        Строки в `pub_profiles` нет — номер вписан руками по-старому, это и есть серийный.
        """
        if profile_id is None:
            return None
        try:
            row = c.execute("SELECT serial FROM pub_profiles WHERE id=?",
                            (profile_id,)).fetchone()
        except sqlite3.OperationalError:
            row = None                      # база старше таблицы профилей
        if row is None:
            return profile_id
        return _agent_profile(row["serial"] if hasattr(row, "keys") else row[0])

    def _remember_block(c, r, why: str, unknown: str, now: float) -> None:
        """Запомнить или снять «задание стоит из-за чужого профиля».

        В журнал пишем только на переломе состояния. Нода опрашивает кабинет несколько
        раз в минуту, и строка на каждый опрос превратила бы журнал выпуска в ленту
        одинаковых сообщений, в которой не найти, что было до того.

        Состояния ТРИ, и третье — не оттенок второго (разбор 30.09.2026). «Занят
        посторонним», «точно свободен» и «спросить не смогли» — разные вещи. Снимать
        пометку и писать «профиль освободился» можно ТОЛЬКО когда мы знаем, что он
        свободен. Раньше сюда приходила одна строка «занят», и любое незнание —
        упавший AdsPower, машина не опрошена, профиль не сверён — выглядело как
        освобождение: пометка снималась, в журнал шло «профиль освободился», а тревога
        уходила владельцу как «ПОЧИНИЛОСЬ». То есть обрыв связи с машиной сам себя
        объявлял починкой ровно в ту минуту, когда посторонний держал профиль.
        """
        if not why and unknown:
            # Не знаем. Прежнее знание не трогаем: ни пометку, ни журнал. Задание при
            # этом выдаётся (решение владельца: незнание заливку не останавливает).
            return
        if why:
            if r["blocked_since"] is None:
                c.execute("UPDATE pub_upload_jobs SET blocked_since=?, blocked_why=?, "
                          "updated=? WHERE id=?", (now, why[:400], now, r["id"]))
                log_line(c, r["release_id"], "заливка", why, level="warn", now=now)
            elif (r["blocked_why"] or "") != why:
                c.execute("UPDATE pub_upload_jobs SET blocked_why=? WHERE id=?",
                          (why[:400], r["id"]))
        elif r["blocked_since"] is not None:
            c.execute("UPDATE pub_upload_jobs SET blocked_since=NULL, blocked_why=NULL, "
                      "updated=? WHERE id=?", (now, r["id"]))
            log_line(c, r["release_id"], "заливка",
                     "профиль освободился — задание снова можно выдавать", now=now)

    # ── ворота: ролик должен быть ТЕМ САМЫМ, который допустили ──

    def _stale_reason(c, row) -> str:
        """Чем ролик перестал быть принятой версией. Всё в порядке — пустая строка.

        Сверка идёт по опознанию, записанному в момент допуска (`pub_artifact`), а не
        по пути: путь у рендера постоянный, и пересобранный проект подменяет файл, не
        меняя в задании ни строчки. Читается при этом два мегабайта с краёв, а не весь
        ролик, — иначе каждая выдача задания упиралась бы в диск на минуты.

        У заданий, допущенных до появления опознания, сверять не с чем; молчим, а не
        объявляем их пересобранными: остановить исправный выпуск здесь так же плохо,
        как пропустить пересобранный, — готовый ролик встанет и будет ждать человека.
        """
        saved = pub_artifact.load(row["video_passport"])
        if not saved:
            return ""
        fresh = pub_artifact.passport(c, row["release_id"], row["video_path"])
        return pub_artifact.mismatch(saved, fresh)

    def _refuse_stale(c, row, why: str, now: float, was: str) -> str:
        """Снять задание с заливки и сказать человеку, почему. Возвращает текст отказа.

        Клейм НЕ обнуляем: нода могла держать это задание и доводить мастер до конца,
        и её поздний вердикт обязан доехать — по нему видно, ушёл ролик или нет.
        Статус `error` заодно защищает саму причину: поздний НЕуспех от ноды ложится
        как «already_error» и не перетирает объяснение (см. `upload_result`).
        """
        text = pub_artifact.refusal(why)
        c.execute("UPDATE pub_upload_jobs SET status='error', error=?, finished=?, "
                  "updated=? WHERE id=? AND status=?", (text, now, now, row["id"], was))
        log_line(c, row["release_id"], "заливка", text, level="error", now=now)
        # `stale`: повтор здесь не запрещён, он бессмыслен — на диске другой ролик, и
        # заливать его можно только после новой приёмки (решение владельца, п. 7).
        pub_upload_census.note(c, row["release_id"], text, now=now, stale=True)
        return text

    # ── взять задание ──
    @app.post("/api/upload/claim")
    async def upload_claim(request: Request):
        _auth(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        host = str(body.get("host") or "").strip()[:40] or "unknown"
        # 04.09.2026: машина заливки сообщает своё свободное место тем же опросом —
        # см. pub_calls.note_node_disk. Старый пуллер его не шлёт, и это не ошибка.
        if body.get("free_gb") is not None:
            try:
                from pub_calls import note_node_disk
                _c = db()
                try:
                    note_node_disk(_c, host, "upload", body.get("free_gb"),
                                   body.get("total_gb"))
                    _c.commit()
                finally:
                    _c.close()
            except Exception:                                      # noqa: BLE001
                pass
        # Рабочее место ноды. Без него одна машина смогла бы держать только одну заливку,
        # и правило «разные профили льются параллельно» не работало бы.
        worker = str(body.get("worker") or "1").strip()[:20]
        now = _now()

        c = db()
        try:
            # Спрашиваем AdsPower ДО транзакции и вне цикла событий — см. `_ask_hosts`.
            targets = _host_targets(c)
            hosts = await run_in_threadpool(_ask_hosts, targets) if targets else {}
            c.execute("BEGIN IMMEDIATE")
            # Живое знание о занятости ложится в справочник (`pub_profiles.active_now` и
            # `active_at`) — из него экран «Админ» и рисует «занят / свободен / не знаем».
            # Без этой записи карточка профилей знала бы только о ручной описи, то есть
            # почти всегда честно отвечала бы «не знаем». Молчащая машина не пишет ничего.
            for _hid, (_hname, _act) in hosts.items():
                pub_adspower.save_active(c, _hid, _act)
            # (3) Повторный запрос той же ноды — возвращаем её же задание с тем же токеном.
            mine = c.execute(
                "SELECT * FROM pub_upload_jobs WHERE status='running' AND host=? AND worker=? "
                "ORDER BY claimed LIMIT 1", (host, worker)).fetchone()
            if mine is not None:
                c.commit()
                return JSONResponse(_task_payload(c, mine, repeat=True))

            # Идёт выкатка: она ждёт, пока доливаются идущие заливки, и просит НОВЫХ не
            # выдавать (04.10.2026: при потоке заливок «ноль идущих» не наступал, и
            # выкатка ждала зря). Своё уже взятое задание нода получает и сейчас — выше.
            if _draining():
                c.commit()
                return JSONResponse({"task": None})

            # (2) Профили, по которым сейчас уже льют, — исключаем целиком.
            running = c.execute(
                "SELECT profile_id, uc FROM pub_upload_jobs WHERE status='running'").fetchall()
            busy_profiles = {r["profile_id"] for r in running if r["profile_id"] is not None}
            busy_channels = {r["uc"] for r in running}
            # Только задания ЖИВЫХ выпусков: отменённый или карантинный выпуск мог оставить
            # своё задание в очереди — заливать его значит заливать ролик без хозяина.
            #
            # Вторая ветка WHERE — задачи ДОВЕДЕНИЯ (п. 6 в шапке): задание в `repair` с
            # номером ролика. Выпуск тут может быть и в разборе (`blocked`): туда его
            # увёл как раз вердикт «залит, но не подтверждён», и доведение — способ из
            # разбора выйти машиной, а не руками. Подтверждённый руками выпуск закрыт
            # человеком — его не трогаем.
            rows = c.execute(
                "SELECT u.*, p.id ads_pid, p.serial ads_serial, p.adspower_user_id ads_uid, "
                "p.default_host_id ads_host, r.plan_item_id rel_plan_item "
                "FROM pub_upload_jobs u JOIN pub_releases r ON r.release_id=u.release_id "
                "LEFT JOIN pub_profiles p ON p.id=u.profile_id "
                "WHERE (u.status='queued' AND r.status IN ('pending','running')) "
                "OR (u.status='repair' AND COALESCE(u.video_id,'')!='' "
                "AND r.status IN ('pending','running','blocked') AND r.hand_confirmed IS NULL) "
                "ORDER BY u.slot_at, u.id").fetchall()
            task = None
            unknown_why = ""
            # Перебираем ВСЕ задания, даже найдя подходящее: пометка «стоит из-за чужого
            # профиля» должна быть верна у каждого, иначе задание, которое давно
            # освободилось, но до которого очередь не дошла, поднимет ложную тревогу
            # через полчаса.
            for r in rows:
                finalize = r["status"] == "repair"
                if finalize:
                    # Не раньше чем через 6 часов после прошлой попытки и не больше трёх;
                    # без обложки строгий режим заливщика черновик не доводит — такое
                    # задание не выдаём вовсе, им займётся человек (см. finalize_refusal).
                    if not finalize_due(r, now):
                        continue
                    if r["schedule_iso"] and r["slot_at"] <= now + 1800:
                        # Выход прошёл, пока ждали: переносим на ближайший свободный, как
                        # движок делает для очереди. Некуда — задание остаётся, но об
                        # этом сказано в журнале один раз, а не на каждый опрос.
                        iso = reslot_upload_job(c, r["id"], r["release_id"], r["channel_id"],
                                                r["rel_plan_item"], now, status="repair")
                        if not iso:
                            if (r["blocked_why"] or "") != FINALIZE_HOLD_SLOT:
                                c.execute("UPDATE pub_upload_jobs SET blocked_why=? WHERE id=?",
                                          (FINALIZE_HOLD_SLOT, r["id"]))
                                log_line(c, r["release_id"], "заливка", FINALIZE_HOLD_SLOT,
                                         level="warn", now=now)
                            continue
                        log_line(c, r["release_id"], "заливка",
                                 f"время выхода прошло, пока черновик ждал доведения — "
                                 f"перенёс выход на {iso}", now=now)
                # Задание могло состариться в очереди: запланировать прошедший момент
                # нельзя, отсеиваем ДО того, как нода начнёт качать файл.
                elif r["schedule_iso"] and r["slot_at"] <= now + 1800:
                    c.execute("UPDATE pub_upload_jobs SET status='error', error=?, "
                              "updated=? WHERE id=? AND status='queued'",
                              ("к времени выхода не успели — задание состарилось в очереди",
                               now, r["id"]))
                    log_line(c, r["release_id"], "заливка",
                             "задание состарилось в очереди — время выхода прошло",
                             level="error", now=now)
                    # Тоже `stale`: повторять нечего, время выхода уже прошло.
                    pub_upload_census.note(
                        c, r["release_id"], "время выхода прошло, пока стояло в очереди",
                        now=now, stale=True)
                    continue
                # (4а) Профиль без входа или канал без прав — на паузе до человека
                # (см. pub_engine.pause_scope): задание ждёт с причиной, как при чужом
                # профиле, и не жжёт попытки входа Google.
                paused = pause_get(c, "profile", r["profile_id"], now)
                scope = "profile" if paused else ""
                if not paused:
                    paused = pause_get(c, "channel", r["uc"], now)
                    scope = "channel" if paused else ""
                if paused:
                    _remember_block(c, r, pause_text(scope, paused, r["ads_serial"], r["uc"]),
                                    "", now)
                    continue
                if r["ads_pid"] is not None and _agent_profile(r["ads_serial"]) is None:
                    # Без номера AdsPower нода откроет профиль по нашему внутреннему
                    # номеру — то есть ЧУЖОЙ профиль (см. `_agent_profile`). Ждём сверки.
                    _remember_block(c, r, (
                        f"у профиля {r['profile_id']} нет номера AdsPower — нажмите "
                        "«Обновить список профилей» у машины; без номера машина открыла "
                        "бы чужой профиль"), "", now)
                    continue
                ours = r["profile_id"] in busy_profiles
                # (5) Чужим считаем профиль, только если он НЕ занят нами: наша же идущая
                # заливка держит его открытым в том же AdsPower, и без этой проверки мы
                # приняли бы себя за постороннего и объявили тревогу на ровном месте.
                busy_why, why_unknown = ("", "") if ours else _profile_verdict(r, hosts)
                _remember_block(c, r, busy_why, why_unknown, now)
                if busy_why or ours or r["uc"] in busy_channels:
                    continue
                if task is None:
                    # Сверяем ровно того кандидата, которого собираемся выдать: чтение
                    # файла идёт внутри `BEGIN IMMEDIATE`, а замок держит запись всей
                    # базы. Проверять так все задания очереди значило бы упереть кабинет
                    # в диск на каждом опросе ноды. Доведению сверка не нужна: файл с
                    # сервера не уходит, ролик уже на канале.
                    stale = "" if finalize else _stale_reason(c, r)
                    if stale:
                        _refuse_stale(c, r, stale, now, "queued")
                        continue
                    task = r
                    unknown_why = why_unknown
                    # Со следующего задания этот профиль и этот канал заняты нами.
                    # Пустой профиль в множество не кладём: иначе два задания без
                    # выбранного профиля стали бы мешать друг другу, чего раньше не было.
                    if r["profile_id"] is not None:
                        busy_profiles.add(r["profile_id"])
                    busy_channels.add(r["uc"])
            if task is None:
                c.commit()
                return JSONResponse({"task": None})

            token = "upc_" + secrets.token_urlsafe(18)
            finalize = task["status"] == "repair"
            if finalize:
                # Попытка считается с момента выдачи, а не с вердикта: если машина
                # замолчит, задание вернётся в `repair` по сроку, и следующая попытка
                # всё равно отсчитается от этой. Три молчания подряд — тоже три попытки.
                done = c.execute(
                    "UPDATE pub_upload_jobs SET status='running', claim_token=?, claimed=?, "
                    "host=?, worker=?, lease_until=?, blocked_since=NULL, blocked_why=NULL, "
                    "finished=NULL, finalize_tries=COALESCE(finalize_tries,0)+1, "
                    "finalize_last=?, updated=? WHERE id=? AND status='repair'",
                    (token, now, host, worker, now + UPLOAD_LEASE_SEC, now, now, task["id"]))
            else:
                done = c.execute(
                    # Пометку ожидания снимаем вместе с выдачей: она означает «стоит из-за
                    # чужого профиля», а выданное задание уже не стоит. Оставленная на
                    # работающем задании, она врала бы на экране и всплыла бы тревогой, если
                    # задание когда-нибудь вернётся в очередь.
                    "UPDATE pub_upload_jobs SET status='running', claim_token=?, claimed=?, "
                    "host=?, worker=?, lease_until=?, blocked_since=NULL, blocked_why=NULL, "
                    "updated=? WHERE id=? AND status='queued'",
                    (token, now, host, worker, now + UPLOAD_LEASE_SEC, now, task["id"]))
            if done.rowcount != 1:
                c.commit()                        # кто-то успел раньше — пусть спросит снова
                return JSONResponse({"task": None})
            c.commit()
            fresh = c.execute("SELECT * FROM pub_upload_jobs WHERE id=?", (task["id"],)).fetchone()
            if finalize:
                log_line(c, fresh["release_id"], "заливка",
                         f"выдал машине {host}, место {worker}, ДОВЕДЕНИЕ черновика "
                         f"{fresh['video_id']} по номеру (попытка {finalize_tries(fresh)} из "
                         f"{FINALIZE_MAX_TRIES}): файл не отдаю, заливщик найдёт строку в "
                         f"Studio и допишет расписание; профиль {fresh['profile_id']} "
                         f"(AdsPower №{_profile_for_agent(c, fresh['profile_id'])}), "
                         f"выход {fresh['schedule_iso'] or 'сразу'}", now=now)
            else:
                log_line(c, fresh["release_id"], "заливка",
                         f"выдал задание машине {host}, место {worker}, профиль "
                         f"{fresh['profile_id']} (AdsPower №"
                         f"{_profile_for_agent(c, fresh['profile_id'])}), "
                         f"выход {fresh['schedule_iso'] or 'сразу'}", now=now)
            if unknown_why:
                # Выдали, НЕ проверив занятость. Молчать об этом нельзя: если в профиль
                # в этот момент кто-то зашёл руками, разбираться с порванными куками
                # будут по этой строке журнала.
                log_line(c, fresh["release_id"], "заливка",
                         f"занятость профиля проверить не удалось ({unknown_why}) — "
                         f"задание выдано без проверки", level="warn", now=now)
            c.commit()          # запись журнала после commit иначе теряется при закрытии
            log(f"заливка: выдал задание {fresh['release_id']} → {host}/{worker}, "
                f"профиль {fresh['profile_id']}")
            return JSONResponse(_task_payload(c, fresh))
        finally:
            c.close()

    def _task_payload(c, row, repeat: bool = False) -> dict:
        fin = _finalizing(row)
        task = {
            "release_id": row["release_id"],
            "claim_token": row["claim_token"],
            "profile": _profile_for_agent(c, row["profile_id"]),
            "channel_url": row["channel_url"],
            "title": row["title"],
            "description": row["description"],
            "schedule": row["schedule_iso"] or "",
            "visibility": row["visibility"] or "public",
            "collaborator": row["collaborator"] or "",
            # У задачи доведения отпечатка файла нет: файл машине не нужен и не отдаётся.
            "video_sha256": "" if fin else (row["video_sha256"] or ""),
            "repeat": bool(repeat),
        }
        if fin:
            # Машина по этим двум полям не качает ролик, запускает заливщик с
            # `--finalize-draft --finalize-video-id` и строгим режимом; обложку качает
            # как обычно — строгий режим без неё черновик не доводит.
            task["finalize"] = True
            task["finalize_video_id"] = row["video_id"]
        return {"task": task}

    # ── удержанное окно: заливка оборвалась на «Verify it's you» до номера видео ──
    #
    # 17.10.2026. Восемь профилей на Минске стояли сутками с открытым мастером: файл залит,
    # шаг Details, поверх «Verify it's you», ссылки на видео нет. Заливщик по правилу
    # «не перезаливать» оставил браузер открытым (UPLOAD_IN_PROGRESS_PROFILE_RETAINED),
    # задание ушло в `repair` без номера видео — и дальше его не мог взять никто: пуллер
    # видит открытый профиль и ждёт, «вернуть в работу» запрещено (загрузка начиналась),
    # доведение черновика требует номер видео. Владелец: «работа ведётся для того, чтобы
    # это было не руками, а скриптом».
    #
    # Две ручки для машины заливки (общий секрет ноды, без клейма — клейма у такого
    # задания уже нет):
    #   • GET  /api/upload/retained?profile=&uc=  — чьё это окно: задание в `repair` без
    #     номера видео на этом профиле и канале;
    #   • POST /api/upload/{release_id}/retained_progress — машина прошла подтверждение
    #     в том же окне и прочитала номер видео: записываем его в задание. Статус остаётся
    #     `repair` — доведение черновика до расписания идёт отдельным шагом по номеру.
    # Ничего, кроме записи номера и фазы, здесь не происходит: файл не отправляется,
    # повтор заливки не разрешается, прежний номер не перетирается.
    def _retained_job(c, serial, uc):
        try:
            rows = c.execute(
                "SELECT u.*, p.serial ads_serial, r.status rel_status, r.hand_confirmed "
                "FROM pub_upload_jobs u JOIN pub_releases r ON r.release_id=u.release_id "
                "LEFT JOIN pub_profiles p ON p.id=u.profile_id "
                "WHERE (u.status='repair' OR (u.status='error' AND u.error LIKE 'AUTH_GATE%')) "
                "AND COALESCE(u.video_id,'')='' AND u.uc=? "
                "ORDER BY u.updated DESC", (uc,)).fetchall()
        except sqlite3.OperationalError:
            return None
        found = []
        for r in rows:
            if str(_agent_profile(r["ads_serial"]) if r["ads_serial"] else r["profile_id"]) != str(serial):
                continue
            if r["hand_confirmed"] or r["rel_status"] != "blocked":
                continue
            found.append(r)
        if not found:
            return None
        # Свежее — первым; сколько всего — машине важно: при двух и более окно не
        # атрибутировать по профилю и каналу, номер мог бы уйти чужому выпуску.
        first = dict(found[0])
        first["candidates"] = len(found)
        return first

    @app.get("/api/upload/retained")
    def upload_retained(request: Request, profile: str = "", uc: str = ""):
        _auth(request)
        profile, uc = str(profile or "").strip(), str(uc or "").strip()
        if not profile or not UC_RE.fullmatch(uc):
            raise HTTPException(400, "нужны profile и uc")
        c = db()
        try:
            r = _retained_job(c, profile, uc)
            if r is None:
                return JSONResponse({"release_id": None})
            return JSONResponse({"release_id": r["release_id"], "title": r["title"],
                                 "video_sha256": r["video_sha256"] or "",
                                 "candidates": r["candidates"],
                                 "error": (r["error"] or "")[:200]})
        finally:
            c.close()

    @app.post("/api/upload/{release_id}/retained_progress")
    async def upload_retained_progress(release_id: str, request: Request):
        _auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "жду JSON")
        profile = str(body.get("profile") or "").strip()
        uc = str(body.get("uc") or "").strip()
        phase = str(body.get("phase") or "")[:400]
        now = _now()
        c = db()
        try:
            rel = _release(c, release_id)
            if rel is None:
                raise HTTPException(404, "нет такого задания")
            ours = _retained_job(c, profile, uc)
            if ours is None or ours["release_id"] != release_id:
                # После того как машина донесла номер, задание уже не «без номера» — но
                # итоговую строку («черновик сохранён, профиль закрыт») от той же машины
                # по тому же профилю и каналу принимаем: это продолжение того же окна.
                same = None
                if rel["status"] == "repair" and rel["uc"] == uc and (rel["video_id"] or ""):
                    serial = c.execute("SELECT serial FROM pub_profiles WHERE id=?",
                                       (rel["profile_id"],)).fetchone()
                    agent = _agent_profile(serial["serial"]) if serial and serial["serial"] else rel["profile_id"]
                    same = rel if str(agent) == profile else None
                if same is None:
                    raise HTTPException(409, "это не удержанное задание этого профиля и канала")
            said, video_id = phase, ""
            try:
                parsed = json.loads(phase)
                if isinstance(parsed, dict):
                    said = str(parsed.get("phase") or "")
                    video_id = str(parsed.get("video_id") or "")
            except (ValueError, TypeError):
                pass
            if video_id and not VIDEO_ID_RE.fullmatch(video_id):
                raise HTTPException(400, "кривой номер видео")
            if video_id and (rel["video_id"] or "") and video_id != rel["video_id"]:
                raise HTTPException(409, "у задания уже другой номер видео")
            fields = ["phase=?", "heartbeat=?", "updated=?"]
            args = [phase or rel["phase"], now, now]
            if video_id:
                fields.append("video_id=?")
                args.append(video_id)
            c.execute(f"UPDATE pub_upload_jobs SET {', '.join(fields)} WHERE id=?",
                      args + [rel["id"]])
            log_line(c, release_id, "заливка",
                     f"удержанное окно: машина сообщает {said or 'фазу'}"
                     + (f", видео {video_id}" if video_id else ""), now=now)
            stdout = str(body.get("stdout") or "")
            if stdout:
                add_lines(c, release_id, "заливщик", stdout)
            c.commit()
            return JSONResponse({"ok": True, "video_id": video_id or rel["video_id"] or ""})
        finally:
            c.close()

    # 18.10.2026. Третья ручка того же окна: машина сама закрыла удержанное окно, которое
    # довести не смогла (мастер без номера видео после неудачного «Verify it's you»), и
    # сообщает об этом. Ролика на канале нет и не будет — выпуск возвращается заново как
    # «окно потеряно» (`pub_engine.revive(lost_window=True)`): та же дверь, что у кнопки
    # в пульте, с теми же защитами от второго ролика (номер есть — отказ, задание держит
    # машина — отказ). Чужой выпуск трогать нельзя: при двух и более удержанных заданиях
    # у канала окно по профилю и каналу не атрибутировать — возвращать кнопкой в пульте.
    @app.post("/api/upload/{release_id}/retained_lost")
    async def upload_retained_lost(release_id: str, request: Request):
        _auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "жду JSON")
        profile = str(body.get("profile") or "").strip()
        uc = str(body.get("uc") or "").strip()
        why = str(body.get("why") or "").strip()
        if not profile or not UC_RE.fullmatch(uc):
            raise HTTPException(400, "нужны profile и uc")
        now = _now()
        c = db()
        try:
            ours = _retained_job(c, profile, uc)
            if ours is None or ours["release_id"] != release_id:
                raise HTTPException(409, "это не удержанное задание этого профиля и канала")
            if ours["candidates"] > 1:
                raise HTTPException(409, "у канала несколько удержанных заданий без номера — "
                                         "вернуть кнопкой в пульте")
            log_line(c, release_id, "заливка",
                     f"удержанное окно закрыто пуллером: {why[:200] or 'причина не названа'} — "
                     "выпуск возвращён заново («окно потеряно»)", now=now)
            out = engine_revive(c, release_id, now, lost_window=True)
            if not out["ok"]:
                c.rollback()
                raise HTTPException(409, out["why"])
            c.commit()
            return JSONResponse({"ok": True, "release_id": release_id, "revived": True})
        finally:
            c.close()

    # ── забрать файлы ──
    @app.get("/api/upload/{release_id}/assets")
    def upload_assets(release_id: str, request: Request):
        _auth(request)
        c = db()
        try:
            rel = _release(c, release_id)
            if rel is None:
                raise HTTPException(404, "нет такого задания")
            _own(request, rel)
            return JSONResponse({"video": rel["video_path"], "cover": rel["cover_path"] or ""})
        finally:
            c.close()

    # ── скачать файл ──
    @app.get("/api/upload/{release_id}/file")
    def upload_file(release_id: str, kind: str, request: Request):
        """Отдать ролик или обложку.

        Минская нода чистит рабочую папку после рендера (проверено: там ноль файлов),
        поэтому готовый ролик живёт на сервере и его надо привезти обратно.
        """
        _auth(request)
        c = db()
        try:
            rel = _release(c, release_id)
            if rel is None:
                raise HTTPException(404, "нет такого задания")
            _own(request, rel)
            path = rel["video_path"] if kind == "video" else (rel["cover_path"] or "")
            if kind == "video" and _finalizing(rel):
                # Доведение по номеру: ролик уже на канале, второй экземпляр файла
                # машине не нужен — и отдать его значило бы дать повод залить снова.
                raise HTTPException(409, "задание на доведение черновика по номеру — "
                                         "файл ролика не отдаётся, он уже на канале")
            if kind == "video":
                # Последние ворота перед тем, как байты уйдут с сервера. Ворота на
                # выдаче задания стоят раньше, но между ними и этой секундой рендер
                # успевает закончиться и переписать файл: отдать его уже нельзя —
                # обратно из YouTube ролик не вернуть.
                stale = _stale_reason(c, rel)
                if stale:
                    now = _now()
                    text = _refuse_stale(c, rel, stale, now, "running")
                    c.commit()
                    raise HTTPException(409, text)
        finally:
            c.close()
        if not path or not os.path.exists(path):
            raise HTTPException(404, f"файла нет: {kind}")
        media = "video/mp4" if kind == "video" else "image/jpeg"
        return FileResponse(path, media_type=media, filename=os.path.basename(path))

    # ── биение и фазы ──
    @app.post("/api/upload/{release_id}/progress")
    async def upload_progress(release_id: str, request: Request):
        _auth(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        # Раньше здесь стояло [:40], и в базу попадал огрызок JSON вида
        # {"phase":"profile_opened","upload_phase_ — разобрать его нельзя, поэтому
        # движок не мог узнать, дошла ли машина до отправки файла, и считал любую
        # неудачу необратимой. Держим целиком: строка короткая, места не жалко.
        phase = str(body.get("phase") or "")[:400]
        now = _now()
        c = db()
        try:
            rel = _release(c, release_id)
            if rel is None:
                raise HTTPException(404, "нет такого задания")
            _own(request, rel)
            c.execute("UPDATE pub_upload_jobs SET phase=?, heartbeat=?, lease_until=?, updated=? "
                      "WHERE id=?", (phase or rel["phase"], now, now + UPLOAD_LEASE_SEC, now,
                                     rel["id"]))
            # Фазу дублируем в журнал выпуска: именно по нему движок понимает, что заливка
            # уже началась, и запрещает повтор навсегда.
            if phase:
                # Машина шлёт фазу целым JSON по контракту (`upload_contract`), а сравнение
                # ниже ждало голого имени: `phase == "upload_started"` не совпадало НИКОГДА,
                # и фаза «идёт заливка» в журнал выпуска не попадала ни разу. Разбираем имя;
                # старый формат (голая строка) продолжает работать.
                said = phase
                try:
                    parsed = json.loads(phase)
                    if isinstance(parsed, dict) and parsed.get("phase"):
                        said = str(parsed["phase"])
                except (ValueError, TypeError):
                    pass
                log_line(c, release_id, "заливка", f"машина сообщает: {said}", now=now)
                stdout = str(body.get("stdout") or "")
                if stdout:
                    add_lines(c, release_id, "заливщик", stdout)
                from pub_engine import log_phase
                known = {r[0] for r in c.execute(
                    "SELECT phase FROM pub_release_events WHERE release_id=?",
                    (release_id,)).fetchall()}
                if said == "upload_started" and "upload_running" not in known:
                    log_phase(c, release_id, "upload_running", now)
            c.commit()
            return JSONResponse({"ok": True})
        finally:
            c.close()

    # ── вердикт ──
    @app.post("/api/upload/{release_id}/result")
    async def upload_result(release_id: str, request: Request):
        _auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "жду результат заливки объектом JSON")
        now = _now()
        c = db()
        try:
            rel = _release(c, release_id)
            if rel is None:
                raise HTTPException(404, "нет такого задания")
            _own(request, rel)
            verdict = read_verdict(body, expected_uc=rel["uc"] or "",
                                   expected_iso=rel["schedule_iso"] or "")
            ok = verdict["kind"] in ("scheduled", "published")
            # `repair` среди принимаемых — ради позднего успеха доведения: задание,
            # снятое по молчанию во время доведения, возвращается в `repair` с клеймом,
            # и подтверждение Studio, пришедшее после, обязано его закрыть.
            if rel["status"] not in ("running", "error", "repair"):
                # Задание уже закрыто (двойная доставка) — не перетираем итог.
                return JSONResponse({"ok": True, "verdict": rel["verdict"] or "already"})
            if rel["status"] in ("error", "repair") and not ok:
                # Задание снято по молчанию; поздний НЕуспех ничего не меняет.
                return JSONResponse({"ok": True, "verdict": "already_error"})
            fin = _finalizing(rel)
            state, error = verdict["state"], verdict["error"]
            video_id = verdict["video_id"] or rel["video_id"] or ""
            if not ok and (rel["video_id"] or ""):
                # Номер ролика — улика, что он на канале. Ни один неуспех его не стирает,
                # и задание с номером не уходит в `error`: из `repair` его доведёт машина
                # или закроет человек «подтвердить руками», из `error` — никто.
                if verdict["video_id"] and verdict["video_id"] != rel["video_id"]:
                    log_line(c, release_id, "заливка",
                             f"машина назвала другой номер ролика ({verdict['video_id']}), "
                             f"чем записан ({rel['video_id']}) — оставляю записанный, "
                             f"нужен разбор", level="warn", now=now)
                video_id, state = rel["video_id"], "repair"
                if fin:
                    tries = finalize_tries(rel)
                    said = error or "заливщик не объяснил"
                    if tries >= FINALIZE_MAX_TRIES:
                        error = finalize_exhausted_reason(said)
                    else:
                        error = (f"доведение по номеру не удалось (попытка {tries} из "
                                 f"{FINALIZE_MAX_TRIES}, следующая не раньше чем через "
                                 f"{FINALIZE_GAP_SEC // 3600} ч): {said}")
            c.execute(
                "UPDATE pub_upload_jobs SET status=?, verdict=?, video_id=?, evidence=?, "
                "error=?, finished=?, updated=?, claim_token=NULL WHERE id=?",
                (state, verdict["kind"], video_id or None,
                 json.dumps(verdict["evidence"], ensure_ascii=False) if verdict["evidence"]
                 else rel["evidence"],
                 error, now, now, rel["id"]))
            if ok and fin:
                log_line(c, release_id, "заливка",
                         f"черновик {video_id} доведён до расписания по номеру "
                         f"(попытка {finalize_tries(rel)} из {FINALIZE_MAX_TRIES})", now=now)
            if ok:
                # Поздний успех: выпуск мог уже уехать в разбор — и по молчанию ноды
                # (задание в error), и по таймеру движка, пока задание ещё шло
                # (06.10.2026: ролик встал в расписание через 6 минут после разбора,
                # а выпуск так и висел «попытки исчерпаны»). Подтверждение Studio
                # сильнее любого нашего таймера — поднимаем выпуск.
                lifted = c.execute(
                    "UPDATE pub_releases SET status='running', blocked_reason=NULL, "
                    "deadline=?, updated=? WHERE release_id=? AND status='blocked'",
                    (now + 1800, now, release_id)).rowcount
                if lifted:
                    log_line(c, release_id, "заливка",
                             "ролик подтверждён после разбора: снимаю разбор", now=now)
            # Пауза профиля или канала: отказ, который чинит только человек (вход, права),
            # останавливает выдачу следующих выпусков туда же; успех паузу снимает.
            if ok:
                if pause_clear(c, "profile", rel["profile_id"], now):
                    log_line(c, release_id, "заливка",
                             "заливка прошла — пауза профиля снята", now=now)
                if pause_clear(c, "channel", rel["uc"], now):
                    log_line(c, release_id, "заливка",
                             "заливка прошла — пауза канала снята", now=now)
            elif verdict["kind"] == "failed" and not (rel["video_id"] or ""):
                scope = pause_scope(verdict["error"])
                ident = rel["profile_id"] if scope == "profile" else rel["uc"]
                if scope and ident:
                    until = pause_set(c, scope, ident, verdict["error"], release_id, now)
                    log_line(c, release_id, "заливка",
                             ("профиль" if scope == "profile" else "канал")
                             + f" ставлю на паузу до "
                             f"{time.strftime('%d.%m %H:%M', time.localtime(until))}: "
                             f"следующие выпуски туда не выдаю, пока человек не войдёт "
                             f"(«Вернуть в работу» снимает паузу)", level="warn", now=now)
            level = {"scheduled": "info", "published": "info"}.get(verdict["kind"], "error")
            if verdict["kind"] not in ("scheduled", "published"):
                # Класс берётся уликами, а не словом вердикта: `needs_repair` означает,
                # что ролик на канале есть, и это `sent`; `failed` до передачи файла —
                # `safe`. Разбирать текст ошибки на слова нельзя, он меняется.
                pub_upload_census.note(c, release_id,
                                       f"{verdict['kind']}: {verdict['error'] or 'без слов'}",
                                       now=now)
            log_line(c, release_id, "заливка",
                     f"вердикт: {verdict['kind']}"
                     + (f", видео {verdict['video_id']}" if verdict["video_id"] else "")
                     + (f" — {verdict['error']}" if verdict["error"] else ""),
                     level=level, now=now)
            if body.get("stdout"):
                add_lines(c, release_id, "заливщик", str(body["stdout"]), level=level)
            c.commit()
            log(f"заливка: вердикт {release_id} → {verdict['kind']}")
            return JSONResponse({"ok": True, "verdict": verdict["kind"]})
        finally:
            c.close()


# ─────────────────────────── разбор вердикта ───────────────────────────

def _iso_epoch(iso: str):
    """ISO-время задания → целые секунды. Не разобралось — None (сверку пропускаем)."""
    import datetime as _dt
    try:
        return int(_dt.datetime.fromisoformat(iso).timestamp())
    except Exception:
        return None


def read_verdict(body: dict, expected_uc: str = "", expected_iso: str = "") -> dict:
    """Ответ заливщика → однозначный исход.

    Успех требует ВСЁ разом: точное подтверждение Studio (не строковое из списка),
    `success=true` от самого заливщика, совпадение канала и момента выхода с заданием.
    Любое расхождение — «нужен разбор»: ролик, скорее всего, на канале, но не там или
    не тогда, и повторная заливка запрещена.
    """
    if not isinstance(body, dict):
        return {"kind": "broken", "state": "error", "video_id": "", "evidence": None,
                "error": "ответ не объект"}
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    video_id = str(result.get("video_id") or "")
    exact = result.get("exact_studio_evidence")
    evidence = exact or result.get("studio_row_evidence")
    state = ""
    if isinstance(evidence, dict):
        state = str(evidence.get("studio_state") or "")
    error = str(result.get("error") or "")[:400]
    no_reupload = bool(result.get("do_not_reupload"))
    success = bool(result.get("success"))

    def _mismatch() -> str:
        if not isinstance(exact, dict):
            return "нет точного подтверждения Studio (только строковое)"
        if not success:
            return "заливщик сам не считает итог успехом"
        got_uc = str(exact.get("verified_channel_uc") or "")
        if expected_uc and got_uc and got_uc != expected_uc:
            return f"канал в подтверждении {got_uc} не совпал с заданием"
        want = _iso_epoch(expected_iso) if expected_iso else None
        got = exact.get("scheduled_time_seconds")
        if want is not None and isinstance(got, int) and got != want:
            return "время в подтверждении не совпало с заданием"
        return ""

    if state in OK_STATES:
        why = _mismatch()
        if why:
            return {"kind": "needs_repair", "state": "repair", "video_id": video_id,
                    "evidence": evidence, "error": f"SCHEDULED, но {why}"}
        return {"kind": "scheduled", "state": "done", "video_id": video_id,
                "evidence": evidence, "error": ""}
    if state in PUBLISHED_STATES:
        return {"kind": "published", "state": "done", "video_id": video_id,
                "evidence": evidence, "error": ""}
    if video_id or no_reupload:
        # Ролик на канале есть, но подтверждения нет: повторять заливку нельзя.
        return {"kind": "needs_repair", "state": "repair", "video_id": video_id,
                "evidence": evidence,
                "error": error or f"ролик залит, но Studio показывает «{state or 'неизвестно'}»"}
    return {"kind": "failed", "state": "error", "video_id": "", "evidence": evidence,
            "error": error or "заливка не удалась"}


def sweep_stuck(c, now: float) -> list:
    """Задания, от которых нет вестей дольше аренды, — В КАРАНТИН, не назад в очередь.

    Возврат в очередь здесь означал бы второй ролик на канале: нода могла в этот момент
    продолжать заливку, просто молча.
    """
    rows = c.execute(
        "SELECT * FROM pub_upload_jobs WHERE status='running' AND lease_until IS NOT NULL "
        "AND lease_until < ?", (now,)).fetchall()
    out = []
    for r in rows:
        if _finalizing(r):
            # Доведение по номеру: обратно в `repair`, не в `error` — номер ролика
            # должен остаться виден и следующей попытке, и человеку. Попытка уже
            # сосчитана при выдаче; следующая — не раньше чем через 6 часов.
            tries = finalize_tries(r)
            said = (f"машина молчит дольше {UPLOAD_LEASE_SEC // 60} минут при доведении "
                    f"черновика по номеру")
            text = (finalize_exhausted_reason(said) if tries >= FINALIZE_MAX_TRIES else
                    f"{said} (попытка {tries} из {FINALIZE_MAX_TRIES}, следующая не раньше "
                    f"чем через {FINALIZE_GAP_SEC // 3600} ч)")
            c.execute("UPDATE pub_upload_jobs SET status='repair', error=?, finished=?, "
                      "updated=? WHERE id=?", (text, now, now, r["id"]))
            pub_upload_census.note(c, r["release_id"], said, now=now, silent=True)
            out.append(r["release_id"])
            continue
        # claim_token СОХРАНЯЕМ: нода могла продолжать молча и довести мастер до конца —
        # её поздний вердикт с этим клеймом обязан доехать и поднять запись до успеха.
        c.execute(
            "UPDATE pub_upload_jobs SET status='error', error=?, finished=?, updated=? "
            "WHERE id=?",
            (f"нода молчит дольше {UPLOAD_LEASE_SEC // 60} минут — нужен разбор",
             now, now, r["id"]))
        # `silent` и только он: фаз у замолчавшей ноды нет, и по фазам такой срыв
        # выглядит безопасным. Она могла продолжать загрузку молча.
        pub_upload_census.note(c, r["release_id"],
                               f"нода молчит дольше {UPLOAD_LEASE_SEC // 60} минут",
                               now=now, silent=True)
        out.append(r["release_id"])
    return out
