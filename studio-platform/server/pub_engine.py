#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Движок выпусков: ведёт строку плана до ролика в расписании YouTube без человека.

Один тик делает две вещи:
  1) заводит выпуски для строк, которым пора начинать (слот минус «готовить заранее»);
  2) двигает уже заведённые выпуски на одну фазу вперёд.

Правила, ради которых всё и написано:

* **Никаких двух роликов.** Выпуск на версию строки плана ровно один — это уникальный
  индекс в базе, а не проверка в коде. После первого необратимого шага (началась загрузка
  в браузере) повторить заливку нельзя: только продолжить или увести в карантин.
* **Падение не теряет работу.** Каждая фаза пишется в журнал. Перезапуск сервиса продолжает
  с последней подтверждённой фазы.
* **Зависание — это отказ, а не работа.** У фазы есть срок. Живой, но не двигающийся
  процесс снимается по сроку: мёртвый прокси даёт белый экран и девять минут тишины
  вообще без ошибки.
* **Отказ общего ресурса не ломает строки.** Кончились кредиты или лёг сосед — очередь
  этого ресурса встаёт целиком, строки остаются на месте и продолжат сами.

Внешние сервисы приходят объектом `services`, поэтому весь движок проверяется тестами
без единого сетевого вызова. Обработчику передаётся ТО ЖЕ соединение с базой: своё он
открывать не должен — тик держит незакрытую запись, и второе соединение встало бы
на блокировке до самого таймаута.
"""

from __future__ import annotations

import json
import re
import secrets
import time

import preset_links
from block_reasons import COVER_PRESET_MISSING
from pub_log import add as log_line
from pub_publish import schedule_iso

# Порядок фаз. Двигаться можно только вперёд и только на одну ступень.
PHASES = (
    "created",         # выпуск заведён
    "source_ready",    # текст готов (рерайт)
    "project_ready",   # проект в VideoStudio создан
    "media_ready",     # озвучка, картинки и рендер готовы
    "cover_ready",     # обложка готова
    "upload_pending",  # комплект отдан машине с профилем
    "upload_running",  # НЕОБРАТИМО: загрузка в браузере началась
    "scheduled",       # Studio подтвердила: стоит в расписании
    "published",       # вышел и подтверждён как публичный
)
PHASE_INDEX = {p: i for i, p in enumerate(PHASES)}

# С этой фазы повторная заливка запрещена навсегда.
IRREVERSIBLE_FROM = PHASE_INDEX["upload_running"]

# Сколько ждать прогресса фазы, прежде чем считать её зависшей (секунды).
PHASE_DEADLINES = {
    "created": 600,
    "source_ready": 3 * 3600,
    "project_ready": 900,
    "media_ready": 6 * 3600,
    "cover_ready": 3600,
    "upload_pending": 2 * 3600,
    "upload_running": 1800,
    "scheduled": 900,
}
DEFAULT_DEADLINE = 3600

MAX_ATTEMPTS = 3


class ResourceDown(Exception):
    """Отказ ОБЩЕГО ресурса: кредиты, сосед, машина заливки.

    Строку в карантин не уводим — встаёт очередь этого ресурса целиком, потому что
    иначе один эпизод исчерпанных кредитов отправит в карантин весь операционный день.
    """

    def __init__(self, resource: str, message: str):
        super().__init__(message)
        self.resource = resource


class ItemFailed(Exception):
    """Отказ, специфичный для этой строки: битый донор, отказ модерации, чужой канал."""


def new_release_id() -> str:
    return "r_" + secrets.token_hex(5)


# ─────────────────────────── журнал фаз ───────────────────────────

def log_phase(c, release_id: str, phase: str, now: float, video_id: str = "",
              payload=None) -> int:
    """Записать фазу. Номер растёт монотонно, повтор той же фазы не создаёт дубля.

    До 19.09.2026 докстринг это обещал, а код делал ровно обратное: INSERT шёл
    безусловно. Цена видна в бою — движок пишет `resource_wait` каждые 30 секунд по
    каждому стоящему выпуску, и при 187 ждущих это 530 тысяч строк в сутки; база
    кабинета доросла до 1,64 ГБ, ночной архив и уборка тянут её целиком.

    Теперь подряд идущий повтор ТОЙ ЖЕ фазы (та же фаза, тот же ролик, тот же payload)
    не создаёт строку: растёт `repeats` и обновляется `last_seen`. Смысл журнала от
    этого только полнее — `created` отвечает «с какого времени ждём», `last_seen` —
    «когда проверяли в последний раз», а раньше на этот вопрос отвечала стена
    одинаковых строк. Номер `seq` сохраняется прежний: на него ссылаются читатели
    (`phase_history`, экран выпуска, поздняя проверка публикации).

    Повтор именно ПОДРЯД ИДУЩИЙ: `created → resource_wait → created` — три события,
    потому что между ними фаза менялась. Схлопывается только «то же самое опять».
    """
    body = json.dumps(payload, ensure_ascii=False) if payload else None
    last = c.execute(
        "SELECT seq, phase, video_id, payload FROM pub_release_events "
        "WHERE release_id=? ORDER BY seq DESC LIMIT 1", (release_id,)).fetchone()
    if last and last[1] == phase and (last[2] or "") == (video_id or "") \
            and (last[3] or None) == body:
        c.execute("UPDATE pub_release_events SET repeats=repeats+1, last_seen=? "
                  "WHERE release_id=? AND seq=?", (now, release_id, last[0]))
        return last[0]
    seq = (last[0] if last else 0) + 1
    c.execute(
        "INSERT INTO pub_release_events(release_id,seq,phase,video_id,payload,created,"
        "repeats,last_seen) VALUES(?,?,?,?,?,?,1,?)",
        (release_id, seq, phase, video_id or None, body, now, now))
    return seq


def phase_history(c, release_id: str) -> list:
    return [r["phase"] for r in c.execute(
        "SELECT phase FROM pub_release_events WHERE release_id=? ORDER BY seq",
        (release_id,)).fetchall()]


# Что машина успела сообщить до того, как файл пошёл в YouTube. На этих фазах повтор
# безопасен: браузер только открылся, загрузка ещё не начиналась.
MACHINE_PRE_UPLOAD = ("preflight", "profile_opened")


def reached_irreversible(c, release_id: str) -> bool:
    """Была ли уже начата загрузка файла в YouTube.

    Спрашиваем сначала саму машину: она отмечает свои фазы по ходу работы. Наша фаза
    «идёт заливка» ставится в момент ВЫДАЧИ задания, то есть ещё до того, как браузер
    что-либо отправил, — и если машина отказалась (не смогла прочитать состояние канала,
    например), выпуск навсегда числился «загрузка уже начиналась, повтор запрещён»,
    хотя файл не отправлялся ни разу и повтор был безопасен.
    """
    row = c.execute("SELECT phase, video_id FROM pub_upload_jobs WHERE release_id=?",
                    (release_id,)).fetchone()
    # YouTube уже выдал идентификатор ролику: это безусловная улика начавшейся
    # загрузки, даже если машинная фаза записалась старой/обрезанной. Повторять нельзя.
    if row and row["video_id"]:
        return True
    if row and row["phase"]:
        try:
            said = str((json.loads(row["phase"]) or {}).get("phase") or "")
        except Exception:
            # Старые записи лежат обрезанными до сорока символов и как JSON не читаются.
            # Имя фазы стоит в них первым, поэтому достаём его прямо из строки: иначе
            # выпуски, залитые до починки обрезки, так и остались бы «необратимыми».
            found = re.search(r'"phase"\s*:\s*"([a-z_]+)"', str(row["phase"]))
            said = found.group(1) if found else ""
        if said in MACHINE_PRE_UPLOAD:
            return False        # машина до отправки файла не дошла — повторить можно
    elif row and not row["phase"]:
        # Задание машине выдали, но она не успела сообщить ни одной фазы (упала связь,
        # умер браузер). Фазы идут ДО отправки файла, поэтому их отсутствие означает,
        # что в YouTube ничего не ушло — повтор безопасен.
        return False
    for phase in phase_history(c, release_id):
        if PHASE_INDEX.get(phase, -1) >= IRREVERSIBLE_FROM:
            return True
    return False



def reflow_slots(c, now: float) -> int:
    """Раздать освободившиеся выходы строкам, оставшимся без даты.

    Строка без даты — это ссылка, которой не хватило места в горизонте (или её вставили,
    когда у канала ещё не было расписания). Раньше такая строка не оживала никогда: дату
    назначал только момент вставки, и обещание «займёт выход, как только он освободится»
    держать было нечем — ссылка молча висела вечно, а в календаре её даже не видно.

    Теперь каждый тик добирает свободные выходы горизонта и отдаёт их ожидающим строкам
    по порядку очереди. Возвращает, сколько строк получили дату.
    """
    import datetime as dt
    import json as _json
    from channel_slots import upcoming_slots, ScheduleError

    waiting = c.execute(
        "SELECT DISTINCT channel_id FROM pub_plan_items "
        "WHERE slot_at IS NULL AND status='planned'").fetchall()
    if not waiting:
        return 0

    moment = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    placed = 0
    for row in waiting:
        cid = row["channel_id"]
        ch = c.execute("SELECT * FROM pub_channels_live WHERE id=? AND status='active'",
                       (cid,)).fetchone()
        if not ch:
            continue                    # канал на паузе или в архиве — не его время
        try:
            schedule = _json.loads(ch["schedule"] or "{}")
            if not schedule:
                continue                # расписания нет — раздавать нечего
            slots = upcoming_slots(schedule, ch["tz"], int(ch["horizon_days"] or 3), moment)
        except (ScheduleError, ValueError):
            continue                    # кривое расписание не должно валить весь тик
        busy = {round(r["slot_at"]) for r in c.execute(
            "SELECT slot_at FROM pub_plan_items WHERE channel_id=? AND slot_at IS NOT NULL "
            "AND status IN ('planned','frozen','running','done','error')", (cid,)).fetchall()
            if r["slot_at"]}
        free = [s for s in slots if round(s.timestamp()) not in busy]
        if not free:
            continue
        items = c.execute(
            "SELECT id FROM pub_plan_items WHERE channel_id=? AND slot_at IS NULL "
            "AND status='planned' ORDER BY position, id", (cid,)).fetchall()
        for item, slot in zip(items, free):
            stamp = round(slot.timestamp())
            # Гонку с параллельной вставкой ловим условием в самом UPDATE: занятый
            # тем временем выход просто не отдастся второй раз (уникальный индекс).
            try:
                done = c.execute(
                    "UPDATE pub_plan_items SET slot_at=?, updated=? "
                    "WHERE id=? AND slot_at IS NULL AND status='planned'",
                    (stamp, now, item["id"]))
            except Exception:
                continue                # выход занят другим — следующая строка возьмёт свой
            if done.rowcount == 1:
                placed += 1
    return placed



def reslot_pending_uploads(c, now: float) -> int:
    """Перенести выходы у заданий заливки, до которых уже не успеть.

    Задание лежит в очереди, пока за ним не придёт машина с нужным профилем. Если за
    это время назначенный выход прошёл, YouTube такое расписание не примет — раньше
    задание просто помечалось ошибкой, и готовый ролик пропадал, хотя всё, что ему
    нужно, — другое время. Двигаем сами: ролик выйдет позже, но выйдет.
    """
    import datetime as dt
    import json as _json
    from channel_slots import upcoming_slots, ScheduleError

    rows = c.execute(
        "SELECT u.id, u.release_id, u.channel_id, u.slot_at, r.plan_item_id "
        "FROM pub_upload_jobs u JOIN pub_releases r ON r.release_id=u.release_id "
        "WHERE u.status='queued' AND u.schedule_iso!='' AND u.slot_at <= ?",
        (now + 2400,)).fetchall()
    moved = 0
    for row in rows:
        ch = c.execute("SELECT * FROM pub_channels_live WHERE id=?", (row["channel_id"],)).fetchone()
        if not ch:
            continue
        try:
            schedule = _json.loads(ch["schedule"] or "{}")
            if not schedule:
                continue
            slots = upcoming_slots(schedule, ch["tz"], int(ch["horizon_days"] or 3),
                                   dt.datetime.fromtimestamp(now, dt.timezone.utc))
        except (ScheduleError, ValueError):
            continue
        busy = {round(r["slot_at"]) for r in c.execute(
            "SELECT slot_at FROM pub_plan_items WHERE channel_id=? AND slot_at IS NOT NULL "
            "AND status IN ('planned','frozen','running','done','error')",
            (row["channel_id"],)).fetchall() if r["slot_at"]}
        for point in slots:
            stamp = round(point.timestamp())
            if stamp in busy or point.timestamp() <= now + 2400:
                continue
            iso = schedule_iso(stamp, ch["tz"])
            c.execute("UPDATE pub_upload_jobs SET slot_at=?, schedule_iso=?, updated=? "
                      "WHERE id=? AND status='queued'", (stamp, iso, now, row["id"]))
            c.execute("UPDATE pub_plan_items SET slot_at=?, updated=? WHERE id=?",
                      (stamp, now, row["plan_item_id"]))
            c.execute("UPDATE pub_releases SET slot_at=?, updated=? WHERE release_id=?",
                      (stamp, now, row["release_id"]))
            log_line(c, row["release_id"], "заливка",
                     f"к прежнему времени машина не успела — перенёс выход на {iso}", now=now)
            moved += 1
            break
    return moved


# ─────────────────────────── заведение выпусков ───────────────────────────

def due_items(c, now: float) -> list:
    """Строки плана, которым пора начинать: слот минус «готовить заранее» уже наступил."""
    return c.execute(
        "SELECT p.*, ch.lead_days lead_days, ch.status ch_status, ch.uc ch_uc "
        "FROM pub_plan_items p JOIN pub_channels_live ch ON ch.id=p.channel_id "
        "WHERE p.status='planned' AND p.slot_at IS NOT NULL AND ch.status='active' "
        "AND p.slot_at - COALESCE(ch.lead_days,2)*86400 <= ? "
        "ORDER BY p.slot_at", (now,)).fetchall()


# Пункт 6.1 ТЗ: канал без ID YouTube заводить можно, а выпускать на нём — нет, иначе
# готовому ролику некуда будет заливаться. Текст попадает в заметку строки плана —
# колонку, которую сегодня больше никто не занимает (в отличие от полей канала, у строк
# плана нет собственного постоянного места для причины остановки).
NO_UC_REASON = "у канала не задан ID YouTube — выпуск не заводится"


def materialize(c, now: float) -> list:
    """Завести выпуски для созревших строк. Повторный вызов новых не создаёт."""
    made = []
    for item in due_items(c, now):
        if not item["ch_uc"]:
            # Строка остаётся ждать (status не меняется) — как только владелец впишет
            # ID во вкладке «Настройки» канала в пульте (`pub_ui.set_channel_uc`,
            # дополнение 16.09, вопрос 1), следующий тик заведёт выпуск как обычно.
            # Другие строки этот канал не блокирует: цикл идёт дальше.
            if item["note"] != NO_UC_REASON:
                c.execute("UPDATE pub_plan_items SET note=?, updated=? WHERE id=?",
                          (NO_UC_REASON, now, item["id"]))
            continue
        rid = new_release_id()
        try:
            c.execute(
                "INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
                "channel_id,uc,slot_at,deadline,created,updated) "
                "SELECT ?,?,?,?,?,ch.uc,?,?,?,? FROM pub_channels ch WHERE ch.id=?",
                (rid, item["tenant_id"], item["id"], item["version"], item["channel_id"],
                 item["slot_at"], now + PHASE_DEADLINES["created"], now, now,
                 item["channel_id"]))
        except Exception as exc:
            if "UNIQUE" in str(exc):
                continue          # выпуск на эту версию строки уже есть — так и задумано
            raise
        # Доработка ревьюера (code review, итерация 2, Issue 4): если строка раньше
        # ждала из-за пустого ID, заметка с причиной NO_UC_REASON устаревает в момент
        # заведения выпуска — стираем её, но только если там стоит РОВНО эта причина,
        # чтобы не затереть чужую заметку (например, вписанную человеком вручную).
        cas = c.execute(
            "UPDATE pub_plan_items SET status='frozen', frozen=1, updated=?, "
            "note=(CASE WHEN note=? THEN NULL ELSE note END) "
            "WHERE id=? AND status='planned'", (now, NO_UC_REASON, item["id"]))
        if cas.rowcount != 1:
            # Строку отменили между выборкой и заморозкой — выпуск не нужен.
            c.execute("DELETE FROM pub_releases WHERE release_id=?", (rid,))
            continue
        log_phase(c, rid, "created", now)
        log_line(c, rid, "движок",
                 f"взял в работу ссылку {item['donor_url'][:80]} — выход через "
                 f"{max(0, int((item['slot_at'] - now) / 3600))} ч", now=now)
        made.append(rid)
    return made


# ─────────────────────────── продвижение ───────────────────────────

def channel_presets(c, preset_vsgen_id, preset_rewrite="", preset_cover="") -> tuple:
    """Какими пресетами делаются текст и обложка. Решает ПРЕСЕТ ПРОИЗВОДСТВА.

    Решение владельца 09.09.2026: канал указывает на один пресет, и текст с обложкой
    берутся оттуда. До этого канал держал три ссылки на три сервиса, и один ролик
    настраивался в трёх местах — ровно то, что ТЗ §04 просит свести в одно.

    Колонки канала остаются как запасной путь: у каналов, заведённых раньше, пресет
    производства про соседей ещё ничего не знает, и молча обнулить им текст и обложку
    значило бы остановить их выпуски. Пустое поле в пресете — это «не задано», а не
    «убрать»: у нас нет способа отличить одно от другого, а цена ошибки разная.

    Читает `preset_links.resolve`, а не `preset_settings` напрямую (батч «Пресеты целые
    и частичные»): «Сценарий» и «Обложка» — тоже группы пресета (с 18.09.2026 разные),
    и если у любой из них стоит ссылка на частичный пресет, `preset_rewrite` или
    `preset_cover` обязан прийти оттуда, а не из своего (пустого) поля целого.
    """
    s = preset_links.resolve(c, preset_vsgen_id)
    rew = str(s.get("preset_rewrite") or "").strip() or (preset_rewrite or "")
    cov = str(s.get("preset_cover") or "").strip() or (preset_cover or "")
    return rew, cov


def live_releases(c) -> list:
    rows = c.execute(
        "SELECT r.*, ch.name ch_name, ch.tz tz, ch.profile_id profile_id, ch.host_id host_id, "
        "ch.preset_rewrite preset_rewrite, ch.preset_vsgen_id preset_vsgen_id, "
        "ch.preset_cover preset_cover, ch.collaborator collaborator, ch.lang lang, "
        "ch.deferred deferred, ch.deferred_limit deferred_limit, "
        "p.donor_url donor_url, p.donor_title donor_title "
        "FROM pub_releases r JOIN pub_channels_live ch ON ch.id=r.channel_id "
        "JOIN pub_plan_items p ON p.id=r.plan_item_id "
        "WHERE r.status IN ('pending','running') ORDER BY r.slot_at").fetchall()
    # Строки превращаются в словари: значения пресетов подменяются, а sqlite3.Row
    # неизменяем. Читатели обращаются по имени (`rel["preset_cover"]`) — для словаря
    # это работает так же.
    out = []
    for r in rows:
        d = dict(r)
        d["preset_rewrite"], d["preset_cover"] = channel_presets(
            c, d.get("preset_vsgen_id"), d.get("preset_rewrite") or "",
            d.get("preset_cover") or "")
        out.append(d)
    return out


LIVE_STATES = ("pending", "running")


def _set(c, rid: str, now: float, guard: bool = True, **fields) -> bool:
    """Обновить выпуск. С охраной по статусу: пока тик ходил в сеть, владелец мог
    отменить выпуск — перетирать «отменён» обратно на «в работе» нельзя.

    Возвращает False, если выпуск уже не живой: продвижение надо прекратить.
    """
    fields["updated"] = now
    sets = ", ".join(f"{k}=?" for k in fields)
    where = "release_id=?" + (" AND status IN ('pending','running')" if guard else "")
    cur = c.execute(f"UPDATE pub_releases SET {sets} WHERE {where}", (*fields.values(), rid))
    return cur.rowcount > 0


def revive(c, release_id: str, now: float = None, keep_attempts: bool = False) -> dict:
    """Вернуть выпуск из разбора в работу.

    ДОБАВЛЕНО 04.09.2026. Карантин был односторонней дверью: движок помечал выпуск
    «сбой» и больше его не трогал, а вернуть в работу было нечем — ни кнопки, ни
    эндпоинта. Единственным выходом оставалась правка базы руками.

    Что делаем и чего НЕ делаем:

    * фазу не откатываем — работа, которая уже сделана и оплачена, остаётся сделанной.
      Движок продолжит с того шага, где остановился. Единственное исключение —
      аннулирование допуска заливки ниже: там прежняя приёмка ролика уже
      недействительна, и продолжать с неё значит залить не ту версию;
    * попытки обнуляем: человек посмотрел и решил, что причина устранена, — считать
      ему старые провалы значит через два тика снова уронить выпуск в разбор.
      `keep_attempts=True` оставляет счётчик, если возврат делается вслепую;
    * из необратимой фазы не возвращаем. Если загрузка уже начиналась, повтор
      запрещён навсегда — это правило заливки, и обходить его отсюда нельзя;
    * строку плана возвращаем из `error` в `running`: иначе календарь так и будет
      показывать пропуск на месте работающего выпуска.
    """
    now = time.time() if now is None else now
    rel = c.execute("SELECT * FROM pub_releases WHERE release_id=?",
                    (release_id,)).fetchone()
    if rel is None:
        return {"ok": False, "why": "выпуск не найден"}
    if rel["status"] != "blocked":
        return {"ok": False, "why": f"выпуск не в разборе (сейчас «{rel['status']}»)"}
    if rel["hand_confirmed"]:
        # Пометка «подтверждено человеком» остаётся навсегда (решение владельца, п. 6):
        # ролик на канале есть, и возврат в работу означал бы второй ролик.
        return {"ok": False,
                "why": "выпуск подтверждён руками — ролик на канале есть, "
                       "возвращать в работу нечего"}
    if reached_irreversible(c, release_id):
        return {"ok": False,
                "why": "загрузка уже начиналась — повтор запрещён навсегда, "
                       "нужен разбор вручную"}
    # Допуск ролика в очередь привязан к конкретной версии проекта и файла, и мёртвое
    # задание заливки означает, что прежняя приёмка недействительна: файл пересобрали
    # либо выпуск сняли в разбор ещё до выдачи. Оставить выпуск на фазе «комплект отдан»
    # нельзя — вперёд из неё ведёт только «идёт заливка», а задания больше нет, и выпуск
    # тем же тиком уехал бы обратно в разбор. Поэтому допуск аннулируем целиком: задание
    # убираем, фазу возвращаем на «обложка готова», и движок соберёт комплект заново,
    # заново опознав файл.
    #
    # Делаем это, только когда доказано, что ничего не отправлено и задание никто не
    # держит: `reached_irreversible` выше уже сказал, что в YouTube ничего не ушло, а
    # здесь проверяем, что нода не сидит на задании прямо сейчас (нет клейма, статус не
    # «running»). Иначе снос задания оборвал бы связь с живой заливкой: её поздний
    # вердикт получил бы «нет такого задания», и ролик на канале остался бы для нас
    # невидимым — а это прямая дорога ко второму ролику.
    #
    # Цена решения: комплект собирается заново целиком, то есть название ролика второй
    # раз просится у модели — это оплачиваемый вызов. Он копеечный и он один, а
    # собирать комплект наполовину (взять старое название к новому файлу) значит
    # отправить на канал ролик с описанием от версии, которой больше нет.
    phase = rel["phase"] or "created"
    job = c.execute("SELECT id, status, claim_token, video_id FROM pub_upload_jobs "
                    "WHERE release_id=?", (release_id,)).fetchone()
    annul = bool(job and phase in ("upload_pending", "upload_running")
                 and (job["status"] or "") != "running"
                 and not job["claim_token"] and not job["video_id"])
    if annul:
        phase = "cover_ready"
    fields = {"status": "pending", "blocked_reason": None,
              "deadline": now + PHASE_DEADLINES.get(phase, DEFAULT_DEADLINE)}
    if annul:
        fields["phase"] = phase
    if not keep_attempts:
        fields["attempts"] = 0
    # guard=False сознательно: охрана `_set` пропускает только живые выпуски
    # («pending», «running»), а мы как раз оживляем неживой. Статус уже проверен выше —
    # возвращаем только из «blocked» и только не из необратимой фазы.
    if not _set(c, release_id, now, guard=False, **fields):
        return {"ok": False, "why": "выпуск не обновился — возможно, он удалён"}
    if annul:
        # Задание удаляем, а не помечаем: это работа, а не история. История остаётся
        # в журнале выпуска — там видно и что допускали, и почему допуск отменён.
        c.execute("DELETE FROM pub_upload_jobs WHERE id=?", (job["id"],))
        log_line(c, release_id, "заливка",
                 "прежняя приёмка отменена вместе с заданием: ролик опознаем заново "
                 "и соберём комплект с нуля", now=now)
    c.execute("UPDATE pub_plan_items SET status='running', updated=? WHERE id=? "
              "AND status='error'", (now, rel["plan_item_id"]))
    reset_rewrite_resumes(c, release_id)
    log_line(c, release_id, "движок",
             f"возвращён в работу с фазы «{phase}»"
             + ("" if keep_attempts else ", счётчик попыток обнулён"), now=now)
    log_phase(c, release_id, "revived", now, payload={"from_phase": rel["phase"]})
    return {"ok": True, "phase": phase}


def reset_rewrite_resumes(c, release_id: str) -> bool:
    """Обнулить счётчик возобновлений задачи рерайта (pub_services._resume_rewrite).

    Движок возобновляет одну задачу соседа не больше REWRITE_MAX_RESUMES раз подряд.
    Человек, вернувший выпуск в работу, посмотрел и решил, что причина устранена, —
    счёт начинается заново, как и `attempts` выше. Зеркало задачи при этом остаётся в
    «error»: именно по нему движок понимает, что упавшую задачу велено поднять.
    """
    try:
        row = c.execute("SELECT payload FROM pub_release_jobs WHERE release_id=? "
                        "AND service='rewrite' AND kind='job'", (release_id,)).fetchone()
    except Exception:                                              # noqa: BLE001
        return False                 # база старше колонки `payload` — считать негде
    if not row or not row["payload"]:
        return False
    try:
        payload = json.loads(row["payload"]) or {}
    except (TypeError, ValueError):
        return False
    if "resumes" not in payload:
        return False
    payload.pop("resumes", None)
    c.execute("UPDATE pub_release_jobs SET payload=? WHERE release_id=? "
              "AND service='rewrite' AND kind='job'",
              (json.dumps(payload, ensure_ascii=False), release_id))
    return True


HAND_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


def confirm_by_hand(c, release_id: str, who: str = "", video_id: str = "",
                    note: str = "", now: float = None) -> dict:
    """Человек посмотрел канал и говорит: ролик там. Единственный выход из тупика.

    Решение владельца 01.10.2026, п. 6: «подтвердить руками» считается выполнением
    плана, пометка «подтверждено человеком» остаётся навсегда.

    Зачем это вообще. У выпуска, у которого загрузка уже начиналась, не было ни одного
    доступного действия: вернуть в работу нельзя (`revive` отказывает — повтор запрещён
    навсегда), снять строку плана нельзя, переставить нельзя, а выход канала занят
    насовсем. Единственным выходом оставалась правка базы руками. При этом ролик на
    канале чаще всего ЕСТЬ — просто подтверждения в machine-readable виде мы не
    получили: браузер умер на последнем шаге, Studio ответила не тем состоянием, нода
    замолчала после отправки файла.

    Что здесь нельзя делать и почему именно так сделано:

    * **Нельзя принимать подтверждение без номера ролика.** «Не знаем» не равно
      «подтверждено» — это закон проекта. Номер берётся из задания, если нода успела его
      прислать, иначе его обязан вписать человек, глядя на адресную строку. Без номера
      отказываем словами, а не закрываем выпуск «на доверии».
    * **Нельзя выдавать это за машинное подтверждение.** Фазу `published` ставит только
      поздняя проверка по ответу YouTube Data API. Ручное подтверждение живёт своими
      колонками, и сводка называет такие выходы отдельно: «в том числе руками N».
    * **Нельзя повторять заливку после этого.** Задание помечается `done`, выход канала
      остаётся занятым, строка плана закрывается как выполненная. Повтор здесь — это
      второй ролик на канале.
    * **Нельзя подтверждать то, что заливкой и не пахло.** Если `reached_irreversible`
      говорит «файл не уходил», выпуск надо возвращать в работу (`revive`), а не
      закрывать: иначе одна неверная кнопка превратит сорвавшийся до отправки выпуск в
      «вышел», и ролика на канале не будет никогда. Исключение — выпуск с номером
      ролика: номер и есть улика отправки.
    * **Пометка не снимается.** `revive` ниже отказывает подтверждённому руками выпуску,
      и ни один путь в коде эти колонки не чистит.

    Возвращает `{"ok": ..., "why": ...}`; ошибки — словами для человека.
    """
    now = time.time() if now is None else now
    rel = c.execute("SELECT * FROM pub_releases WHERE release_id=?",
                    (release_id,)).fetchone()
    if rel is None:
        return {"ok": False, "why": "выпуск не найден"}
    if rel["hand_confirmed"]:
        return {"ok": False, "why": "этот выпуск уже подтверждён руками"}
    if rel["status"] != "blocked":
        return {"ok": False,
                "why": f"подтверждают руками только выпуск в разборе "
                       f"(сейчас «{rel['status']}»)"}
    job = c.execute("SELECT id, status, video_id FROM pub_upload_jobs WHERE release_id=?",
                    (release_id,)).fetchone()
    # Номер ролика: сначала то, что прислала нода, потом то, что вписал человек.
    # Чужой формат не принимаем — одиннадцать символов алфавита YouTube и ничего кроме:
    # «посмотрю потом» в этом поле превратило бы улику в отговорку.
    from pub_ids import video_id as _vid                            # noqa: PLC0415
    known = str((job["video_id"] if job else "") or "").strip()
    # Человек вставляет то, что у него под рукой: и ссылку целиком, и один номер. И то и
    # другое понятно, а вот «посмотрю потом» в этом поле превратило бы улику в отговорку.
    вписано = str(video_id or "").strip()
    typed = вписано if HAND_ID_RE.fullmatch(вписано) else _vid(вписано)
    if вписано and not typed:
        # Отдельный ответ, а не общий «номера не знаем»: человек УЖЕ что-то вписал, и
        # сказать ему «впишите ссылку» значит отправить его делать то, что он сделал.
        return {"ok": False,
                "why": f"«{вписано[:60]}» не похоже на ролик YouTube: нужна ссылка на "
                       f"него или одиннадцать символов его номера"}
    vid = known or typed
    if not vid:
        return {"ok": False,
                "why": "подтверждать нечем: номера ролика на YouTube мы не знаем. "
                       "Откройте канал, скопируйте ссылку на ролик и впишите её"}
    if known and typed and known != typed:
        return {"ok": False,
                "why": f"нода прислала другой номер ролика ({known}), чем вписан "
                       f"({typed}). Так закрывать нельзя — нужен разбор"}
    if not reached_irreversible(c, release_id) and not known:
        return {"ok": False,
                "why": "файл в YouTube не уходил — такой выпуск возвращают в работу, "
                       "а не закрывают как вышедший"}
    fields = {"status": "done", "video_id": vid}
    if not _set(c, release_id, now, guard=False, **fields):
        return {"ok": False, "why": "выпуск не обновился — возможно, он удалён"}
    c.execute("UPDATE pub_releases SET hand_confirmed=?, hand_by=?, hand_video_id=?, "
              "hand_note=? WHERE release_id=?",
              (now, str(who or "")[:80], vid, " ".join(str(note or "").split())[:400],
               release_id))
    # Строка плана закрывается как выполненная: выход занят, и занят по-настоящему —
    # ролик на канале есть. Выход при этом НЕ освобождается: освободив его, мы отдали
    # бы то же время второму ролику.
    c.execute("UPDATE pub_plan_items SET status='done', updated=? WHERE id=?",
              (now, rel["plan_item_id"]))
    if job:
        # Задание закрываем, а не удаляем: по нему видно, что заливка была и чем
        # кончилась. `done` здесь означает «работа окончена», а не «подтверждено
        # машиной» — машинный вердикт лежит в своей колонке и не переписывается.
        c.execute("UPDATE pub_upload_jobs SET status='done', video_id=?, finished=?, "
                  "updated=? WHERE id=?", (vid, now, now, job["id"]))
    кто = (who or "человек")
    log_line(c, release_id, "движок",
             f"подтверждено руками ({кто}): ролик {vid} на канале есть. Выпуск закрыт "
             f"как выполненный, заливка больше не повторится"
             + (f". {note}" if note else ""), now=now)
    log_phase(c, release_id, "hand_confirmed", now,
              payload={"by": str(who or "")[:80], "video_id": vid})
    return {"ok": True, "video_id": vid}


def quarantine(c, rel, now: float, reason: str) -> None:
    """Увести строку в карантин: остальные продолжают идти.

    Заодно гасим ЕЩЁ НЕ ВЗЯТОЕ задание заливки: иначе нода позже заберёт его и зальёт
    ролик, который в кабинете значится сломанным.
    """
    log_line(c, rel["release_id"], "движок", f"остановил и убрал в разбор: {reason}",
             level="error", now=now)
    if not _set(c, rel["release_id"], now, status="blocked", blocked_reason=reason[:500]):
        return          # выпуск уже отменён владельцем — не воскрешаем его в blocked
    c.execute("UPDATE pub_plan_items SET status='error', updated=? WHERE id=?",
              (now, rel["plan_item_id"]))
    c.execute("UPDATE pub_upload_jobs SET status='cancelled', error=?, updated=? "
              "WHERE release_id=? AND status='queued'",
              ("выпуск убран в разбор до начала заливки", now, rel["release_id"]))
    log_phase(c, rel["release_id"], "blocked", now, payload={"reason": reason[:500]})


# Признак жизни считается свежим столько времени: машина заливки шлёт пульс раз в
# минуту, так что десять минут тишины — это уже настоящая тишина, а не пауза.
ALIVE_SILENCE = 600
# Потолок для неавторитетных зеркал соседей без lease/heartbeat. Штатные очереди со
# своими арендой и watchdog этим таймером не обрываем.
HARD_STALL = 12 * 3600


def _authoritative_work_alive(c, rel, now: float) -> bool:
    """Живая штатная очередь именно ТЕКУЩЕЙ фазы.

    Её собственные lease/heartbeat точнее общего таймера выпуска, поэтому такую работу
    нельзя снимать даже по HARD_STALL. Старый код смотрел задачи всех фаз сразу: забытый
    rewrite мог навечно прикрыть уже зависший render.
    """
    rid = rel["release_id"]
    phase = rel["phase"] or "created"

    if phase == "project_ready":
        rows = c.execute(
            "SELECT q.status status FROM pub_release_jobs j "
            "JOIN queue_tasks q ON CAST(q.job_id AS TEXT)=j.remote_job_id "
            "WHERE j.release_id=? AND j.service='vsgen'",
            (rid,),
        ).fetchall()
        return any((row["status"] or "") in ("queued", "running") for row in rows)

    if phase in ("cover_ready", "upload_pending", "upload_running"):
        row = c.execute(
            "SELECT status, heartbeat FROM pub_upload_jobs WHERE release_id=?",
            (rid,),
        ).fetchone()
        if not row:
            return False
        if (row["status"] or "") == "queued":
            return True
        return bool(
            (row["status"] or "") == "running"
            and row["heartbeat"]
            and now - row["heartbeat"] < ALIVE_SILENCE
        )
    return False


def work_alive(c, rel, now: float) -> bool:
    """Идёт ли работа над выпуском ПРЯМО СЕЙЧАС, по внешним признакам.

    Срок фазы отмеряет наше терпение, а не чужую работу: рерайт стоит в очереди соседа,
    рендер — в очереди кабинета, заливка идёт на машине с профилем. Судить о зависании
    по одним часам значит снимать исправные выпуски, что и происходило: заливку убивали
    через полчаса, пока машина исправно слала пульс каждую минуту.
    """
    if _authoritative_work_alive(c, rel, now):
        return True
    rid = rel["release_id"]
    phase = rel["phase"] or "created"
    # Рерайт и обложки идут у СОСЕДНИХ сервисов, их очередь в нашей базе не видна: здесь
    # есть только номер заказанной задачи. Пока задача заказана и не закрыта — работа идёт,
    # и снимать выпуск нельзя. Без этой ветки восемьдесят выпусков, честно ждавших текст,
    # были сняты как «зависшие», пока сосед их писал. От вечного ожидания страхует общий
    # потолок HARD_STALL. В отличие от штатных очередей это лишь наше зеркало без lease,
    # поэтому оно не имеет права перекрывать абсолютный потолок ожидания.
    service = {"created": "rewrite", "media_ready": "cover"}.get(phase)
    if not service:
        return False
    pending = c.execute(
        "SELECT COUNT(*) n FROM pub_release_jobs WHERE release_id=? AND kind='job' "
        "AND service=? AND COALESCE(remote_job_id,'')!='' "
        "AND status NOT IN ('done','error','cancelled')", (rid, service)).fetchone()
    return bool(pending and pending["n"])


def is_stuck(c, rel, now: float) -> bool:
    """Выпуск завис: срок вышел И никаких признаков идущей работы нет."""
    deadline = rel["deadline"]
    if not bool(deadline) or now <= deadline:
        return False
    # Авторитетная queued/running задача управляется своей арендой и watchdog. Общий
    # HARD_STALL не должен убивать её посреди честной работы.
    if _authoritative_work_alive(c, rel, now):
        return False
    if now > deadline + HARD_STALL:
        return True
    return not work_alive(c, rel, now)


def next_phase(current: str) -> str:
    idx = PHASE_INDEX.get(current, 0)
    return PHASES[idx + 1] if idx + 1 < len(PHASES) else current


def advance(c, rel, services, now: float) -> str:
    """Двинуть выпуск на одну фазу. Возвращает, что произошло."""
    rid = rel["release_id"]
    phase = rel["phase"] or "created"

    # video_id означает, что файл уже ушёл в YouTube. Если зеркало фазы откатилось до
    # upload_pending, не ретраим и не карантиним автоматически: только ручная сверка
    # существующего ролика может решить, scheduled он или needs_repair.
    if phase == "upload_pending" and rel["video_id"]:
        return "manual_reconciliation"

    if is_stuck(c, rel, now):
        # Зависание — отказ, а не работа: снимаем, освобождаем ресурсы, считаем попытку.
        attempts = (rel["attempts"] or 0) + 1
        log_phase(c, rid, "stalled", now, payload={"phase": phase})
        log_line(c, rid, "движок",
                 f"шаг «{phase}» не двигался дольше срока — снимаю и считаю попытку",
                 level="warn", now=now)
        try:
            services.release_resources(c, rel)
        except Exception:
            pass
        if attempts >= MAX_ATTEMPTS or reached_irreversible(c, rid):
            quarantine(c, rel, now, f"зависло на шаге «{phase}» и попытки исчерпаны")
            return "quarantined"
        _set(c, rid, now, attempts=attempts, status="pending",
             deadline=now + PHASE_DEADLINES.get(phase, DEFAULT_DEADLINE))
        return "retried"

    # Успех первой версии — подтверждённое расписание: Studio показала SCHEDULED на нужном
    # канале в нужное время. Проверка «вышел в PUBLIC» делается позже отдельным проходом и
    # только повышает запись; держать выпуск незакрытым до неё значило бы врать про работу.
    if phase in ("scheduled", "published"):
        _set(c, rid, now, status="done")
        c.execute("UPDATE pub_plan_items SET status='done', updated=? WHERE id=?",
                  (now, rel["plan_item_id"]))
        log_line(c, rid, "движок", "ролик подтверждённо стоит в расписании канала", now=now)
        return "done"

    want = next_phase(phase)
    if want == "upload_running" and reached_irreversible(c, rid):
        # Загрузка уже начиналась: повторять её нельзя ни при каких обстоятельствах.
        quarantine(c, rel, now, "загрузка уже начиналась — повтор запрещён, нужен разбор")
        return "quarantined"

    handler = getattr(services, "do_" + want, None)
    if handler is None:
        quarantine(c, rel, now, f"нет обработчика шага «{want}»")
        return "quarantined"

    try:
        c.commit()          # обработчик ходит в сеть и в другие соединения — замок отпустить
        result = handler(c, rel) or {}
    except ResourceDown as exc:
        # Общий ресурс: строку не трогаем, откладываем и помечаем ресурс.
        _set(c, rid, now, status="pending",
             deadline=now + PHASE_DEADLINES.get(phase, DEFAULT_DEADLINE),
             blocked_reason=f"ждём: {exc}")
        log_phase(c, rid, "resource_wait", now, payload={"resource": exc.resource})
        log_line(c, rid, str(exc.resource), f"ждём: {exc}", level="warn", now=now)
        return "resource_wait"
    except ItemFailed as exc:
        quarantine(c, rel, now, str(exc))
        return "quarantined"
    except Exception as exc:                       # noqa: BLE001 — падение обработчика
        quarantine(c, rel, now, f"ошибка на шаге «{want}»: {exc}")
        return "quarantined"

    if result.get("wait"):
        # Работа идёт, но не закончена. Срок НЕ продлеваем: он выставлен при входе в фазу,
        # и только так «зависло дольше срока» вообще может наступить. Продление каждым
        # опросом делало проверку зависания мёртвой.
        _set(c, rid, now, status="running", heartbeat=now)
        return "waiting"

    video_id = result.get("video_id") or rel["video_id"] or ""
    moved = _set(c, rid, now, phase=want, status="running", heartbeat=now,
                 video_id=video_id or None, blocked_reason=None,
                 evidence=json.dumps(result["evidence"], ensure_ascii=False)
                 if result.get("evidence") else rel["evidence"],
                 deadline=now + PHASE_DEADLINES.get(want, DEFAULT_DEADLINE))
    if not moved:
        return "cancelled_meanwhile"      # владелец отменил, пока шаг выполнялся
    log_phase(c, rid, want, now, video_id=video_id, payload=result.get("payload"))
    log_line(c, rid, "движок", f"шаг «{want}» готов", now=now)
    return want


def revive_cover_preset_chosen(c, now: float) -> list:
    """Выпуски, стоявшие без пресета обложек, возвращаются в работу, как только он выбран.

    03.10.2026: тринадцать выпусков новых каналов дошли до обложки и встали — у каналов
    не выбран пресет. Человек, который их настраивает, выберет пресет в карточке канала
    (или в пресете производства) — и на этом его работа должна кончаться. Без этого шага
    выпуски так и стояли бы в разборе, пока кто-то не пройдёт по каждому с «Вернуть в
    работу», и выглядело бы это как «выбрал, а ничего не поменялось».

    Возвращаем ТОЛЬКО остановленные ровно этой причиной и только когда пресет теперь
    действительно находится — тем же `channel_presets`, которым его ищет обложка.
    Остальное делает обычный `revive`: фазу не откатывает, из необратимой не возвращает.
    """
    rows = c.execute(
        "SELECT r.release_id, ch.preset_vsgen_id, ch.preset_rewrite, ch.preset_cover "
        "FROM pub_releases r JOIN pub_channels_live ch ON ch.id=r.channel_id "
        "WHERE r.status='blocked' AND r.blocked_reason=?",
        (COVER_PRESET_MISSING,)).fetchall()
    back = []
    for r in rows:
        _rew, cov = channel_presets(c, r["preset_vsgen_id"], r["preset_rewrite"] or "",
                                    r["preset_cover"] or "")
        if not cov:
            continue
        if revive(c, r["release_id"], now).get("ok"):
            log_line(c, r["release_id"], "обложка",
                     f"у канала выбран пресет обложек «{cov}» — выпуск вернулся в работу "
                     f"сам", now=now)
            back.append(r["release_id"])
    return back


def tick(c, services, now: float = None) -> dict:
    """Один шаг движка. Ошибка одного выпуска не мешает остальным."""
    now = time.time() if now is None else now
    try:
        revive_cover_preset_chosen(c, now)
    except Exception:                              # noqa: BLE001 — не роняем тик
        pass
    reflow_slots(c, now)          # ссылки без даты занимают освободившиеся выходы
    reslot_pending_uploads(c, now)   # готовый ролик не теряется из-за прошедшего времени
    made = materialize(c, now)
    moved, failed = [], []
    for rel in live_releases(c):
        try:
            moved.append((rel["release_id"], advance(c, rel, services, now)))
        except Exception as exc:                   # noqa: BLE001
            failed.append((rel["release_id"], str(exc)))
    c.commit()
    return {"created": made, "moved": moved, "failed": failed}
