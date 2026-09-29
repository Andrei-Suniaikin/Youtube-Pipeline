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
"""

from __future__ import annotations

import json
import os
import secrets

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

import pub_adspower
import pub_artifact
from pub_log import add as log_line, add_lines

# Сколько ждать вестей от ноды, прежде чем считать заливку зависшей.
# С запасом на самый долгий наблюдавшийся мастер Studio (~9 минут на больном прокси).
UPLOAD_LEASE_SEC = 45 * 60

# Успешные исходы Studio. Всё остальное — не успех, даже если video_id получен.
OK_STATES = ("SCHEDULED",)
PUBLISHED_STATES = ("PUBLIC",)


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
            "WHERE u.status='queued' AND p.default_host_id IS NOT NULL").fetchall()
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

            # (2) Профили, по которым сейчас уже льют, — исключаем целиком.
            running = c.execute(
                "SELECT profile_id, uc FROM pub_upload_jobs WHERE status='running'").fetchall()
            busy_profiles = {r["profile_id"] for r in running if r["profile_id"] is not None}
            busy_channels = {r["uc"] for r in running}
            # Только задания ЖИВЫХ выпусков: отменённый или карантинный выпуск мог оставить
            # своё задание в очереди — заливать его значит заливать ролик без хозяина.
            rows = c.execute(
                "SELECT u.*, p.serial ads_serial, p.adspower_user_id ads_uid, "
                "p.default_host_id ads_host FROM pub_upload_jobs u JOIN pub_releases r "
                "ON r.release_id=u.release_id AND r.status IN ('pending','running') "
                "LEFT JOIN pub_profiles p ON p.id=u.profile_id "
                "WHERE u.status='queued' ORDER BY u.slot_at, u.id").fetchall()
            task = None
            unknown_why = ""
            # Перебираем ВСЕ задания, даже найдя подходящее: пометка «стоит из-за чужого
            # профиля» должна быть верна у каждого, иначе задание, которое давно
            # освободилось, но до которого очередь не дошла, поднимет ложную тревогу
            # через полчаса.
            for r in rows:
                # Задание могло состариться в очереди: запланировать прошедший момент
                # нельзя, отсеиваем ДО того, как нода начнёт качать файл.
                if r["schedule_iso"] and r["slot_at"] <= now + 1800:
                    c.execute("UPDATE pub_upload_jobs SET status='error', error=?, "
                              "updated=? WHERE id=? AND status='queued'",
                              ("к времени выхода не успели — задание состарилось в очереди",
                               now, r["id"]))
                    log_line(c, r["release_id"], "заливка",
                             "задание состарилось в очереди — время выхода прошло",
                             level="error", now=now)
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
                    # в диск на каждом опросе ноды.
                    stale = _stale_reason(c, r)
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
            log_line(c, fresh["release_id"], "заливка",
                     f"выдал задание машине {host}, место {worker}, профиль "
                     f"{fresh['profile_id']}, выход {fresh['schedule_iso'] or 'сразу'}", now=now)
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
        return {"task": {
            "release_id": row["release_id"],
            "claim_token": row["claim_token"],
            "profile": row["profile_id"],
            "channel_url": row["channel_url"],
            "title": row["title"],
            "description": row["description"],
            "schedule": row["schedule_iso"] or "",
            "visibility": row["visibility"] or "public",
            "collaborator": row["collaborator"] or "",
            "video_sha256": row["video_sha256"] or "",
            "repeat": bool(repeat),
        }}

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
            if rel["status"] not in ("running", "error"):
                # Задание уже закрыто (двойная доставка) — не перетираем итог.
                return JSONResponse({"ok": True, "verdict": rel["verdict"] or "already"})
            if rel["status"] == "error" and verdict["kind"] not in ("scheduled", "published"):
                # Задание снято по молчанию; поздний НЕуспех ничего не меняет.
                return JSONResponse({"ok": True, "verdict": "already_error"})
            c.execute(
                "UPDATE pub_upload_jobs SET status=?, verdict=?, video_id=?, evidence=?, "
                "error=?, finished=?, updated=?, claim_token=NULL WHERE id=?",
                (verdict["state"], verdict["kind"], verdict["video_id"],
                 json.dumps(verdict["evidence"], ensure_ascii=False) if verdict["evidence"] else None,
                 verdict["error"], now, now, rel["id"]))
            if rel["status"] == "error" and verdict["kind"] in ("scheduled", "published"):
                # Поздний успех живой ноды: выпуск мог уже уехать в разбор — поднимаем его.
                c.execute("UPDATE pub_releases SET status='running', blocked_reason=NULL, "
                          "updated=? WHERE release_id=? AND status='blocked'",
                          (now, release_id))
                log_line(c, release_id, "заливка",
                         "нода домолчалась до успеха: ролик подтверждён, снимаю разбор",
                         now=now)
            level = {"scheduled": "info", "published": "info"}.get(verdict["kind"], "error")
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
        # claim_token СОХРАНЯЕМ: нода могла продолжать молча и довести мастер до конца —
        # её поздний вердикт с этим клеймом обязан доехать и поднять запись до успеха.
        c.execute(
            "UPDATE pub_upload_jobs SET status='error', error=?, finished=?, updated=? "
            "WHERE id=?",
            (f"нода молчит дольше {UPLOAD_LEASE_SEC // 60} минут — нужен разбор",
             now, now, r["id"]))
        out.append(r["release_id"])
    return out
