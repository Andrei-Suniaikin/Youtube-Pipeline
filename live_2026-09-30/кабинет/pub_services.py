#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Связка движка выпусков с реальными стадиями кабинета.

Что уже подключено по-настоящему:
  * создание проекта в VideoStudio из готового текста;
  * постановка озвучки, картинок и рендера в штатную очередь кабинета;
  * ожидание результата по состоянию очереди — без своего опроса и своих таймеров.

Что ещё не подключено (заливка): шаг честно поднимает `ResourceDown`
с понятной причиной. Выпуск при этом НЕ ломается и НЕ уходит в карантин — он ждёт, как
ждал бы при кончившихся кредитах, и продолжит сам, когда стык появится. Это сознательно:
молча «проскочить» неподключённую стадию значило бы объявить готовым то, чего нет.
"""

from __future__ import annotations

import json
import os
import re
import time

from pub_engine import ItemFailed, ResourceDown, reached_irreversible
from pub_log import add as log_line
from pub_neighbors import rewrite_preset_config
from pub_publish import (
    TitleError,
    TitleUnavailable,
    chapters_enabled,
    find_video,
    make_description,
    make_title,
    read_chapters,
    schedule_iso,
)
from pub_remote import (
    CoverClient,
    RewriteClient,
    cover_degraded,
    donor_text,
    get_key,
)

# Этапы кабинета, из которых собирается ролик, в порядке запуска.
MEDIA_STAGES = ("tts", "images", "render", "chapters")

# Этапы, без которых ролик всё равно можно публиковать: их сбой не останавливает выпуск.
# Главы собираются не всегда (мало глав, запасные заголовки) — это не повод терять ролик.
OPTIONAL_STAGES = ("chapters",)

TERMINAL_OK = ("done",)
TERMINAL_BAD = ("error", "cancelled")
# После отказа генератора названий (429, сеть) не ходим к нему столько секунд: иначе
# каждый выпуск тика ждёт свой отказ, и тик движка растягивается на десятки минут.
TITLE_PAUSE_SEC = 300
# Временный отказ сервиса обложек (лимит ключа прокси, сеть) — заказать обложку заново,
# но не больше COVER_REORDER_MAX раз на выпуск: постоянную поломку всё равно увидит человек.
_COVER_TRANSIENT = re.compile(r"429|rate.?limit|waited too long|retry shortly|timed? ?out|"
                              r"temporar|overload|50[234]|connection (?:reset|error|aborted)", re.I)
# Отказ приходит от прокси ДО рисования, так что перезаказ почти ничего не стоит; но ключ
# бывает забит часами (19.09: соседняя служба держала 4 потока всю ночь) — перезаказываем
# не чаще раза в COVER_REORDER_GAP_SEC и не больше COVER_REORDER_MAX раз (≈4 часа).
COVER_REORDER_MAX = 12
COVER_REORDER_GAP_SEC = 1200
COVER_REORDER_MARK = "временный сбой, заказываю заново"
# Заливка, упавшая ДО передачи файла (машина не открыла профиль, не попала в Studio),
# ничем не отличается от сетевого чиха: файл в YouTube не уходил, повтор безопасен. Раньше
# любой такой выпуск ждал человека — 19.09 так встали шесть штук за ночь. Повторяем сами,
# не чаще раза в UPLOAD_RETRY_GAP_SEC и не больше UPLOAD_RETRY_MAX раз; после этого разбор.
# 24.09: было 2. Замер за 7 суток — 28 роликов дошли до последней попытки и ушли
# в разбор, при этом отказы ДО передачи файла случайны (мёртвый браузер AdsPower,
# залипший профиль) и лечатся повтором. Пять попыток при паузе 20 минут — это
# полтора часа терпения на ролик вместо двадцати минут.
UPLOAD_RETRY_MAX = int(os.environ.get("UPLOAD_RETRY_MAX", "5"))
UPLOAD_RETRY_GAP_SEC = 1200
UPLOAD_RETRY_MARK = "повтор заливки"


class PubServices:
    """Обработчики фаз. Каждый метод возвращает {} (шаг сделан) или {"wait": True}."""

    def __init__(self, ctx):
        self.create_project = ctx["create_project"]
        self.enqueue = ctx["enqueue"]
        self.project_dir = ctx["project_dir"]
        self.owner_uid = ctx.get("owner_uid")
        self.default_username = ctx.get("username") or ""
        # чем писать названия: {"base","key","model"} прокси triklz (семейство alt_llm)
        self.title_llm = ctx.get("title_llm") or (lambda: {})

    def _username(self, c, tenant_id: str) -> str:
        """Имя входа владельца кабинета — по нему ищутся его пресеты у соседей."""
        if self.default_username:
            return self.default_username
        row = c.execute("SELECT name FROM users WHERE tenant_id=? ORDER BY id LIMIT 1",
                        (tenant_id,)).fetchone()
        return (row[0] if row else "") or ""

    def _uid(self, c, tenant_id: str):
        """Чьим именем ставить задачи в очередь: владелец этого кабинета."""
        if self.owner_uid:
            return self.owner_uid
        row = c.execute("SELECT id FROM users WHERE tenant_id=? ORDER BY id LIMIT 1",
                        (tenant_id,)).fetchone()
        return row[0] if row else None

    # ── вспомогательное ──

    def _jobs(self, c, release_id: str) -> dict:
        # error нужен наравне с остальными: по нему do_media_ready отличает первый
        # заход от повторной постановки картинок. Без поля Row кидает "No item with that key".
        rows = c.execute("SELECT kind, remote_job_id, status, error FROM pub_release_jobs "
                         "WHERE release_id=? AND service='vsgen'", (release_id,)).fetchall()
        return {r["kind"]: r for r in rows}

    def _remember_job(self, c, release_id: str, kind: str, job_id, now: float) -> None:
        c.execute(
            "INSERT OR REPLACE INTO pub_release_jobs(release_id,service,kind,remote_job_id,"
            "status,created,updated) VALUES(?,?,?,?,?,?,?)",
            (release_id, "vsgen", kind, str(job_id) if job_id else None, "queued", now, now))

    def _queue_status(self, c, job_id) -> str:
        row = c.execute("SELECT status FROM queue_tasks WHERE job_id=?", (job_id,)).fetchone()
        return (row["status"] if row else "") or ""

    def release_resources(self, c, rel) -> None:
        """Зависший выпуск: отпустить всё, что он держит. Задачи очереди трогать нельзя —
        ими управляет штатный планировщик, у него свои повторы и своя парковка."""
        return None

    # ── фазы ──

    def store_source_text(self, c, release_id: str, text: str, now: float = 0.0) -> None:
        """Сохранить готовый текст выпуска. Отсюда его берёт создание проекта.

        Держим в своей таблице, а не в папке проекта: проекта на этот момент ещё нет,
        а текст уже есть — это и есть результат стадии рерайта.
        """
        c.execute(
            "INSERT OR REPLACE INTO pub_release_jobs(release_id,service,kind,status,"
            "payload,created,updated) VALUES(?,?,?,?,?,?,?)",
            (release_id, "rewrite", "text", "done",
             json.dumps({"text": text}, ensure_ascii=False), now, now))

    def _remote(self, c, release_id: str, service: str):
        row = c.execute("SELECT * FROM pub_release_jobs WHERE release_id=? AND service=?"
                        " AND kind='job'", (release_id, service)).fetchone()
        return row

    def _remember_remote(self, c, release_id: str, service: str, job_id: str, now: float):
        c.execute(
            "INSERT OR REPLACE INTO pub_release_jobs(release_id,service,kind,remote_job_id,"
            "status,created,updated) VALUES(?,?,?,?,?,?,?)",
            (release_id, service, "job", str(job_id), "running", now, now))

    def _finish_remote(self, c, release_id: str, service: str, status: str,
                       error: str = "") -> None:
        """Синхронизировать терминальный исход соседа в долговечное зеркало выпуска."""
        c.execute(
            "UPDATE pub_release_jobs SET status=?, error=?, updated=? "
            "WHERE release_id=? AND service=? AND kind='job'",
            (status, (error or None), self._clock(), release_id, service),
        )

    def _new_prompt_work_paused(self, c, tenant_id: str,
                                project_id: str | None = None) -> bool:
        """Остановлена ли новая генерация текстов/промптов владельца кабинета.

        Уже заказанный соседу рерайт продолжаем только опрашивать: кнопка паузы обещает
        не обрывать идущие задачи. Но новый заказ в Rewrite создавать нельзя — иначе
        публикационный контур обходил персональную паузу и продолжал тратить Claude.
        """
        uid = self._uid(c, tenant_id)
        if uid is None:
            return False
        try:
            row = c.execute("SELECT v FROM admin_state WHERE k='queue_pause'").fetchone()
            state = json.loads(row[0]) if row and row[0] else {}
            if not isinstance(state, dict):
                return False
            # Глобальный аварийный стоп и точечная пауза любого prompt-provider
            # сильнее project allowlist. Уже созданную удалённую задачу этот guard
            # не трогает: do_source_ready вызывает его только при known is None.
            if bool(state.get("all")):
                return True
            raw_providers = state.get("providers") or []
            providers = ({str(value).strip().lower() for value in raw_providers
                          if str(value).strip()}
                         if isinstance(raw_providers, (list, tuple, set)) else set())
            if any(provider.startswith("prompt_") for provider in providers):
                return True
            paused = set()
            for value in (state.get("prompt_uids") or []):
                try:
                    parsed_uid = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed_uid > 0:
                    paused.add(parsed_uid)
            if int(uid) not in paused:
                return False
            raw_allowed = state.get("prompt_project_allowlist") or []
            allowed = ({value.strip() for value in raw_allowed
                        if isinstance(value, str) and value.strip()}
                       if isinstance(raw_allowed, (list, tuple, set)) else set())
            return not (isinstance(project_id, str) and project_id in allowed)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def do_source_ready(self, c, rel):
        """Рерайт донорской ссылки: субтитры → задача соседу → готовый текст.

        Задача заказывается один раз, её номер запоминается: повторный тик не заказывает
        рерайт заново, а только смотрит, готов ли он.
        """
        known = self._remote(c, rel["release_id"], "rewrite")
        if known is not None and known['status'] == 'missing':
            raise ItemFailed(known['error'] or 'Сохранённая задача рерайта отсутствует; требуется восстановление')
        try:
            project_id = rel["project_id"]
        except (KeyError, IndexError):
            project_id = None
        if (known is None
                and self._new_prompt_work_paused(
                    c, rel["tenant_id"], project_id=project_id
                )):
            raise ResourceDown(
                "тексты",
                "генерация текстов и промптов этого профиля приостановлена",
            )

        key = get_key(c, "rewrite")
        if not key:
            raise ResourceDown("рерайт", "не сохранён ключ доступа к рерайту")
        client = RewriteClient(key)

        if known is None:
            text = donor_text(rel["donor_url"])
            file_id = client.upload_text(text, filename=f'{rel["release_id"]}.txt')
            preset = rewrite_preset_config(self._username(c, rel["tenant_id"]),
                                           rel["preset_rewrite"] or "")
            job_id = client.create_job(file_id, rel["lang"] or "ru", preset,
                                       channel_name=rel["ch_name"] or "", idempotency_key="release:"+str(rel["release_id"]))
            self._remember_remote(c, rel["release_id"], "rewrite", job_id, rel["updated"])
            log_line(c, rel["release_id"], "рерайт",
                     f"субтитры донора {len(text)} символов, заказал задачу {job_id}, "
                     f"пресет «{rel['preset_rewrite'] or 'не выбран'}»")
            return {"wait": True}

        from pub_remote import RemoteJobMissing
        try:
            info = client.job(known["remote_job_id"])
        except RemoteJobMissing as exc:
            self._finish_remote(c, rel["release_id"], "rewrite", "missing", str(exc))
            raise
        state = str(info.get("status") or "").lower()
        if state in ("error", "failed", "cancelled"):
            reason = "рерайт не справился: " + str(info.get("error") or state)
            self._finish_remote(c, rel["release_id"], "rewrite", "error", reason)
            raise ItemFailed(reason)
        if state not in ("done", "completed", "finished", "success"):
            return {"wait": True}

        try:
            code = client.result_code(info, rel["lang"] or "ru")
            text = client.result_text(known["remote_job_id"], code)
            if len(text.strip()) < 500:
                raise ItemFailed("рерайт вернул слишком короткий текст")
            self.store_source_text(c, rel["release_id"], text, rel["updated"])
        except Exception as exc:
            self._finish_remote(c, rel["release_id"], "rewrite", "error", str(exc))
            raise
        self._finish_remote(c, rel["release_id"], "rewrite", "done")
        log_line(c, rel["release_id"], "рерайт", f"текст готов, {len(text)} символов")
        return {"payload": {"chars": len(text)}}

    def do_project_ready(self, c, rel):
        """Завести проект VideoStudio из готового текста и запомнить его за выпуском."""
        row = c.execute("SELECT project_id FROM pub_releases WHERE release_id=?",
                        (rel["release_id"],)).fetchone()
        if row and row["project_id"]:
            return {}                           # уже создан: повторный тик не дублирует
        text = self._source_text(c, rel)
        if not text:
            raise ItemFailed("нет текста для проекта — рерайт не отдал результат")
        # Настройки пресета кладём В ПРОЕКТ целиком, а не одну пометку о выборе:
        # иначе на странице проекта пресет показан, а голос, стиль и модель картинок
        # у него пустые — озвучка и картинки уедут на умолчаниях кабинета.
        settings = None
        if rel["preset_vsgen_id"]:
            ps = self._preset_settings(c, rel["preset_vsgen_id"])
            if isinstance(ps, dict) and ps:
                settings = dict(ps)
            else:
                # Пресет удалён или нечитаем: проект всё равно заводим, но говорим
                # об этом в журнале выпуска — молчаливые умолчания хуже жалобы.
                log_line(c, rel["release_id"], "ролик",
                         f"настройки пресета {rel['preset_vsgen_id']} не прочитались — "
                         "проект уйдёт на умолчаниях кабинета", level="warn")
                settings = {}
            settings["_applied_preset"] = rel["preset_vsgen_id"]
        c.commit()      # создание проекта откроет своё соединение — наш замок отпустить
        pid = self.create_project(rel["tenant_id"], rel["ch_name"] or "выпуск",
                                  source_text=text, settings=settings)
        c.execute("UPDATE pub_releases SET project_id=? WHERE release_id=?",
                  (pid, rel["release_id"]))
        return {"payload": {"project_id": pid}}


    # Ключи, которые в задачу передавать нельзя: это внутренние пометки кабинета об
    # отложенных этапах и о применённом пресете, а не настройки генерации.
    _NOT_TASK_PARAMS = ("prompt_failover", "_applied_preset")

    def _preset_params(self, c, rel) -> dict:
        """Настройки пресета из проекта — те самые, что кабинет кладёт в задачу.

        Без них этап уходил с пустыми параметрами, а движок озвучки по умолчанию —
        edge: пресет с voicegencore честно лежал в проекте, но до задачи не доезжал,
        и роликам доставался чужой голос и десятки кусков вместо одного файла. Тем же
        путём терялись стиль картинок, скорость и модель. Кабинет собирает параметры
        именно так (настройки проекта = база), повторяем один в один.
        """
        try:
            row = c.execute("SELECT settings FROM projects WHERE id=?",
                            (rel["project_id"],)).fetchone()
        except Exception:
            return {}
        if not row or not row["settings"]:
            return {}
        try:
            st = json.loads(row["settings"])
        except Exception:
            return {}
        if not isinstance(st, dict):
            return {}
        return {k: v for k, v in st.items()
                if k not in self._NOT_TASK_PARAMS and not str(k).startswith("deferred_")}

    def do_media_ready(self, c, rel):
        """Озвучка, картинки, рендер — штатной очередью кабинета, по одному этапу за раз."""
        if not rel["project_id"]:
            raise ItemFailed("у выпуска нет проекта")
        jobs = self._jobs(c, rel["release_id"])
        # Озвучка и картинки друг друга не ждут: обе делаются из готового текста, и
        # кнопка «весь конвейер» в кабинете ставит их одновременно. Здесь же этапы шли
        # строго по очереди, и картинки начинались только после озвучки — при двух
        # десятках выпусков это удваивало путь ролика на ровном месте.
        if jobs.get("tts") is not None and jobs.get("images") is None:
            uid = self._uid(c, rel["tenant_id"])
            if uid:
                c.commit()
                job_id = self.enqueue(uid, rel["tenant_id"], rel["project_id"], "images",
                                      self._preset_params(c, rel))
                if job_id:
                    self._remember_job(c, rel["release_id"], "images", job_id, rel["updated"])
                    log_line(c, rel["release_id"], "ролик",
                             f"поставил этап «images» рядом с озвучкой, задача {job_id}")
                    jobs = self._jobs(c, rel["release_id"])

        for kind in MEDIA_STAGES:
            known = jobs.get(kind)
            if known is None:
                uid = self._uid(c, rel["tenant_id"])
                if not uid:
                    raise ResourceDown("кабинет", "не найден владелец кабинета для постановки")
                c.commit()      # постановка задачи откроет своё соединение — замок отпустить
                job_id = self.enqueue(uid, rel["tenant_id"], rel["project_id"], kind,
                                      self._preset_params(c, rel))
                if not job_id:
                    raise ResourceDown(kind, f"очередь не приняла этап «{kind}»")
                self._remember_job(c, rel["release_id"], kind, job_id, rel["updated"])
                log_line(c, rel["release_id"], "ролик", f"поставил этап «{kind}», задача {job_id}")
                return {"wait": True}
            state = self._queue_status(c, known["remote_job_id"])
            if not state:
                # Строки в очереди больше нет (кнопка «очистить очередь» кабинета стирает
                # историю). Берём зеркальный статус, который сами записали; нет и его —
                # этап потерян, честная ошибка вместо вечного ожидания.
                state = known["status"] or ""
                if state not in TERMINAL_OK + TERMINAL_BAD:
                    raise ItemFailed(f"этап «{kind}» пропал из очереди — поставьте заново")
            elif state in TERMINAL_OK + TERMINAL_BAD and state != known["status"]:
                c.execute("UPDATE pub_release_jobs SET status=? WHERE release_id=? "
                          "AND service='vsgen' AND kind=?",
                          (state, rel["release_id"], kind))
            if state in TERMINAL_BAD:
                if kind in OPTIONAL_STAGES:
                    continue      # без глав ролик публикуется, просто без таймкодов
                raise ItemFailed(f"этап «{kind}» завершился со статусом {state}")
            if state not in TERMINAL_OK:
                return {"wait": True}
            if kind == "images" and not self._images_exist(rel):
                # Кабинет на «images» мог сначала поставить задачу промптов — она done,
                # а картинок нет. Один раз ставим этап заново, второй провал — ошибка.
                if known["error"] == "re-enqueued":
                    raise ItemFailed("этап картинок завершился, а картинок в проекте нет")
                uid = self._uid(c, rel["tenant_id"])
                c.commit()      # то же: чужое соединение не должно ждать наш замок
                job_id = self.enqueue(uid, rel["tenant_id"], rel["project_id"], "images",
                                      self._preset_params(c, rel))
                if not job_id:
                    raise ResourceDown("images", "очередь не приняла повторный этап картинок")
                c.execute("UPDATE pub_release_jobs SET remote_job_id=?, status='queued', "
                          "error='re-enqueued', updated=? WHERE release_id=? "
                          "AND service='vsgen' AND kind='images'",
                          (str(job_id), rel["updated"], rel["release_id"]))
                log_line(c, rel["release_id"], "ролик",
                         "первая задача картинок оказалась промптами — ставлю сами картинки")
                return {"wait": True}
        return {}

    def _images_exist(self, rel) -> bool:
        import glob as _g
        proj = self.project_dir(rel["tenant_id"], rel["project_id"])
        for pat in ("googler/*.jpg", "googler/*.jpeg", "googler/*.png"):
            if _g.glob(os.path.join(proj, pat)):
                return True
        return False

    def _cover_reorders(self, c, release_id: str) -> tuple:
        """(сколько раз обложку перезаказывали после временного сбоя, когда последний раз).

        Счётчик в своей строке, а не в журнале: журнал выпуска подрезается до 400 строк,
        и строки «ждём» каждого тика вытеснили бы отметки — предел перестал бы работать.
        """
        row = c.execute("SELECT payload FROM pub_release_jobs WHERE release_id=? AND service='meta'"
                        " AND kind='cover_reorders'", (release_id,)).fetchone()
        try:
            data = json.loads(row["payload"]) if row and row["payload"] else {}
        except Exception:
            data = {}
        return int(data.get("n") or 0), float(data.get("last") or 0.0)

    def _note_cover_reorder(self, c, release_id: str, n: int, now: float) -> None:
        c.execute("INSERT OR REPLACE INTO pub_release_jobs(release_id,service,kind,status,payload,"
                  "created,updated) VALUES(?,?,?,?,?,?,?)",
                  (release_id, "meta", "cover_reorders", "done",
                   json.dumps({"n": n, "last": now}), now, now))

    def _cover_transient_reorder(self, c, rel, reason: str) -> None:
        """Временный отказ сервиса обложек (429 общего ключа прокси, сеть) → ResourceDown.

        Заказывает обложку заново не чаще раза в COVER_REORDER_GAP_SEC и не больше
        COVER_REORDER_MAX раз; между заказами выпуск просто ждёт. 19.09.2026 без этого три
        выпуска подряд ушли в разбор, пока ключ прокси был перегружен. Сбой не временный
        или запас перезаказов кончился — возвращается, и вызывающий решает сам.
        """
        done_n, last = self._cover_reorders(c, rel["release_id"])
        if not _COVER_TRANSIENT.search(reason) or done_n >= COVER_REORDER_MAX:
            return
        now = self._clock()
        if last and now - last < COVER_REORDER_GAP_SEC:
            raise ResourceDown("обложки", "временный сбой сервиса обложек — закажу заново "
                               + time.strftime("%H:%M", time.localtime(last + COVER_REORDER_GAP_SEC)))
        c.execute("DELETE FROM pub_release_jobs WHERE release_id=? AND service='cover'"
                  " AND kind='job'", (rel["release_id"],))
        self._note_cover_reorder(c, rel["release_id"], done_n + 1, now)
        log_line(c, rel["release_id"], "обложка",
                 f"{COVER_REORDER_MARK} ({done_n + 1}/{COVER_REORDER_MAX}): {reason[:300]}")
        raise ResourceDown("обложки", "временный сбой сервиса обложек — заказываю заново")

    def do_cover_ready(self, c, rel):
        """Обложка по заголовку ролика. Деградировавшую (текст оверлеем) не принимаем."""
        preset = rel["preset_cover"]
        if not preset:
            raise ItemFailed("у канала не выбран пресет обложек")
        key = get_key(c, "cover")
        if not key:
            raise ResourceDown("обложки", "не сохранён ключ доступа к обложкам")
        client = CoverClient(key)
        known = self._remote(c, rel["release_id"], "cover")

        if known is None:
            if not rel["donor_url"]:
                raise ItemFailed("у выпуска нет донорской ссылки для обложки")
            job_id = client.create_job(preset, rel["lang"] or "ru", rel["donor_url"])
            self._remember_remote(c, rel["release_id"], "cover", job_id, rel["updated"])
            log_line(c, rel["release_id"], "обложка",
                     f"заказал по ссылке донора, пресет «{preset}», задача {job_id}")
            return {"wait": True}

        info = client.job(known["remote_job_id"])
        state = str(info.get("status") or "").lower()
        if state in ("error", "failed", "cancelled"):
            reason = "обложка не сделалась: " + str(info.get("error") or state)
            self._finish_remote(c, rel["release_id"], "cover", "error", reason)
            self._cover_transient_reorder(c, rel, reason)
            raise ItemFailed(reason)
        # Завершение опознаём по семейству статуса, а не по точному списку: сосед умеет
        # ещё и «completed_partial» (нарисовал не всё), и такой статус, не попав ни в
        # успех, ни в отказ, оставлял выпуск ждать вечно — до срока в шесть часов.
        finished = (state.startswith(("done", "completed", "finished", "success"))
                    or bool(info.get("finished_at")))
        if not finished:
            return {"wait": True}
        made = int(info.get("results_count") or 0) or len(info.get("output_files") or [])
        if not made:
            # Пустой результат с временной ошибкой соседа (сервис пишет completed_partial,
            # 0/1 и 429 прокси) — тот же перезаказ по окну, что и при статусе error.
            if info.get("error"):
                self._cover_transient_reorder(
                    c, rel, "обложка вышла пустой: " + str(info.get("error")))
            # Пустой результат — почти всегда занятый сосед по общему генератору картинок,
            # а не негодный ролик. Один раз заказываем заново; второй пустой ответ уже
            # считаем отказом, чтобы выпуск не крутился вечно.
            if known["error"] in ("re-ordered", "re-ordered-degraded"):
                reason = ("обложка не нарисовалась дважды: "
                          + str(info.get("progress_detail") or state))
                self._finish_remote(c, rel["release_id"], "cover", "error", reason)
                raise ItemFailed(reason)
            job_id = client.create_job(preset, rel["lang"] or "ru", rel["donor_url"])
            c.execute("UPDATE pub_release_jobs SET remote_job_id=?, status='running', "
                      "error='re-ordered', "
                      "updated=? WHERE release_id=? AND service='cover' AND kind='job'",
                      (job_id, rel["updated"], rel["release_id"]))
            log_line(c, rel["release_id"], "обложка",
                     f"пустой результат ({info.get('progress_detail') or state}) — "
                     f"заказал заново, задача {job_id}")
            return {"wait": True}
        if cover_degraded(info):
            # Брак рисунка — тоже повод переспросить, а не хоронить выпуск: под нагрузкой
            # генератор картинок срывается чаще, а со второго раза обычно рисует нормально.
            if known["error"] in ("re-ordered", "re-ordered-degraded"):
                reason = "обложка деградировала дважды: текст наложен поверх картинки"
                self._finish_remote(c, rel["release_id"], "cover", "error", reason)
                raise ItemFailed(reason)
            job_id = client.create_job(preset, rel["lang"] or "ru", rel["donor_url"])
            c.execute("UPDATE pub_release_jobs SET remote_job_id=?, status='running', "
                      "error='re-ordered-degraded', "
                      "updated=? WHERE release_id=? AND service='cover' AND kind='job'",
                      (job_id, rel["updated"], rel["release_id"]))
            log_line(c, rel["release_id"], "обложка",
                     f"текст лёг поверх картинки — заказал заново, задача {job_id}")
            return {"wait": True}

        try:
            data = client.download(known["remote_job_id"], rel["lang"] or "ru")
            if len(data) < 5000:
                raise ItemFailed("файл обложки подозрительно мал")
            path = os.path.join(self.project_dir(rel["tenant_id"], rel["project_id"]), "cover.jpg")
            with open(path, "wb") as f:
                f.write(data)
        except Exception as exc:
            self._finish_remote(c, rel["release_id"], "cover", "error", str(exc))
            raise
        self._finish_remote(c, rel["release_id"], "cover", "done")
        log_line(c, rel["release_id"], "обложка", f"файл получен, {len(data) // 1024} КБ")
        return {"payload": {"cover_bytes": len(data)}}

    def _title(self, c, rel) -> str:
        """Текст для обложки — из карточки ролика, которую делает стадия `cards`.

        Своего заголовка не выдумываем: «Канал · 17.08» на обложке хуже, чем честное
        ожидание. Пока карточка не готова, обложка не заказывается.
        """
        row = c.execute(
            "SELECT payload FROM pub_release_jobs WHERE release_id=? AND service='meta'"
            " AND kind='card'", (rel["release_id"],)).fetchone()
        if not row or not row["payload"]:
            raise ResourceDown("метаданные",
                               "заголовок ролика ещё не сделан — стадия карточек не подключена")
        try:
            card = json.loads(row["payload"])
        except Exception:
            raise ItemFailed("карточка ролика повреждена")
        lines = card.get("thumbnail_lines")
        if isinstance(lines, list) and lines:
            text = " ".join(str(x) for x in lines if x)
        else:
            text = str(card.get("title") or "")
        if not text.strip():
            raise ItemFailed("в карточке ролика нет текста для обложки")
        return text.strip()[:200]

    def store_card(self, c, release_id: str, card: dict, now: float) -> None:
        """Сохранить карточку ролика (заголовок, описание, текст обложки)."""
        c.execute(
            "INSERT OR REPLACE INTO pub_release_jobs(release_id,service,kind,status,payload,"
            "created,updated) VALUES(?,?,?,?,?,?,?)",
            (release_id, "meta", "card", "done",
             json.dumps(card, ensure_ascii=False), now, now))

    # ── заливка ──

    def _upload_job(self, c, release_id: str):
        return c.execute("SELECT * FROM pub_upload_jobs WHERE release_id=?",
                         (release_id,)).fetchone()

    def _deferred_room(self, c, rel) -> bool:
        """Есть ли место в отложке канала. Лимит исчерпан — готовый ролик ждёт, не льём."""
        limit = int(rel["deferred_limit"] or 0)
        if limit <= 0:
            return True
        waiting = c.execute(
            "SELECT COUNT(*) FROM pub_upload_jobs u JOIN pub_releases r "
            "ON r.release_id=u.release_id WHERE r.channel_id=? AND u.verdict='scheduled' "
            "AND u.slot_at > ?", (rel["channel_id"], self._clock())).fetchone()[0]
        return waiting < limit

    def _clock(self) -> float:
        import time as _t
        return _t.time()


    def _reslot(self, c, rel) -> str:
        """Перенести выпуск на ближайший свободный выход канала. Возвращает время или ''.

        Слот занимаем и в строке плана, и в выпуске: иначе движок продолжил бы считать
        временем выхода прошедший момент, а заливка — отказываться его планировать.
        """
        import datetime as dt
        import json as _json
        from channel_slots import upcoming_slots, ScheduleError

        ch = c.execute("SELECT * FROM pub_channels WHERE id=?", (rel["channel_id"],)).fetchone()
        if not ch:
            return ""
        try:
            schedule = _json.loads(ch["schedule"] or "{}")
            if not schedule:
                return ""
            now = dt.datetime.fromtimestamp(self._clock(), dt.timezone.utc)
            slots = upcoming_slots(schedule, ch["tz"], int(ch["horizon_days"] or 3), now)
        except (ScheduleError, ValueError):
            return ""
        busy = {round(r["slot_at"]) for r in c.execute(
            "SELECT slot_at FROM pub_plan_items WHERE channel_id=? AND slot_at IS NOT NULL "
            "AND status IN ('planned','frozen','running','done','error')",
            (rel["channel_id"],)).fetchall() if r["slot_at"]}
        for point in slots:
            stamp = round(point.timestamp())
            if stamp in busy or point.timestamp() <= self._clock() + 1800:
                continue
            c.execute("UPDATE pub_plan_items SET slot_at=?, updated=? WHERE id=?",
                      (stamp, self._clock(), rel["plan_item_id"]))
            c.execute("UPDATE pub_releases SET slot_at=?, updated=? WHERE release_id=?",
                      (stamp, self._clock(), rel["release_id"]))
            return point.strftime("%d.%m %H:%M")
        return ""

    def do_upload_pending(self, c, rel):
        """Собрать комплект и поставить задание машине с профилем.

        Комплект собирается ОДИН раз: если задание уже есть, повторный тик его не создаёт.
        """
        if self._upload_job(c, rel["release_id"]) is not None:
            return {}
        if not rel["profile_id"]:
            raise ItemFailed("у канала не указан профиль AdsPower — заливать нечем")
        if not rel["project_id"]:
            raise ItemFailed("у выпуска нет проекта")

        deferred = bool(rel["deferred"] if rel["deferred"] is not None else 1)
        # Ролик не успел к своему времени: запланировать прошедший момент YouTube не даст.
        # Раньше здесь выпуск останавливался и ждал, пока владелец переставит его руками —
        # то есть готовый ролик пропадал из-за того, что производство заняло дольше, чем
        # предполагал план. Переставляем сами на ближайший свободный выход канала.
        if deferred and rel["slot_at"] <= self._clock() + 600:
            moved = self._reslot(c, rel)
            if not moved:
                raise ItemFailed("к времени выхода не успели, и свободных выходов "
                                 "в горизонте канала нет — добавьте времена выхода")
            log_line(c, rel["release_id"], "движок",
                     f"к прежнему времени не успели — перенёс выход на {moved}")
            return {"wait": True}
        if deferred and not self._deferred_room(c, rel):
            raise ResourceDown("отложка",
                               f"в отложке канала уже {rel['deferred_limit']} роликов — ждём места")

        proj = self.project_dir(rel["tenant_id"], rel["project_id"])
        render_job = (self._jobs(c, rel["release_id"]).get("render") or {})
        video = find_video(proj, render_job["remote_job_id"] if render_job else None)
        if not video:
            raise ItemFailed("готового mp4 в проекте нет")
        cover = os.path.join(proj, "cover.jpg")
        if not os.path.exists(cover):
            cover = ""

        # Генератор названий только что отказал — не ходим к нему заново до конца паузы.
        # Иначе каждый выпуск тика честно ждал свой 429 (очередь ключа прокси), тик
        # растягивался на 19 минут, и за это время успешные заливки уходили в разбор.
        pause_until = getattr(self, "_title_pause_until", 0.0)
        if self._clock() < pause_until:
            raise ResourceDown("название", "генератор названий недавно отказал — пауза до "
                               + time.strftime("%H:%M", time.localtime(pause_until)))
        try:
            title = make_title(rel["donor_title"] or "", rel["lang"] or "ru",
                               self._title_llm(c))
            chapters = ""
            if chapters_enabled(self._preset_settings(c, rel["preset_vsgen_id"])):
                chapters = read_chapters(proj)
            description = make_description(title, chapters)
            schedule = schedule_iso(rel["slot_at"], rel["tz"]) if deferred else ""
        except TitleUnavailable as exc:
            # Генератор названий временно недоступен (ключ/лимит/сеть) — ролик ЖДЁТ,
            # а не умирает: один 429 не повод хоронить готовый выпуск.
            self._title_pause_until = self._clock() + TITLE_PAUSE_SEC
            raise ResourceDown("название", str(exc))
        except TitleError as exc:
            raise ItemFailed(str(exc))

        now = self._clock()
        c.execute(
            "INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,host_id,"
            "channel_url,title,description,schedule_iso,visibility,collaborator,video_path,"
            "cover_path,slot_at,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rel["release_id"], rel["tenant_id"], rel["channel_id"], rel["uc"],
             rel["profile_id"], rel["host_id"],
             f'https://studio.youtube.com/channel/{rel["uc"]}/',
             title, description, schedule, "public", rel["collaborator"] or "",
             video, cover, rel["slot_at"], now, now))
        log_line(c, rel["release_id"], "заливка",
                 f"комплект готов: «{title}», выход {schedule or 'сразу'}, "
                 f"обложка {'есть' if cover else 'нет'}, профиль {rel['profile_id']}")
        return {"payload": {"title": title, "schedule": schedule}}

    def do_upload_running(self, c, rel):
        """Ждём, пока машина реально начнёт заливать."""
        job = self._upload_job(c, rel["release_id"])
        if job is None:
            raise ItemFailed("задание на заливку исчезло")
        if job["status"] == "error":
            # Тот же самый безопасный отказ (файл в YouTube не уходил) раньше чинился
            # сам только на фазе «scheduled», а здесь звал человека: какая фаза поймает
            # отказ, решал случай. Одинаковый разбор в обоих местах — иначе половина
            # ошибок лечится, а половина ложится в разбор без всякой разницы по сути.
            if self._retry_upload_before_handoff(c, rel, job):
                return {"wait": True}
            raise ItemFailed(job["error"] or "заливка не удалась")
        if job["status"] == "queued":
            return {"wait": True}
        return {}                       # взято в работу — дальше фаза необратима

    def do_scheduled(self, c, rel):
        """Вердикт: ролик подтверждённо стоит в расписании — или нет."""
        job = self._upload_job(c, rel["release_id"])
        if job is None:
            raise ItemFailed("задание на заливку исчезло")
        if job["verdict"] in ("scheduled", "published"):
            return {"video_id": job["video_id"] or "",
                    "evidence": json.loads(job["evidence"]) if job["evidence"] else None}
        if job["status"] == "repair":
            raise ItemFailed(job["error"] or "ролик залит, но Studio не подтвердила — нужен разбор")
        if job["status"] == "error":
            if self._retry_upload_before_handoff(c, rel, job):
                return {"wait": True}
            raise ItemFailed(job["error"] or "заливка не удалась")
        return {"wait": True}

    def _retry_upload_before_handoff(self, c, rel, job) -> bool:
        """Вернуть задание в очередь, если файл в YouTube ещё не уходил. True — повторяем.

        Необратимость определяет движок по фазам, которые прислала машина, и по номеру
        ролика: preflight/profile_opened — файл не отправлялся. После начала передачи
        повтор запрещён навсегда, тут ничего не меняется.
        """
        rid = rel["release_id"]
        # Две независимые улики должны СОГЛАСИТЬСЯ, что файл не уходил: фаза, которую
        # прислала сама машина, и журнал выпуска. Журнала одного мало — строка с фазой
        # «upload_started» без события в журнале иначе выглядела бы безопасной.
        said = ""
        try:
            said = str((json.loads(job["phase"]) or {}).get("phase") or "") if job["phase"] else ""
        except Exception:                                  # noqa: BLE001 — старая обрезанная запись
            found = re.search(r'"phase"\s*:\s*"([a-z_]+)"', str(job["phase"]))
            said = found.group(1) if found else "неразборчиво"
        if said not in ("", "preflight", "profile_opened"):
            return False
        if job["video_id"] or reached_irreversible(c, rid):
            return False
        done_n, last = self._upload_retries(c, rid)
        if done_n >= UPLOAD_RETRY_MAX:
            return False
        now = self._clock()
        if last and now - last < UPLOAD_RETRY_GAP_SEC:
            return True                     # пауза между повторами: ждём, не сдаёмся
        c.execute("UPDATE pub_upload_jobs SET status='queued', verdict=NULL, error=NULL, phase=NULL,"
                  " claim_token=NULL, host=NULL, worker=NULL, claimed=NULL, lease_until=NULL,"
                  " heartbeat=NULL, evidence=NULL, finished=NULL, updated=? WHERE release_id=?"
                  " AND status='error'", (now, rid))
        c.execute("INSERT OR REPLACE INTO pub_release_jobs(release_id,service,kind,status,payload,"
                  "created,updated) VALUES(?,?,?,?,?,?,?)",
                  (rid, "meta", "upload_retries", "done",
                   json.dumps({"n": done_n + 1, "last": now}), now, now))
        log_line(c, rid, "заливка",
                 f"{UPLOAD_RETRY_MARK} ({done_n + 1}/{UPLOAD_RETRY_MAX}): файл в YouTube не уходил "
                 f"({(job['error'] or '')[:120]})")
        return True

    def _upload_retries(self, c, release_id: str) -> tuple:
        row = c.execute("SELECT payload FROM pub_release_jobs WHERE release_id=? AND service='meta'"
                        " AND kind='upload_retries'", (release_id,)).fetchone()
        try:
            data = json.loads(row["payload"]) if row and row["payload"] else {}
        except Exception:
            data = {}
        return int(data.get("n") or 0), float(data.get("last") or 0.0)

    def do_published(self, c, rel):
        """Проверка фактического выхода — отдельным проходом, позже.

        До неё выпуск уже закрыт по подтверждённому расписанию, поэтому сюда движок
        обычно не заходит вовсе.
        """
        raise ResourceDown("проверка публикации", "проверка выхода в PUBLIC пока не подключена")

    def _title_llm(self, c) -> dict:
        """Адрес, ключ и модель прокси названий. Нет настройки — пустой словарь:
        make_title поднимет TitleUnavailable, ролик ждёт, а не портит название."""
        try:
            value = self.title_llm() or {}
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def _preset_settings(self, c, preset_id) -> dict:
        """Настройки пресета VideoStudio словарём.

        Пресет удалён, нечитаем или хранит мусор — пустой словарь: ролик уходит без
        таймкодов и на умолчаниях, а не умирает на заливке из-за чужой уборки.
        Возвращаем именно dict, а не сырую строку: настройки уходят и в проект тоже,
        и разбирать JSON в двух местах по-разному — верный способ разойтись.
        """
        if not preset_id:
            return {}
        try:
            row = c.execute("SELECT settings FROM presets WHERE id=?", (preset_id,)).fetchone()
        except Exception:
            return {}
        raw = (row["settings"] if row else "") or ""
        if isinstance(raw, dict):
            return raw
        try:
            value = json.loads(raw) if str(raw).strip() else {}
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    # ── источник текста ──

    def _source_text(self, c, rel) -> str:
        """Текст, отданный стадией рерайта. Если проект уже был — берём из его папки."""
        row = c.execute(
            "SELECT payload FROM pub_release_jobs WHERE release_id=? AND service='rewrite'"
            " AND kind='text'", (rel["release_id"],)).fetchone()
        if row and row["payload"]:
            try:
                return (json.loads(row["payload"]).get("text") or "").strip()
            except Exception:
                pass
        if rel["project_id"]:
            path = os.path.join(self.project_dir(rel["tenant_id"], rel["project_id"]),
                                "source.txt")
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                return ""
        return ""


def describe_waiting(reason: str) -> str:
    """Причина ожидания человеческим языком для карточки канала."""
    if not reason:
        return ""
    text = reason.replace("ждём: ", "")
    return text[:200]
