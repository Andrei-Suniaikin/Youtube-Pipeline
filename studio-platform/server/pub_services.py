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

import preset_fields
import preset_links
import hashlib
import json
import os
import sqlite3
import time

import pub_artifact
import pub_calls
from pub_engine import ItemFailed, ResourceDown
# Фазу выпуска двигает не только движок: проход «забрать уже оплаченное» (в конце
# файла) доводит до `source_ready` текст, который сосед написал ещё во время паузы.
# Охрана по статусу («владелец мог отменить выпуск, пока мы ходили в сеть») обязана
# быть одна на всех, поэтому берём ту же функцию, а не переписываем UPDATE рядом.
from pub_engine import DEFAULT_DEADLINE, PHASE_DEADLINES, log_phase
from pub_engine import RESLOT_MAX_DAYS, UPLOAD_LEAD_SEC, ready_slot_horizon
from pub_engine import _set as set_release_fields
from pub_log import add as log_line
from pub_neighbors import rewrite_preset_config
from pub_publish import (
    TitleError,
    TitleUnavailable,
    VideoAmbiguous,
    chapters_enabled,
    find_video,
    make_description,
    make_title,
    make_title_codex,
    read_chapters,
    schedule_iso,
)
from pub_remote import (
    REWRITE_BASE,
    CoverClient,
    RemoteJobMissing,
    RewriteClient,
    cover_degraded,
    cover_unchecked,
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

# Приёмка текста рерайта: какую долю донора обязан сохранить результат.
#
# Почему 0,6. Рерайт — пересказ, а не перевод: нормальный результат держится около
# длины донора или длиннее. При этом честно короче он быть может — субтитры идут без
# пунктуации, с повторами строк и служебными вставками, и аккуратный пересказ
# ужимается процентов на двадцать-тридцать. Порог 0,6 оставляет этот запас и ловит
# случай из боя 18.09: донор 3271 слово, результат 1332 (46%) со статусом «Готово».
# Цена пропуска — не один испорченный текст, а три оплаченные стадии следом
# (озвучка, картинки, рендер) и ролик-половинка на канале.
#
# Ноль отключает проверку целиком: осознанный откат без правки кода.
TEXT_MIN_RATIO = float(os.environ.get("VS_TEXT_MIN_RATIO", "0.6"))

# Абсолютный пол длины: текст короче просто не о чем озвучивать, каким бы коротким
# ни был донор. Проверка существовала и до доли — она остаётся как была.
TEXT_MIN_CHARS = int(os.environ.get("VS_TEXT_MIN_CHARS", "500"))


# Состояния задачи рерайта, из которых её можно ВОЗОБНОВИТЬ тем же номером: сосед
# дописывает только незавершённые куски (`/jobs/{id}/retry`). «Прерванную» он сам не
# поднимает никогда — у него это конечное состояние после рестарта.
REWRITE_RESUMABLE = ("interrupted", "failed", "partial", "cancelled")
# Сколько раз подряд движок возобновляет одну задачу без человека. Дальше — разбор:
# «вернуть в работу» обнуляет счётчик (pub_engine.revive).
REWRITE_MAX_RESUMES = 3


class PubServices:
    """Обработчики фаз. Каждый метод возвращает {} (шаг сделан) или {"wait": True}."""

    def __init__(self, ctx):
        self.create_project = ctx["create_project"]
        self.enqueue = ctx["enqueue"]
        self.project_dir = ctx["project_dir"]
        self.owner_uid = ctx.get("owner_uid")
        self.default_username = ctx.get("username") or ""
        self.openai_key = ctx.get("openai_key") or (lambda: "")
        # Названия пишутся в фоне: выпуск → {"thread", "title", "error", "fatal"}.
        import threading as _threading
        self._titles = {}
        self._titles_lock = _threading.Lock()

    # 04.10.2026: названия пишет подписка Codex. Её места делят все службы машины, и
    # ответ шёл 4–8 минут (ожидание места плюс сам вызов). Внутри круга движка это
    # останавливало все остальные выпуски: у кого срок фазы короче (project_ready — 15
    # минут), засчитывали попытку каждый круг, не дойдя до своего обработчика. Сначала
    # был бюджет времени на круг; этого мало — один вызов длиннее любого бюджета. Теперь
    # круг только заказывает название и забирает готовое, а пишется оно в фоне, не больше
    # TITLE_WORKERS одновременно.
    TITLE_WORKERS = int(os.environ.get("VS_TITLE_WORKERS", "2") or 2)
    # Быстрый ответ (место в подписке свободно) забираем в том же круге, не откладывая.
    TITLE_GRACE_SEC = float(os.environ.get("VS_TITLE_GRACE_SEC", "2") or 2)

    def begin_tick(self, now: float = 0.0) -> None:
        """Движок начинает новый круг. Названиям бюджет больше не нужен — они в фоне."""

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

    def _remember_remote(self, c, release_id: str, service: str, job_id: str, now: float,
                         donor_chars: int = 0):
        """Запомнить задачу соседа. `donor_chars` — длина исходника в момент заказа.

        Почему длина донора живёт здесь, а не в `provider_calls`. Журнал платных
        вызовов отвечает на вопрос «платили ли мы за это» — у него фиксированный набор
        колонок и своя уникальность, и дописывать туда мерку приёмки значило бы менять
        общую схему ради одной стадии. А строка `pub_release_jobs` — это и есть наша
        копия «что мы заказали соседу»: у неё уже есть свободное поле `payload`, она
        живёт ровно столько же, сколько задача, и читается там же, где забирается
        результат. Донор на момент получения текста недоступен (субтитры качались тиком
        раньше), поэтому запомнить его надо именно при заказе.
        """
        blob = (json.dumps({"donor_chars": int(donor_chars)}, ensure_ascii=False)
                if donor_chars > 0 else None)
        try:
            c.execute(
                "INSERT OR REPLACE INTO pub_release_jobs(release_id,service,kind,"
                "remote_job_id,status,payload,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (release_id, service, "job", str(job_id), "running", blob, now, now))
        except sqlite3.OperationalError:
            # База старше колонки `payload`. Длина донора — мерка приёмки, а не условие
            # работы: без неё проверка доли молчит, но выпуск идёт своим ходом.
            c.execute(
                "INSERT OR REPLACE INTO pub_release_jobs(release_id,service,kind,"
                "remote_job_id,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                (release_id, service, "job", str(job_id), "running", now, now))

    @staticmethod
    def _donor_chars(row) -> int:
        """Длина донора, запомненная при заказе. 0 — «не знаем», а не «ноль знаков»."""
        try:
            raw = row["payload"]
        except (KeyError, IndexError, TypeError):
            return 0
        try:
            return int((json.loads(raw) or {}).get("donor_chars") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0

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
        if known is not None and known["status"] == "missing":
            # Задача, за которую уже заплачено, у соседа не нашлась. Молча заказать
            # заново — заплатить второй раз за тот же текст, поэтому строка стоит до
            # решения человека и причина видна прямо в ней.
            raise ItemFailed(known["error"] or "сохранённая задача рерайта отсутствует, "
                                               "нужен разбор: повтор оплачивается заново")
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
            # asr=True: дорожки нет, а ролик на месте — текст распознаётся на Минске,
            # выпуск ждёт (ResourceDown), без людей (решение владельца 03.10.2026).
            text = donor_text(rel["donor_url"], asr=True)
            # 04.09.2026: рерайт подключён к журналу платных вызовов — на уровне
            # (выпуск, язык), а не куска текста. Это ровно та единица, за которую платит
            # кабинет: одна задача рерайта. Внутри неё Rewrite сам не переплачивает —
            # у него есть пофрагментное продолжение (PROGRESS_MANAGER, «обрабатываем
            # только незавершённые чанки»). А вот между запусками защиты не было:
            # тот же текст, тот же пресет, новая задача — и оплата заново.
            lang = rel["lang"] or "ru"
            # 04.09.2026: по общему идентификатору, а не по логину владельца тенанта.
            preset = rewrite_preset_config(rel["tenant_id"], rel["preset_rewrite"] or "")
            # Решение владельца 01.10.2026, п. 5: сначала ПРЕДУПРЕЖДЕНИЕ, не отказ.
            #
            # Ненайденный пресет рерайта — это не ошибка, а пустой словарь: задача
            # уходит на зашитых умолчаниях (gpt-4.1-nano, куски по 3500, без ведущего),
            # а в журнал раньше писалось «пресет «<выбранное имя>»» — то есть успех,
            # отличить который от настоящего было нельзя ни запросом, ни глазами.
            # Отказом это делать нельзя: каналы, годами работающие на умолчаниях с
            # непустым полем пресета, встали бы все разом на первом же тике. Поэтому
            # честная строка уровня warn — и видно, и ничего не остановлено.
            if (rel["preset_rewrite"] or "").strip() and not preset:
                log_line(c, rel["release_id"], "рерайт",
                         f"пресет «{rel['preset_rewrite']}» у этого владельца не найден — "
                         f"задача уйдёт на умолчаниях рерайта, а не на настройках канала",
                         level="warn")
            # Модель выясняем ЗДЕСЬ — до брони и до загрузки донора соседу. Раньше её
            # выбирал сам `create_job`, то есть уже после `pub_calls.reserve` и после
            # того, как весь донор уехал к соседу: денег это не стоило, но каждый тик
            # зря гонял файл по сети и открывал строку журнала заново.
            #
            # Отказ держим в руках, а не бросаем сразу. Ветки «уже оплачен» и «уже
            # заказан» ниже модель не заказывают — им нужно только подхватить чужой
            # результат, и уронить их из-за недоступного списка моделей значило бы
            # бросить оплаченный текст (те самые 146 из разбора 18.09). Поэтому сначала
            # разбираем бронь, и только на ветке настоящего заказа отказ становится
            # отказом. А вот ResourceDown (сосед лёг) отсюда уходит наверх как есть:
            # у лежачего рерайта и результат не заберёшь, ждать должна вся очередь.
            try:
                model, model_error = client.resolve_model(preset), None
            except ItemFailed as exc:
                model, model_error = "", exc
            # Отпечаток намеренно остаётся на модели ПРЕСЕТА, хотя заказ с 19.09 может
            # уйти на модели, подсказанной соседом (pub_remote.resolve_model). Отпечаток
            # отвечает на один вопрос: «за это уже платили?». Добавить туда вычисленную
            # модель значило бы объявить новыми все строки, где пресет молчит, — а это
            # ровно те 146 оплаченных и не забранных текстов из разбора 18.09. Смена
            # модели В ПРЕСЕТЕ отпечаток меняет, как и меняла.
            text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            fp = pub_calls.fingerprint(
                text=text_sha,
                lang=lang, preset=rel["preset_rewrite"] or "",
                model=(preset or {}).get("model") or "")
            decision = pub_calls.reserve(
                c, tenant_id=rel["tenant_id"], project_id=rel["project_id"] or "",
                release_id=rel["release_id"], kind="text", unit=lang, fp=fp,
                provider="rewrite", model=(preset or {}).get("model") or "")
            c.commit()
            if decision["action"] == "skip":
                # Тот же текст с теми же настройками уже оплачен и получен. Второй заказ
                # был бы вторыми деньгами за тот же результат.
                done = (decision["row"]["result_path"]
                        or decision["row"]["remote_order_id"] or "")
                log_line(c, rel["release_id"], "рерайт",
                         f"этот текст на «{lang}» уже оплачен раньше "
                         f"(задача {done or 'неизвестна'}) — новый заказ не создаю",
                         level="warn")
                if done:
                    # Донора передаём и здесь: подхват уже оплаченной задачи всё равно
                    # закончится приёмкой текста на следующем тике, а без длины донора
                    # приёмка по доле промолчит — ровно на тех выпусках, ради которых
                    # она и писалась.
                    self._remember_remote(c, rel["release_id"], "rewrite", done,
                                          rel["updated"], donor_chars=len(text))
                    return {"wait": True}
                raise ItemFailed("рерайт числится оплаченным, но задача не записана — "
                                 "нужен разбор, повторно платить нельзя")
            if decision["action"] == "verify":
                # Заказ создавался, а результата мы не видели. Второй заказ — вторые
                # деньги, поэтому подхватываем существующий по его id.
                was = decision["row"]["remote_order_id"] or ""
                if not was:
                    raise ResourceDown("рерайт", "заказ создавался, id неизвестен — "
                                                 "повторно платить нельзя до разбора")
                log_line(c, rel["release_id"], "рерайт",
                         f"задача {was} уже заказана раньше — подхватываю её, "
                         f"нового заказа не создаю", level="warn")
                # Та же причина, что и в ветке «уже оплачен»: текст по этой задаче мы
                # ещё будем принимать, и длина донора нужна именно сейчас.
                self._remember_remote(c, rel["release_id"], "rewrite", was,
                                      rel["updated"], donor_chars=len(text))
                return {"wait": True}
            if model_error is not None:
                # Настоящий заказ без выбранной модели невозможен. Строку журнала
                # закрываем как «до провайдера не дошли»: денег нет, повтор безопасен,
                # и следующий тик после правки пресета пойдёт заново.
                pub_calls.mark_failed(c, decision["call_id"], str(model_error)[:400],
                                      ordered=False)
                c.commit()
                raise model_error
            file_id = client.upload_text(text, filename=f'{rel["release_id"]}.txt')
            try:
                job_id = client.create_job(
                    file_id, lang, preset, channel_name=rel["ch_name"] or "",
                    idempotency_key=f'release:{rel["release_id"]}', model=model,
                    # Тот же хэш донора, что и в отпечатке платежа, но работает он на
                    # другой вопрос. Отпечаток решает «за это уже платили?», ключ
                    # повтора — «это тот же самый запрос?». Сосед с 18.09 сравнивает
                    # ТЕЛО заказа, а `file_id` у него новый на каждую загрузку, поэтому
                    # в ключ идёт содержимое, а не номер файла (pub_remote._order_digest).
                    source_sha=text_sha)
            except Exception as exc:
                # Заказа нет — денег нет: строку открываем заново, повтор безопасен.
                pub_calls.mark_failed(c, decision["call_id"], str(exc)[:400],
                                      ordered=False)
                c.commit()
                raise
            # Задача создана — с этого момента она стоит денег, и её id это чек.
            # «Готово» ставится позже, когда текст действительно забран: заказ и
            # результат — разные события, и путать их значит потерять разницу между
            # «оплачено» и «получено».
            pub_calls.mark_ordered(c, decision["call_id"], job_id)
            c.commit()
            # Длину донора запоминаем вместе с задачей: при получении текста её уже
            # неоткуда взять, а без неё приёмку по доле делать не из чего.
            self._remember_remote(c, rel["release_id"], "rewrite", job_id,
                                  rel["updated"], donor_chars=len(text))
            # В журнал — ФАКТИЧЕСКИ применённое, а не выбранное имя пресета (решение
            # владельца, п. 5): по «пресет «X»» нельзя было понять, применился он или
            # молча не нашёлся.
            ведущий = str((preset or {}).get("host_name") or "").strip()
            log_line(c, rel["release_id"], "рерайт",
                     f"субтитры донора {len(text)} символов, заказал задачу {job_id}, "
                     f"пресет «{rel['preset_rewrite'] or 'не выбран'}»"
                     f"{'' if preset else ' (не найден, умолчания)'}, "
                     f"модель {model or 'по выбору рерайта'}, "
                     f"ведущий {ведущий or 'не задан'}")
            return {"wait": True}

        try:
            info = client.job(known["remote_job_id"])
        except RemoteJobMissing as exc:
            # Помечаем задачу пропавшей: иначе следующий тик увидел бы её «running» и
            # спросил бы снова, а ручной сброс строки привёл бы к заказу дубля.
            self._close_text_call(c, rel, error=str(exc))
            self._finish_remote(c, rel["release_id"], "rewrite", "missing", str(exc))
            log_line(c, rel["release_id"], "рерайт", str(exc), level="error")
            raise
        state = str(info.get("status") or "").lower()
        if state in REWRITE_RESUMABLE and (state == "interrupted"
                                           or known["status"] == "error"):
            # Прерванную задачу поднимаем сами; упавшую — только после того, как
            # человек вернул выпуск в работу (строка-зеркало уже стоит в «error»).
            return self._resume_rewrite(c, rel, known, info, client)
        if state in ("error", "failed", "cancelled", "partial"):
            reason = "рерайт не справился: " + str(info.get("error") or state)
            self._close_text_call(c, rel, error=reason)
            self._finish_remote(c, rel["release_id"], "rewrite", "error", reason)
            raise ItemFailed(reason)
        if state not in ("done", "completed", "finished", "success"):
            if (state in ("pending", "queued", "running", "processing")
                    and (known["status"] or "") not in ("running", "queued")):
                # Задачу подняли снова (возвратом в работу или кнопкой «повторить»), и
                # она уже стоит у сервиса текста. Зеркало держало старое «error» с
                # прежней причиной — экран показывал «ошибка» и значок «повторить», а
                # сервис на каждое нажатие отвечал «повтор только для упавших»
                # (04.10.2026, владелец: «почему не могу запустить»).
                c.execute("UPDATE pub_release_jobs SET status='running', error=NULL, "
                          "updated=? WHERE release_id=? AND service='rewrite' AND kind='job'",
                          (self._clock(), rel["release_id"]))
                log_line(c, rel["release_id"], "рерайт",
                         f"задача {known['remote_job_id']} снова в работе у сервиса текста "
                         f"(«{state}»): дописывает недописанные куски, написанное не теряется")
            return {"wait": True}

        donor_chars = self._donor_chars(known)
        try:
            code = client.result_code(info, rel["lang"] or "ru")
            text = client.result_text(known["remote_job_id"], code)
            self._check_text_length(c, rel, text, donor_chars)
            self.store_source_text(c, rel["release_id"], text, rel["updated"])
        except Exception as exc:
            self._close_text_call(c, rel, error=str(exc))
            self._finish_remote(c, rel["release_id"], "rewrite", "error", str(exc))
            raise
        self._close_text_call(c, rel, result=known["remote_job_id"])
        self._finish_remote(c, rel["release_id"], "rewrite", "done")
        # Одна мера длины на всю строку: и «сколько знаков», и доля считаются от
        # `text.strip()` — от того же числа, которым мерила приёмка. Раньше рядом
        # стояли len(text) и доля от len(text.strip()), и на тексте с хвостом пустых
        # строк человек делил одно на другое и не сходился.
        body = len(text.strip())
        share = (f", {body / donor_chars * 100:.1f}% донора" if donor_chars > 0 else "")
        log_line(c, rel["release_id"], "рерайт", f"текст готов, {body} символов{share}")
        return {"payload": {"chars": body}}

    def _check_text_length(self, c, rel, text: str, donor_chars: int) -> None:
        """Приёмка результата рерайта по длине. Не прошло — выпуск дальше не идёт.

        Зачем вообще. Единственной проверкой была нижняя планка в 500 знаков, а в бою
        18.09 случилось другое: донор 3271 слово, результат 1332 (46%) — и статус
        «Готово». Половина сюжета — это не короткий текст, это другой текст, и стоит
        он ровно столько же: следом идут озвучка, картинки и рендер, каждый за деньги.
        Дешевле остановиться здесь, чем получить оплаченный ролик-половинку.

        Почему не считаем слова: знаки уже есть под рукой в обоих концах и не зависят
        от языка — у польского и немецкого слова разной длины, доля по знакам честнее.
        """
        body = len(text.strip())
        if body < TEXT_MIN_CHARS:
            raise ItemFailed(f"рерайт вернул слишком короткий текст: {body} знаков "
                             f"при минимуме {TEXT_MIN_CHARS}")
        if TEXT_MIN_RATIO <= 0 or donor_chars <= 0:
            # Порог снят владельцем либо длина донора неизвестна: задача заказана до
            # появления этой мерки или база старше колонки `payload`. Молчим честно —
            # выдумать долю не из чего, а придираться к старым задачам не за что.
            return
        share = body / donor_chars
        if share >= TEXT_MIN_RATIO:
            return
        # Знак после запятой обязателен. С округлением до целых текст на 59,6% давал
        # «(60% при пороге 60%) — дальше не пускаю», то есть человек читал равенство
        # там, где сработало строгое «меньше», и шёл искать несуществующий баг.
        message = (f"текст короче донора: {body} знаков против {donor_chars} у донора "
                   f"({share * 100:.1f}% при пороге {TEXT_MIN_RATIO * 100:.1f}%) — "
                   f"дальше не пускаю, следом три оплачиваемых шага")
        # Пишем строку сами, отдельно от карантинной: человеку нужен шаг «рерайт» и
        # обе длины рядом, а не только итог «убрал в разбор».
        log_line(c, rel["release_id"], "рерайт", message, level="error")
        raise ItemFailed(message)

    @staticmethod
    def _resumes(known) -> int:
        """Сколько раз эту задачу соседа уже возобновляли (payload.resumes)."""
        try:
            raw = known["payload"]
        except (KeyError, IndexError, TypeError):
            return 0
        try:
            return int((json.loads(raw) or {}).get("resumes") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0

    def _note_resume(self, c, release_id: str, n: int) -> None:
        """Запомнить возобновление: счётчик в payload, зеркало снова «running»."""
        row = self._remote(c, release_id, "rewrite")
        try:
            payload = json.loads(row["payload"] or "{}") or {}
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        payload["resumes"] = int(n)
        c.execute(
            "UPDATE pub_release_jobs SET status='running', error=NULL, payload=?, "
            "updated=? WHERE release_id=? AND service='rewrite' AND kind='job'",
            (json.dumps(payload, ensure_ascii=False), self._clock(), release_id))

    def _resume_rewrite(self, c, rel, known, info, client):
        """Возобновить задачу рерайта ТЕМ ЖЕ номером вместо нового заказа.

        03.10.2026, ролики на час и длиннее. Две находки на бою:

        * рестарт службы переводит идущую задачу соседа в «interrupted» — у него это
          конечное состояние, сам он её не поднимает (job_queue.reap_stale_running).
          Для движка это не входило ни в «не справился», ни в «готово» и попадало в
          «ждём»: выпуск двенадцать часов числился работающим, до потолка HARD_STALL.
          Десять задач стояли так с 14/18, 13/23, 12/23 готовыми кусками;
        * «вернуть в работу» после «рерайт не справился» ничего не возвращало: зеркало
          задачи оставалось, движок опрашивал ту же упавшую задачу и тем же тиком снова
          ронял выпуск в разбор. Единственный обход — снести строку и заказать заново,
          то есть заплатить за все куски второй раз.

        Сосед умеет `/retry`: дописать только незавершённые куски той же задачи.
        Это не новый заказ (номер и деньги те же), поэтому журнал платных вызовов не
        трогаем, а считаем возобновления в payload зеркала — не больше
        REWRITE_MAX_RESUMES подряд, дальше честный провал с причиной словами.
        «Вернуть в работу» обнуляет счётчик (pub_engine.revive).
        """
        job_id = str(known["remote_job_id"] or "")
        state = str(info.get("status") or "").lower()
        # Словами человека, а не соседа. `progress_detail` у рерайта — его счётчик
        # ФАЙЛОВ («1/1 — mzyZGlue_r_0e1ec6aba2.txt (Polish)»), а не кусков: в журнале
        # выпуска он читался как загадка (владелец 03.10.2026: «что это значит?»).
        # «interrupted» рерайт ставит только своей задаче, шедшей в момент его
        # перезапуска (job_queue.reap_stale_running) — так и пишем.
        words = ("задача рерайта прервалась — рерайт перезапускали, пока он писал текст"
                 if state == "interrupted"
                 else f"рерайт не довёл задачу до конца (у него она «{state}»)")
        done = self._resumes(known)
        if done >= REWRITE_MAX_RESUMES:
            reason = (f"{words}; продолжал её {done} раз(а) подряд и не довёл — "
                      f"нужен разбор")
            self._close_text_call(c, rel, error=reason)
            self._finish_remote(c, rel["release_id"], "rewrite", "error", reason)
            raise ItemFailed(reason)
        try:
            client.retry(job_id)
        except ResourceDown:
            raise                 # сосед лежит — это «ждём», а не «в разбор»
        except Exception as exc:                                   # noqa: BLE001
            reason = f"рерайт не дал возобновить задачу {job_id}: {exc}"
            self._close_text_call(c, rel, error=reason)
            self._finish_remote(c, rel["release_id"], "rewrite", "error", reason)
            raise ItemFailed(reason) from exc
        try:
            self._note_resume(c, rel["release_id"], done + 1)
        except sqlite3.OperationalError as exc:
            # База старше колонки `payload`: без счётчика возобновлять нельзя — это
            # была бы вечная петля на задаче, которую сосед никогда не доделает.
            reason = f"возобновил задачу {job_id}, но считать возобновления негде: {exc}"
            self._finish_remote(c, rel["release_id"], "rewrite", "error", reason)
            raise ItemFailed(reason) from exc
        log_line(c, rel["release_id"], "рерайт",
                 f"{words}. Студия продолжила ту же задачу с места остановки: уже "
                 f"написанные куски текста не переписываются и второй раз не оплачиваются "
                 f"(продолжение {done + 1} из {REWRITE_MAX_RESUMES})",
                 level="warn")
        return {"wait": True}

    def _close_text_call(self, c, rel, result: str = "", error: str = "") -> None:
        """Закрыть строку журнала за этот текст: получено или не получено.

        Строку ищем по выпуску и языку, а не по отпечатку: на этом тике исходного
        текста уже нет под рукой, а пара (выпуск, язык) определяет её однозначно.

        Отказ закрывается как `ordered=True`: заказ был создан, деньги потрачены.
        Повтор должен спросить про существующий заказ, а не создать новый.
        """
        lang = rel["lang"] or "ru"
        try:
            row = c.execute(
                "SELECT id FROM provider_calls WHERE release_id=? AND kind='text' "
                "AND unit=? ORDER BY id DESC LIMIT 1",
                (rel["release_id"], lang)).fetchone()
            if not row:
                return
            if result:
                pub_calls.mark_delivered(c, row["id"], result)
            else:
                pub_calls.mark_failed(c, row["id"], (error or "текст не получен")[:400],
                                      ordered=True)
            c.commit()
        except Exception:                                          # noqa: BLE001
            pass          # журнал не должен ронять сам выпуск

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
            # `_preset_settings` уже разрешает ссылки частичных пресетов
            # (`preset_links.resolve`) — здесь остаётся только убрать служебные
            # ключи `_scope`/`_links`/`_link_problems`, чтобы они не уехали в
            # `projects.settings` вместе с настоящими полями.
            ps = self._preset_settings(c, rel["preset_vsgen_id"])
            # `resolve()` всегда добавляет служебные `_scope`/`_links`/`_link_problems` —
            # словарь после него не пустой, даже если у пресета нет ни одного настоящего
            # поля. Проверять «прочитался ли пресет» нужно уже ПОСЛЕ снятия этих ключей,
            # иначе старый warn «настройки не прочитались» перестаёт срабатывать (Issue 2
            # ревью цикла 1) и проект молча уходит на умолчаниях кабинета без единой строки
            # в журнале.
            real = {k: v for k, v in ps.items() if not str(k).startswith("_")} if isinstance(ps, dict) else {}
            # Причину сломанной ссылки пишем в журнал ВСЕГДА, а не только когда у
            # пресета остались свои поля (Minor 2 ревью цикла 2): и при пустом
            # результате `resolve()` человек обязан узнать, что именно не сработало,
            # а не только то, что пресет «не прочитался».
            for prob in (ps.get("_link_problems") or []) if isinstance(ps, dict) else []:
                log_line(c, rel["release_id"], "ролик",
                         f"пресет {rel['preset_vsgen_id']}: ссылка «{prob.get('part')}» "
                         f"на частичный пресет {prob.get('preset_id')} не сработала — "
                         f"{prob.get('why')}; группа осталась на своих полях",
                         level="warn")
            if real:
                settings = real
            else:
                # Пресет удалён, нечитаем или в нём нет ни одного настоящего поля: проект
                # всё равно заводим, но говорим об этом в журнале выпуска — молчаливые
                # умолчания хуже жалобы.
                log_line(c, rel["release_id"], "ролик",
                         f"настройки пресета {rel['preset_vsgen_id']} не прочитались — "
                         "проект уйдёт на настройках по умолчанию", level="warn")
                settings = {}
            settings["_applied_preset"] = rel["preset_vsgen_id"]
            # Голос по языку канала — правило ТЗ §06 «язык → провайдер → идентификатор».
            # Разрешаем здесь, а не в воркере: язык знает выпуск, а воркер получает уже
            # готовый проект. Настройка, которую некому прочитать, — это то же, что её
            # отсутствие, только выглядит рабочей.
            chosen = preset_fields.voice_for(settings, rel["lang"] or "")
            if chosen:
                settings["voice"] = chosen
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
        # 04.09.2026: обложку заказываем ЗДЕСЬ, а не на своей фазе. Ей нужна только
        # донорская ссылка — ни текста, ни кадров, ни рендера (см. do_cover_ready:
        # create_job принимает donor_url). А фаза cover_ready идёт после media_ready,
        # и обложка ждала часы производства, чтобы потом делаться минуты. На 47 роликах
        # в сутки это час простоя на каждом ролике на ровном месте.
        #
        # Почему не в самом начале, вместе с рерайтом: текст — самый ненадёжный шаг,
        # он зависит от чужих субтитров. Обложка, заказанная до текста, у части
        # выпусков оплачивалась бы и выбрасывалась. Здесь текст уже есть, проект создан,
        # и выпуск почти наверняка дойдёт до конца.
        self._preorder_cover(c, rel)
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
                adopted = self._adopt_stage(c, rel, kind, jobs)
                if adopted:
                    self._remember_job(c, rel["release_id"], kind, adopted, rel["updated"])
                    log_line(c, rel["release_id"], "ролик",
                             f"этап «{kind}» уже стоит в очереди (задача {adopted}, "
                             f"поставлен сам после сборки) — беру его, второй не ставлю")
                    jobs = self._jobs(c, rel["release_id"])
                    known = jobs.get(kind)
            if known is None:
                uid = self._uid(c, rel["tenant_id"])
                if not uid:
                    raise ResourceDown("студия", "не найден владелец проекта для постановки")
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
                newer = self._newer_stage_task(c, rel, kind, known["remote_job_id"])
                if newer:
                    # Этап перезапустили руками (кнопкой «Повторить» в проекте) — новая
                    # задача и есть сегодняшний этап. Раньше выпуск падал по старой.
                    c.execute("UPDATE pub_release_jobs SET remote_job_id=?, status=?, "
                              "error=NULL, updated=? WHERE release_id=? AND service='vsgen' "
                              "AND kind=?", (newer[0], newer[1], rel["updated"],
                                             rel["release_id"], kind))
                    log_line(c, rel["release_id"], "ролик",
                             f"этап «{kind}» перезапущен (задача {newer[0]}, {newer[1]}) — "
                             f"беру её вместо упавшей {known['remote_job_id']}")
                    return {"wait": True}
                if kind in OPTIONAL_STAGES:
                    continue      # без глав ролик публикуется, просто без таймкодов
                if kind == "images" and self._requeue_after_prompt_channel(c, rel, known):
                    return {"wait": True}
                if (kind == "images" and state == "error"
                        and self._frames_dropped(c, known["remote_job_id"])
                        and self._requeue_dropped_frames(c, rel, known,
                                                         known["remote_job_id"])):
                    return {"wait": True}
                raise ItemFailed(f"этап «{kind}» завершился со статусом {state}")
            if state not in TERMINAL_OK:
                if kind in OPTIONAL_STAGES:
                    why = self._chapters_overdue(c, rel, known)
                    if why:
                        log_line(c, rel["release_id"], "ролик",
                                 f"таймкоды не дождались ({why}) — ролик уходит без них")
                        continue
                return {"wait": True}
            if kind == "images":
                later = self._latest_images_task(c, rel, known["remote_job_id"])
                if later and later[1] in ("queued", "running"):
                    # Картинки этого проекта ещё делаются (их поставил сам кабинет после
                    # промптов или человек кнопкой) — сборку не начинаем, ждём их.
                    mark = str(known["error"] or "")
                    c.execute("UPDATE pub_release_jobs SET remote_job_id=?, status=?, "
                              "error=?, updated=? WHERE release_id=? AND service='vsgen' "
                              "AND kind='images'",
                              (later[0], later[1],
                               mark if mark.startswith("images-retry:") else "re-enqueued",
                               rel["updated"], rel["release_id"]))
                    log_line(c, rel["release_id"], "ролик",
                             f"картинки уже стоят в очереди (задача {later[0]}) — жду их")
                    return {"wait": True}
                if later and later[1] == "error" and self._frames_dropped(c, later[0]):
                    # 04.10.2026: задача картинок, поставленная кабинетом, упала с
                    # «не хватает 216 кадров из 272», а движок смотрел на свою, давно
                    # сделанную задачу промптов и шёл к сборке — ролик собрался бы с
                    # дырами. Последний прогон картинок упал — значит, кадров не хватает.
                    if self._requeue_dropped_frames(c, rel, known, later[0]):
                        return {"wait": True}
                    raise ItemFailed(f"кадры выпали и после повторов: задача картинок "
                                     f"{later[0]} завершилась ошибкой")
            if kind == "images" and not self._images_exist(rel):
                # Кабинет на «images» мог сначала поставить задачу промптов — она done,
                # а картинок нет. Один раз ставим этап заново, второй провал — ошибка.
                if known["error"] == "re-enqueued":
                    raise ItemFailed("этап картинок завершился, а картинок в проекте нет")
                uid = self._uid(c, rel["tenant_id"])
                if not uid:
                    # 02.10.2026 здесь не было проверки, и десять задач картинок встали в
                    # очередь без владельца — их не берёт ни один шедулер.
                    raise ResourceDown("студия", "не найден владелец проекта для постановки")
                c.commit()      # то же: чужое соединение не должно ждать наш замок
                job_id = self.enqueue(uid, rel["tenant_id"], rel["project_id"], "images",
                                      self._preset_params(c, rel))
                if not job_id:
                    # Отказ очереди почти всегда значит «картинки этого проекта уже
                    # стоят» (их поставил сам сервер после промптов). Берём стоящую задачу
                    # и честно пишем, что ждём её, — а не «очередь не приняла» без конца.
                    newer = self._newer_stage_task(c, rel, "images", known["remote_job_id"])
                    if newer and newer[1] in ("queued", "running"):
                        c.execute("UPDATE pub_release_jobs SET remote_job_id=?, status=?, "
                                  "error='re-enqueued', updated=? WHERE release_id=? AND "
                                  "service='vsgen' AND kind='images'",
                                  (newer[0], newer[1], rel["updated"], rel["release_id"]))
                        log_line(c, rel["release_id"], "ролик",
                                 f"картинки уже стоят в очереди (задача {newer[0]}) — жду их")
                        return {"wait": True}
                    raise ResourceDown("images", "очередь не приняла повторный этап картинок")
                c.execute("UPDATE pub_release_jobs SET remote_job_id=?, status='queued', "
                          "error='re-enqueued', updated=? WHERE release_id=? "
                          "AND service='vsgen' AND kind='images'",
                          (str(job_id), rel["updated"], rel["release_id"]))
                log_line(c, rel["release_id"], "ролик",
                         "первая задача картинок оказалась промптами — ставлю сами картинки")
                return {"wait": True}
        return {}

    # Сколько готовый ролик ждёт таймкоды и за сколько до слота перестаёт ждать.
    # Решение владельца 04.10.2026 (тест решений, А4): «до 6 часов, но если до слота
    # меньше 3 часов — лить без таймкодов». В тот день восемь готовых роликов ждали
    # таймкоды с 09:25 до ночи за промптами новых проектов и опоздали к слотам; мост ВВ2
    # старого кабинета ждёт те же 6 часов (PUB_BRIDGE_CHAPTERS_WAIT_H).
    CHAPTERS_WAIT_SEC = float(os.environ.get("VS_CHAPTERS_WAIT_SEC") or 6 * 3600)
    CHAPTERS_SLOT_MARGIN_SEC = float(os.environ.get("VS_CHAPTERS_SLOT_MARGIN_SEC") or 3 * 3600)

    def _chapters_overdue(self, c, rel, known) -> str:
        """Почему таймкоды больше не ждём — или "" (ждём дальше).

        Ожидание считаем от постановки задачи таймкодов: от её строки в очереди, а если
        строки нет — от того, когда выпуск её запомнил.
        """
        now = self._clock()
        since = None
        try:
            row = c.execute("SELECT created FROM queue_tasks WHERE job_id=?",
                            (known["remote_job_id"],)).fetchone()
            since = float(row["created"]) if row and row["created"] else None
        except Exception:                                          # noqa: BLE001
            since = None
        if since is None:
            try:
                since = float(known["created"] or 0) or None
            except (KeyError, IndexError, TypeError, ValueError):
                since = None
        try:
            slot = float(rel["slot_at"] or 0) if "slot_at" in rel.keys() else 0.0
        except (TypeError, ValueError):
            slot = 0.0
        if slot and slot - now < self.CHAPTERS_SLOT_MARGIN_SEC:
            left = max(0, int((slot - now) / 60))
            return f"до слота {left} мин — меньше {int(self.CHAPTERS_SLOT_MARGIN_SEC // 3600)} ч"
        if since and now - since >= self.CHAPTERS_WAIT_SEC:
            return (f"ждали {int((now - since) // 3600)} ч из "
                    f"{int(self.CHAPTERS_WAIT_SEC // 3600)}")
        return ""

    def _newer_stage_task(self, c, rel, kind: str, after) -> tuple:
        """(job_id, status) самой новой живой или сделанной задачи этапа новее `after`."""
        try:
            after_id = int(after or 0)
        except (TypeError, ValueError):
            return ()
        if not rel["project_id"] or not after_id:
            return ()
        try:
            row = c.execute(
                "SELECT job_id, status FROM queue_tasks WHERE project_id=? AND kind=? AND "
                "job_id>? AND status IN ('queued','running','done') ORDER BY job_id DESC "
                "LIMIT 1", (rel["project_id"], kind, after_id)).fetchone()
        except Exception:                                          # noqa: BLE001
            return ()
        return (str(row["job_id"]), row["status"]) if row else ()

    # Сколько раз подряд картинки ставятся заново из-за отказа КАНАЛА промптов. Дальше —
    # разбор: значит, нет ни одного рабочего канала, и это уже не случайность.
    PROMPT_CHANNEL_RETRIES = 2

    def _requeue_after_prompt_channel(self, c, rel, known) -> bool:
        """Картинки упали, потому что отказал канал промптов, — поставить их заново.

        04.10.2026: 13 выпусков стояли в разборе с «этап images завершился со статусом
        error». Причина у всех одна: промпты ушли в канал OpenAI, где пустой счёт (с
        21.09). Канал запасной — задача попадает туда, когда Codex занят; пометка
        «канал мёртв» живёт 10 минут и только в памяти службы, так что после каждого
        перезапуска и раз в 10 минут очередная задача снова пробует пустой счёт.

        Сам отказ денег не стоит: ни промптов, ни картинок не сделано. Поэтому ставим
        картинки заново — канал промптов выбирается при постановке, и свежая пометка
        уводит его на рабочий. Не больше PROMPT_CHANNEL_RETRIES раз подряд.
        """
        if not rel["project_id"]:
            return False
        try:
            row = c.execute("SELECT error FROM queue_tasks WHERE project_id=? AND "
                            "kind='prompts' ORDER BY job_id DESC LIMIT 1",
                            (rel["project_id"],)).fetchone()
        except Exception:                                          # noqa: BLE001
            return False
        said = str((row["error"] if row else "") or "").lower()
        if not ("канал" in said and "промптов" in said
                and ("исчерпан" in said or "недоступен" in said)):
            return False
        mark = str(known["error"] or "")
        done = int(mark.split(":", 1)[1]) if mark.startswith("prompt-channel:") \
            and mark.split(":", 1)[1].isdigit() else 0
        if done >= self.PROMPT_CHANNEL_RETRIES:
            return False
        uid = self._uid(c, rel["tenant_id"])
        if not uid:
            return False
        c.commit()      # постановка задачи откроет своё соединение — замок отпустить
        job_id = self.enqueue(uid, rel["tenant_id"], rel["project_id"], "images",
                              self._preset_params(c, rel))
        if not job_id:
            return False
        c.execute("UPDATE pub_release_jobs SET remote_job_id=?, status='queued', error=?, "
                  "updated=? WHERE release_id=? AND service='vsgen' AND kind='images'",
                  (str(job_id), f"prompt-channel:{done + 1}", rel["updated"],
                   rel["release_id"]))
        log_line(c, rel["release_id"], "ролик",
                 f"канал промптов отказал (исчерпан счёт или лимит) — картинки поставлены "
                 f"заново, задача {job_id}: промпты пойдут другим каналом "
                 f"(повтор {done + 1} из {self.PROMPT_CHANNEL_RETRIES})", level="warn")
        return True

    # Сколько раз подряд картинки ставятся заново, если кадры выпали. Дальше — разбор:
    # два прогона подряд не добрали кадры, и это уже не случайность.
    IMAGES_RETRIES = 2

    def _latest_images_task(self, c, rel, after) -> tuple:
        """(job_id, status) самой новой задачи картинок проекта новее `after`. () — нет."""
        try:
            after_id = int(after or 0)
        except (TypeError, ValueError):
            after_id = 0
        if not rel["project_id"]:
            return ()
        try:
            row = c.execute(
                "SELECT job_id, status FROM queue_tasks WHERE project_id=? AND "
                "kind='images' AND job_id>? ORDER BY job_id DESC LIMIT 1",
                (rel["project_id"], after_id)).fetchone()
        except Exception:                                          # noqa: BLE001
            return ()
        return (str(row["job_id"]), row["status"]) if row else ()

    # Строки журнала задачи, по которым видно, что прогон упал из-за выпавших кадров, а
    # не из-за настроек (неверная модель, схема, отказ промптов — от повтора не пройдут).
    DROPPED_FRAME_MARKS = ("не хватает", "кадр пропущен")

    def _frames_dropped(self, c, job_id) -> bool:
        """Упала ли задача картинок из-за выпавших кадров (по её журналу)."""
        try:
            rows = c.execute("SELECT line FROM job_logs WHERE job_id=? ORDER BY id DESC "
                             "LIMIT 400", (int(job_id),)).fetchall()
        except Exception:                                          # noqa: BLE001
            return False
        for (line,) in rows:
            low = str(line or "").lower()
            if any(m in low for m in self.DROPPED_FRAME_MARKS) and "кадр" in low:
                return True
        return False

    def _requeue_dropped_frames(self, c, rel, known, failed_job) -> bool:
        """Задача картинок упала — поставить картинки заново, не больше IMAGES_RETRIES раз.

        Просьба владельца 04.10.2026: «автоматом запускать, чтобы он фотки переделывал,
        если какие-то дропались». Повтор дешёвый и безопасный по деньгам:
          • готовые кадры лежат в проекте и второй раз не рисуются (`--use_cache`);
          • заказ, который уже создавался, журнал платных вызовов второй раз оплатить
            не даст — его опросят, а не закажут заново;
          • заказ, который провайдер потерял (404), журнал как раз разрешает повторить.
        Рисуются только недостающие кадры. Счёт — в зеркале этапа (`images-retry:N`);
        «вернуть в работу» его не обнуляет, потому что ставит этап заново и так.
        """
        if not rel["project_id"]:
            return False
        mark = str(known["error"] or "")
        done = int(mark.split(":", 1)[1]) if mark.startswith("images-retry:") \
            and mark.split(":", 1)[1].isdigit() else 0
        if done >= self.IMAGES_RETRIES:
            return False
        uid = self._uid(c, rel["tenant_id"])
        if not uid:
            return False
        c.commit()      # постановка задачи откроет своё соединение — замок отпустить
        job_id = self.enqueue(uid, rel["tenant_id"], rel["project_id"], "images",
                              self._preset_params(c, rel))
        if not job_id:
            return False
        c.execute("UPDATE pub_release_jobs SET remote_job_id=?, status='queued', error=?, "
                  "updated=? WHERE release_id=? AND service='vsgen' AND kind='images'",
                  (str(job_id), f"images-retry:{done + 1}", rel["updated"],
                   rel["release_id"]))
        log_line(c, rel["release_id"], "ролик",
                 f"часть кадров выпала (задача картинок {failed_job} завершилась ошибкой) — "
                 f"картинки поставлены заново, задача {job_id}. Готовые кадры остаются, "
                 f"дорисуются только недостающие; уже оплаченные заказы журнал второй раз "
                 f"оплатить не даст (повтор {done + 1} из {self.IMAGES_RETRIES})",
                 level="warn")
        return True

    def _adopt_stage(self, c, rel, kind: str, jobs: dict) -> str:
        """Этап, который уже поставил кабинет сам, — номер его задачи. "" — такого нет.

        03.10.2026: у каждого выпуска главы делались дважды. Кабинет после сборки сам
        ставит главы (`auto_chapters` в main.py), а движок про эту задачу не знал и ставил
        свою: очередь отказывала, пока первая не кончится («ждём: очередь не приняла
        этап chapters» — до ста повторов), а потом принимала — и та же работа шла второй
        раз.

        Берём только задачу НОВЕЕ всех этапов, уже записанных за выпуском: старая задача
        того же вида — прошлый заход, и выдать её за сегодняшний этап нельзя. Без
        записанных этапов не берём ничего — первый этап ставит только движок.
        """
        floor = 0
        for row in (jobs or {}).values():
            try:
                floor = max(floor, int(row["remote_job_id"]))
            except (TypeError, ValueError, KeyError, IndexError):
                continue
        if not floor or not rel["project_id"]:
            return ""
        try:
            row = c.execute(
                "SELECT job_id FROM queue_tasks WHERE project_id=? AND kind=? AND job_id>? "
                "AND status NOT IN ('error','cancelled') ORDER BY job_id DESC LIMIT 1",
                (rel["project_id"], kind, floor)).fetchone()
        except Exception:                                          # noqa: BLE001
            return ""
        return str(row["job_id"]) if row else ""

    def _images_exist(self, rel) -> bool:
        import glob as _g
        proj = self.project_dir(rel["tenant_id"], rel["project_id"])
        for pat in ("googler/*.jpg", "googler/*.jpeg", "googler/*.png"):
            if _g.glob(os.path.join(proj, pat)):
                return True
        return False

    def _preorder_cover(self, c, rel) -> None:
        """Заказать обложку заранее, чтобы она делалась рядом с производством.

        Ошибка здесь выпуск не роняет: обложка — параллельная ветка, и её беда
        разбирается на своей фазе, где для этого есть попытки и карантин. Уронить
        ролик на этапе озвучки из-за недоступных обложек было бы подменой причины.
        """
        try:
            if self._remote(c, rel["release_id"], "cover") is not None:
                return                       # уже заказана
            if not (rel["preset_cover"] and rel["donor_url"]):
                return                       # нечем или не из чего — скажет своя фаза
            key = get_key(c, "cover")
            if not key:
                return
            job_id = CoverClient(key).create_job(rel["preset_cover"], rel["lang"] or "ru",
                                                 rel["donor_url"])
            self._remember_remote(c, rel["release_id"], "cover", job_id, rel["updated"])
            log_line(c, rel["release_id"], "обложка",
                     f"заказана заранее, рядом с производством — задача {job_id}")
        except Exception as exc:                                   # noqa: BLE001
            log_line(c, rel["release_id"], "обложка",
                     f"заранее заказать не вышло ({type(exc).__name__}) — "
                     f"вернёмся к ней на своей фазе", level="warn")

    def do_cover_ready(self, c, rel):
        """Обложка по заголовку ролика. Деградировавшую (текст оверлеем) не принимаем.

        К этой фазе обложка обычно уже заказана и часто готова: заказ уходит вместе
        с производством (см. `_preorder_cover`). Здесь остаётся проверить результат.
        """
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

        # Обложку принял человек (экран выпуска, «Взять эту обложку») — она уже лежит в
        # проекте, и чужой вердикт сверки её не отменяет: без этого возврат в работу
        # снова спросил бы ту же задачу, увидел «брак» и перезаказал за деньги.
        cover_file = os.path.join(self.project_dir(rel["tenant_id"], rel["project_id"]),
                                  "cover.jpg")
        if known["status"] == "done" and os.path.exists(cover_file):
            return {"payload": {"cover_bytes": os.path.getsize(cover_file)}}

        info = client.job(known["remote_job_id"])
        state = str(info.get("status") or "").lower()
        if state in ("error", "failed", "cancelled"):
            reason = "обложка не сделалась: " + str(info.get("error") or state)
            self._finish_remote(c, rel["release_id"], "cover", "error", reason)
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
        if cover_unchecked(info):
            log_line(c, rel["release_id"], "обложка",
                     "надпись на обложке не сверена: проверка через подписку Codex не "
                     "дождалась ответа. Картинка нарисована и принята — перерисовка за "
                     "деньги тут ничего не исправит. Посмотрите обложку глазами",
                     level="warn")
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

    def _make_title(self, c, rel) -> str:
        """Название ролика. По умолчанию — подписка Codex (сервис текста, ключ рерайта).

        VS_TITLE_PROVIDER=openai возвращает прежний путь через ключ OpenAI — на случай,
        если счёт пополнят и захотят вернуть; сейчас он пуст и отвечает 429.
        """
        if (os.environ.get("VS_TITLE_PROVIDER") or "codex").strip().lower() == "openai":
            return make_title(rel["donor_title"] or "", rel["lang"] or "ru",
                              self._openai_key(c))
        rid = rel.get("release_id") if hasattr(rel, "get") else rel["release_id"]
        with self._titles_lock:
            job = self._titles.get(rid)
            if job is not None and not job["thread"].is_alive():
                return self._title_result(rid, job)
            if job is not None:
                raise TitleUnavailable("название пишется подпиской Codex — ждём ответа")
            busy = sum(1 for j in self._titles.values() if j["thread"].is_alive())
            if busy >= self.TITLE_WORKERS:
                raise TitleUnavailable(f"названия пишутся по очереди: сейчас в работе {busy} — "
                                       "этот выпуск следующий")
            import threading as _threading
            # Ключ читаем здесь: соединение с базой принадлежит кругу движка, в фон его
            # отдавать нельзя.
            args = (rel["donor_title"] or "", rel["lang"] or "ru", get_key(c, "rewrite"),
                    REWRITE_BASE)
            job = {"title": "", "error": "", "fatal": False}

            def work(job=job, args=args):
                try:
                    job["title"] = make_title_codex(*args)
                except TitleError as exc:
                    job["error"], job["fatal"] = str(exc), True
                except TitleUnavailable as exc:
                    job["error"] = str(exc)
                except Exception as exc:                       # noqa: BLE001
                    job["error"] = f"название не получено: {type(exc).__name__}"
            job["thread"] = _threading.Thread(target=work, daemon=True,
                                              name=f"title-{rid}")
            self._titles[rid] = job
            job["thread"].start()
        job["thread"].join(self.TITLE_GRACE_SEC)
        with self._titles_lock:
            if not job["thread"].is_alive() and self._titles.get(rid) is job:
                return self._title_result(rid, job)
        raise TitleUnavailable("заказал название подписке Codex — ждём ответа")

    def _title_result(self, rid, job) -> str:
        """Готовый фоновый заказ названия: забрать и убрать. Зовётся под _titles_lock."""
        self._titles.pop(rid, None)
        if job["title"]:
            return job["title"]
        if job["fatal"]:
            raise TitleError(job["error"])
        raise TitleUnavailable(job["error"] or "название не получено")

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

        ch = c.execute("SELECT * FROM pub_channels_live WHERE id=?", (rel["channel_id"],)).fetchone()
        if not ch:
            return ""
        try:
            schedule = _json.loads(ch["schedule"] or "{}")
            if not schedule:
                return ""
            now = dt.datetime.fromtimestamp(self._clock(), dt.timezone.utc)
            slots = upcoming_slots(schedule, ch["tz"], ready_slot_horizon(ch), now)
        except (ScheduleError, ValueError):
            return ""
        busy = {round(r["slot_at"]) for r in c.execute(
            "SELECT slot_at FROM pub_plan_items WHERE channel_id=? AND slot_at IS NOT NULL "
            "AND status IN ('planned','frozen','running','done','error')",
            (rel["channel_id"],)).fetchall() if r["slot_at"]}
        for point in slots:
            stamp = round(point.timestamp())
            if stamp in busy or point.timestamp() <= self._clock() + UPLOAD_LEAD_SEC:
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
        Проверка здесь — быстрая, а настоящая охрана от второго задания стоит в базе
        (`release_id` в `pub_upload_jobs` уникален). Обе нужны: между этой проверкой и
        вставкой помещается второй тик или ручной запуск прохода, и тогда единственное,
        что удержит выпуск от второй работы, — уникальность.
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
        # Запас — как у переноса в очереди (UPLOAD_LEAD_SEC): раньше здесь стояли 10 мин,
        # а ворота выдачи отказывают уже за 30, и задание с выходом через полчаса
        # погибало у ворот, не дождавшись переноса (04.10.2026).
        if deferred and rel["slot_at"] <= self._clock() + UPLOAD_LEAD_SEC:
            moved = self._reslot(c, rel)
            if not moved:
                raise ItemFailed(f"к времени выхода не успели, и свободных выходов на "
                                 f"{RESLOT_MAX_DAYS} дней вперёд нет — добавьте времена выхода")
            log_line(c, rel["release_id"], "движок",
                     f"к прежнему времени не успели — перенёс выход на {moved}")
            return {"wait": True}
        if deferred and not self._deferred_room(c, rel):
            raise ResourceDown("отложка",
                               f"в отложке канала уже {rel['deferred_limit']} роликов — ждём места")

        proj = self.project_dir(rel["tenant_id"], rel["project_id"])
        render_job = (self._jobs(c, rel["release_id"]).get("render") or {})
        job_id = render_job["remote_job_id"] if render_job else None
        # Путь, записанный самим рендером при приёме результата: единственный источник,
        # который знает файл ТОЧНО. Всё остальное в find_video — запасные варианты.
        out_rel = ""
        if job_id:
            row = c.execute("SELECT output_rel FROM queue_tasks WHERE job_id=? "
                            "AND kind='render'", (job_id,)).fetchone()
            out_rel = (row["output_rel"] if row and row["output_rel"] else "")
        try:
            video = find_video(proj, job_id, out_rel)
        except VideoAmbiguous as exc:
            raise ItemFailed(str(exc))
        if not video:
            raise ItemFailed("готового mp4 в проекте нет")
        cover = os.path.join(proj, "cover.jpg")
        if not os.path.exists(cover):
            cover = ""

        try:
            title = self._make_title(c, rel)
            chapters = ""
            if chapters_enabled(self._preset_settings(c, rel["preset_vsgen_id"])):
                chapters = read_chapters(proj)
            description = make_description(title, chapters)
            schedule = schedule_iso(rel["slot_at"], rel["tz"]) if deferred else ""
        except TitleUnavailable as exc:
            # Генератор названий временно недоступен (ключ/лимит/сеть) — ролик ЖДЁТ,
            # а не умирает: один 429 не повод хоронить готовый выпуск.
            raise ResourceDown("название", str(exc))
        except TitleError as exc:
            raise ItemFailed(str(exc))

        # Допуск привязывается к КОНКРЕТНОЙ версии ролика: запоминаем, чем был файл в
        # эту секунду (какой рендер его дал и каков он на диске). Перед выдачей задания
        # и перед отдачей байтов это сверяется заново — см. `pub_artifact` и ворота в
        # `pub_upload_api`. Без такой записи в задании оставался только путь, а путь у
        # рендера постоянный: пересобранный проект молча подменял принятый ролик.
        mark = pub_artifact.passport(c, rel["release_id"], video)
        now = self._clock()
        try:
            c.execute(
                "INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,"
                "host_id,channel_url,title,description,schedule_iso,visibility,collaborator,"
                "video_path,video_sha256,video_passport,cover_path,slot_at,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rel["release_id"], rel["tenant_id"], rel["channel_id"], rel["uc"],
                 rel["profile_id"], rel["host_id"],
                 f'https://studio.youtube.com/channel/{rel["uc"]}/',
                 title, description, schedule, "public", rel["collaborator"] or "",
                 video, mark["build"]["sha256"], pub_artifact.dump(mark),
                 cover, rel["slot_at"], now, now))
        except sqlite3.IntegrityError:
            # Задание на этот выпуск уже завёл параллельный проход. Уникальность
            # `release_id` сделала ровно то, ради чего стоит, — второй работы не будет.
            # Для выпуска это не отказ: допуск состоялся, просто не в этом проходе.
            # Падать здесь нельзя: движок ловит любое исключение обработчика и уводит
            # выпуск в разбор, то есть готовый ролик остановился бы из-за гонки.
            return {}
        log_line(c, rel["release_id"], "заливка",
                 f"комплект готов: «{title}», выход {schedule or 'сразу'}, "
                 f"обложка {'есть' if cover else 'нет'}, профиль {rel['profile_id']}")
        log_line(c, rel["release_id"], "заливка",
                 f"допущена версия: рендер №{mark['build']['job'] or 'неизвестен'}, "
                 f"файл {os.path.basename(video)} "
                 f"{pub_artifact.human_size(mark['file'].get('size'))}")
        return {"payload": {"title": title, "schedule": schedule}}

    def do_upload_running(self, c, rel):
        """Ждём, пока машина реально начнёт заливать."""
        job = self._upload_job(c, rel["release_id"])
        if job is None:
            raise ItemFailed("задание на заливку исчезло")
        if job["status"] == "error":
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
            raise ItemFailed(job["error"] or "заливка не удалась")
        return {"wait": True}

    def do_published(self, c, rel):
        """Проверка фактического выхода — отдельным проходом, позже.

        До неё выпуск уже закрыт по подтверждённому расписанию, поэтому сюда движок
        обычно не заходит вовсе; основной проход живёт в `pub_verify.run()`.

        Доказательство — ответ YouTube Data API, а не разбор страницы: браузер
        результат не доказывает. Нет ключа или нет ответа — ждём, а не выдумываем:
        отсутствие ответа не является ответом.
        """
        import pub_verify
        key = pub_verify.api_key(c)
        if not key:
            raise ResourceDown("проверка публикации",
                               "нет ключа YouTube Data API "
                               "(вставить на экране «Ключи»)")
        res = pub_verify.check(rel["video_id"] or "", key)
        if res["state"] == pub_verify.PUBLIC:
            return {"video_id": rel["video_id"] or "",
                    "evidence": {"public_check": res}}
        if res["state"] == pub_verify.UNLISTED:
            raise ItemFailed("ролик доступен только по ссылке, а должен быть публичным")
        raise ResourceDown("проверка публикации",
                           res.get("error") or "выход пока не подтверждён")

    def _openai_key(self, c) -> str:
        """Ключ для рерайта названия. Нет ключа — ждём настройки, а не портим название."""
        try:
            return self.openai_key() or ""
        except Exception:
            return ""

    def _preset_settings(self, c, preset_id) -> dict:
        """Настройки пресета VideoStudio словарём, с разрешёнными ссылками частичных
        пресетов (`preset_links.resolve`) — включая служебные `_scope`/`_links`/
        `_link_problems`, которые вызывающая сторона обязана снять перед тем, как
        класть результат в проект или в задачу (правило 2 архитектуры: одна функция
        разрешает пресет, но убрать метаданные — дело каждого потребителя отдельно,
        как это уже делает `_preset_project_payload` в кабинете).

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
        if not row:
            return {}
        raw = row["settings"] or ""
        if not isinstance(raw, dict):
            try:
                value = json.loads(raw) if str(raw).strip() else {}
            except Exception:
                return {}
            if not isinstance(value, dict):
                return {}
        try:
            return preset_links.resolve(c, preset_id)
        except Exception:
            return {}

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


# ════════════ проход «забрать уже оплаченное» (19.09.2026) ════════════
#
# Зачем он есть. Движок выпусков стоит на паузе с 31.08, а рерайт всё это время
# доделывал заказанное: в кабинете владельца 146 задач со статусом completed —
# текст написан, ОПЛАЧЕН и лежит у соседа, но никем не забран. Ещё 187 выпусков
# ждут в очереди. Снять паузу ради этих текстов нельзя: пауза держит НОВЫЕ траты,
# и вместе с забором готового поехали бы новые заказы озвучки, картинок и рендера.
#
# Поэтому забор сделан отдельным проходом, который невозможно перепутать с работой
# движка:
#   * он никогда не зовёт `create_job` и вообще ничего не заказывает;
#   * он ходит ТОЛЬКО по уже записанному номеру задачи — нашему (`pub_release_jobs`)
#     или записанному в журнале оплат (`provider_calls.remote_order_id`);
#   * он не снимает паузу и не обходит её: тратить ему нечего, спрашивать не о чем;
#   * пропавшая у соседа задача (404/410) НЕ превращается в новый заказ — строка
#     помечается `missing` и ждёт человека, ровно как в штатном пути;
#   * текст принимается той же приёмкой по доле донора, что и обычно.
#
# Правило проекта «повтор чтения безопасен, повтор оплачиваемого создания — нет»
# здесь выполняется буквально: проход состоит из одних чтений.

# Что у соседа значит «готово», а что «не справился». Слова у разных версий рерайта
# разные (`done` у нашего форка, `completed` у боевого 18.09), поэтому списком.
REWRITE_DONE = ("done", "completed", "finished", "success")
REWRITE_BAD = ("error", "failed", "cancelled")

# Строки о задаче, которые проход не трогает: `done` — текст уже забран,
# `missing` — сосед уже сказал «такой задачи нет», и до разбора человеком повторный
# стук ничего не изменит, а строку он бы перетирал.
COLLECT_SKIP_JOB = ("done", "missing")

# Сколько задач опрашивать за один проход по умолчанию. Каждая задача — это запрос
# статуса и скачивание файла, то есть до TIMEOUT секунд на неотвечающей; 146 штук
# подряд держали бы запрос пульта минутами, и экран выглядел бы зависшим. Ответ
# честно говорит, сколько осталось, — кнопку жмут ещё раз.
# `limit=0` в аргументе снимает ограничение: так проход зовут из консоли, когда
# ждать некому.
COLLECT_BATCH = int(os.environ.get("VS_COLLECT_BATCH", "25"))

# Что случилось с каждым выпуском — кодом для машины и словами для экрана.
COLLECT_REASONS = {
    "taken": "текст забран, фаза двинута",
    "waiting": "текст ещё не готов",
    "missing": "задача текста пропала — нужен разбор, повтор оплачивается заново",
    "failed": "заказ текста вернулся отказом",
    "rejected": "текст пришёл, но не прошёл приёмку",
    "ask_failed": "статус задачи спросить не удалось",
    "cancelled": "выпуск отменён, пока шёл проход",
}


def _text_only_services() -> PubServices:
    """Обработчик стадий для прохода, которому нечего заказывать.

    Проходу нужны только четыре вещи из `PubServices`: приёмка текста по длине,
    сохранение текста, закрытие строки журнала оплат и отметка исхода задачи. Ни
    создание проекта, ни очередь кабинета ему не нужны — он забирает готовый файл.
    Заглушки стоят намеренно и падают громко: если через них кто-то позовёт стадию,
    это ошибка вызова, а не тихий пропуск оплаченной работы.
    """
    def _refuse(*_a, **_kw):
        raise RuntimeError("проход «забрать оплаченное» ничего не заказывает "
                           "и не создаёт проектов")

    return PubServices({"create_project": _refuse, "enqueue": _refuse,
                        "project_dir": _refuse})


def _dicts(cur) -> list:
    """Строки запроса словарями независимо от `row_factory` соединения.

    Кабинет всегда ставит `sqlite3.Row`, но проход зовут и из пульта, и из консоли,
    и с чужим соединением: `dict(row)` на голом кортеже падает, а разбирать это в
    чужом процессе дорого. Имена берём из описания курсора — они есть всегда.
    """
    names = [d[0] for d in (cur.description or [])]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def paid_text_candidates(c, tenant_id: str = "", release_ids=None,
                         include_blocked: bool = False) -> list:
    """Выпуски, за текст которых уже заплачено, а текста у нас нет.

    Два источника номера задачи, и оба — уже записанные, ни одного нового:

      * `pub_release_jobs` (service='rewrite', kind='job') — наша копия «что заказано
        соседу». Обычный случай тех самых 146;
      * `provider_calls.remote_order_id` со статусом `ordered` — журнал оплат. Сюда
        попадают выпуски, у которых наша строка о задаче потерялась (ручной сброс,
        переезд базы): деньги записаны, номер известен, спрашивать по нему безопасно.
        Штатный путь их тоже подхватывает, но только после повторной загрузки
        субтитров донора — ради того же номера это лишняя работа и лишний риск.

    Берём только фазу `created`: текст забирается ровно на переходе «заведён → текст
    готов», дальше его уже забрали. Выпуски в разборе (`blocked`) по умолчанию не
    трогаем — их остановил человек или движок, и возвращать их в работу это решение
    человека (`pub_engine.revive`), а не побочный эффект забора текста.
    """
    live = ["pending", "running"] + (["blocked"] if include_blocked else [])
    where = ["r.phase='created'", "r.status IN (%s)" % ",".join("?" * len(live))]
    args = list(live)
    if tenant_id:
        where.append("r.tenant_id=?")
        args.append(str(tenant_id))
    ids = [str(x) for x in (release_ids or []) if str(x).strip()]
    if ids:
        where.append("r.release_id IN (%s)" % ",".join("?" * len(ids)))
        args += ids
    # Текст уже лежит у нас — забирать нечего, чем бы ни кончилась строка задачи.
    no_text = ("NOT EXISTS(SELECT 1 FROM pub_release_jobs t WHERE t.release_id=r.release_id"
               " AND t.service='rewrite' AND t.kind='text')")
    head = ("SELECT r.release_id, r.tenant_id, r.project_id, r.status rel_status, "
            "r.slot_at, ch.name ch_name, ch.lang lang, p.donor_title donor_title, ")
    common = ("FROM pub_releases r JOIN pub_channels_live ch ON ch.id=r.channel_id "
              "JOIN pub_plan_items p ON p.id=r.plan_item_id ")

    out, seen = [], set()
    own = _dicts(c.execute(
        head + "j.remote_job_id job_id, j.payload payload, j.status job_status, "
        "j.updated ordered_at, 'job' src " + common +
        "JOIN pub_release_jobs j ON j.release_id=r.release_id AND j.service='rewrite' "
        "AND j.kind='job' WHERE " + " AND ".join(
            where + ["IFNULL(j.remote_job_id,'')<>''",
                     "j.status NOT IN (%s)" % ",".join("?" * len(COLLECT_SKIP_JOB)),
                     no_text]) + " ORDER BY r.slot_at",
        args + list(COLLECT_SKIP_JOB)))
    for row in own:
        out.append(row)
        seen.add(row["release_id"])

    try:
        paid = _dicts(c.execute(
            head + "pc.remote_order_id job_id, NULL payload, '' job_status, "
            "pc.updated ordered_at, 'calls' src " + common +
            "JOIN provider_calls pc ON pc.release_id=r.release_id AND pc.kind='text' "
            # Язык и есть единица этой строки журнала — та же пара (выпуск, язык),
            # по которой штатный путь закрывает оплату (`_close_text_call`).
            "AND pc.unit=COALESCE(NULLIF(ch.lang,''),'ru') AND pc.status='ordered' "
            "WHERE " + " AND ".join(
                where + ["IFNULL(pc.remote_order_id,'')<>''", no_text,
                         "NOT EXISTS(SELECT 1 FROM pub_release_jobs j "
                         "WHERE j.release_id=r.release_id AND j.service='rewrite' "
                         "AND j.kind='job')"]) + " ORDER BY r.slot_at, pc.id DESC",
            args))
    except sqlite3.OperationalError:
        # База старше журнала оплат. Это не повод ронять проход: свой источник
        # номеров у него уже есть.
        paid = []
    for row in paid:
        if row["release_id"] in seen:
            continue                 # одна задача на выпуск, дубля быть не должно
        out.append(row)
        seen.add(row["release_id"])
    return out


def paid_texts_waiting(c, tenant_id: str = "", include_blocked: bool = False,
                       now: float = None) -> list:
    """То же самое для экрана «оплачено, но не забрано»: без единого сетевого запроса.

    Экран обязан открываться, даже когда сосед лежит: он отвечает на вопрос «за что
    уже заплачено», а не «что сосед думает об этом сейчас».
    """
    moment = time.time() if now is None else now
    rows = paid_text_candidates(c, tenant_id=tenant_id, include_blocked=include_blocked)
    for row in rows:
        row["waiting_sec"] = max(0.0, moment - float(row.get("ordered_at") or moment))
    return rows


def _take_paid_text(c, svc, client, item: dict, now: float) -> tuple:
    """Забрать текст одной уже оплаченной задачи. Возвращает (код, словами, знаков).

    Ничего не заказывает: единственное обращение к соседу — статус задачи и файл
    результата по её номеру.
    """
    rid = item["release_id"]
    job_id = str(item["job_id"])
    # Обработчикам нужен «выпуск» с четырьмя полями: по ним пишется журнал, ищется
    # строка оплаты и принимается текст. Время ставим текущее — оно уходит в отметки
    # created/updated сохранённого текста.
    rel = {"release_id": rid, "tenant_id": item["tenant_id"],
           "project_id": item["project_id"], "lang": item["lang"] or "ru",
           "updated": now}

    if item.get("src") == "calls":
        # Номер задачи взят из журнала оплат: восстановим свою строку, иначе исход
        # (готово/пропала) записать некуда и движок после паузы спросит заново.
        svc._remember_remote(c, rid, "rewrite", job_id, now)
        log_line(c, rid, "рерайт",
                 f"задача {job_id} числится оплаченной в журнале — подхватываю её, "
                 f"нового заказа не создаю", level="warn", now=now)

    try:
        info = client.job(job_id)
    except RemoteJobMissing as exc:
        # Ровно то же, что делает штатный путь: пометить пропажу и остановиться.
        # Заказать заново значит заплатить второй раз за тот же текст.
        svc._close_text_call(c, rel, error=str(exc))
        svc._finish_remote(c, rid, "rewrite", "missing", str(exc))
        log_line(c, rid, "рерайт", str(exc), level="error", now=now)
        c.commit()
        return ("missing", str(exc)[:200], 0)
    except ResourceDown:
        raise                      # сосед лёг — это дело всего прохода, не строки
    except Exception as exc:       # noqa: BLE001
        # Спросить не вышло, а судьба задачи неизвестна. Ничего не помечаем: запись
        # «не справился» по неудавшемуся вопросу стоила бы оплаченного текста.
        return ("ask_failed", str(exc)[:200], 0)

    state = str(info.get("status") or "").lower()
    if state in REWRITE_BAD:
        reason = "рерайт не справился: " + str(info.get("error") or state)
        svc._close_text_call(c, rel, error=reason)
        svc._finish_remote(c, rid, "rewrite", "error", reason)
        log_line(c, rid, "рерайт", reason, level="error", now=now)
        c.commit()
        return ("failed", reason[:200], 0)
    if state not in REWRITE_DONE:
        return ("waiting", f"текст: «{state or 'без статуса'}»", 0)

    donor_chars = PubServices._donor_chars(item)
    try:
        code = client.result_code(info, rel["lang"])
        text = client.result_text(job_id, code)
        svc._check_text_length(c, rel, text, donor_chars)
        svc.store_source_text(c, rid, text, now)
    except ResourceDown:
        raise
    except Exception as exc:       # noqa: BLE001
        svc._close_text_call(c, rel, error=str(exc))
        svc._finish_remote(c, rid, "rewrite", "error", str(exc))
        c.commit()
        return ("rejected", str(exc)[:200], 0)

    svc._close_text_call(c, rel, result=job_id)
    svc._finish_remote(c, rid, "rewrite", "done")
    body = len(text.strip())
    share = (f", {body / donor_chars * 100:.1f}% донора" if donor_chars > 0 else "")

    # Двигаем фазу той же функцией, что и движок: охрана по статусу должна быть одна
    # на всех. Владелец мог отменить выпуск, пока мы ходили к соседу, — воскрешать
    # его отсюда нельзя.
    moved = set_release_fields(
        c, rid, now, phase="source_ready", status="running", heartbeat=now,
        blocked_reason=None,
        deadline=now + PHASE_DEADLINES.get("source_ready", DEFAULT_DEADLINE))
    if not moved:
        # Текст всё равно сохранён: он оплачен, и терять его из-за отмены незачем.
        # Фаза неживого выпуска — не наше дело.
        log_line(c, rid, "рерайт",
                 f"текст забран ({body} символов{share}), но выпуск уже не в работе — "
                 f"фазу не двигаю", level="warn", now=now)
        c.commit()
        return ("cancelled", "выпуск отменён, пока шёл проход — текст сохранён", body)

    log_line(c, rid, "рерайт",
             f"текст забран проходом «оплачено, но не забрано»: {body} символов{share}, "
             f"задача {job_id} заказана раньше, нового заказа не создавал", now=now)
    log_phase(c, rid, "source_ready", now, payload={"chars": body})
    log_line(c, rid, "движок", "шаг «source_ready» готов", now=now)
    c.commit()
    return ("taken", f"{body} символов{share}", body)


def collect_paid_texts(c, tenant_id: str = "", release_ids=None,
                       limit: int = COLLECT_BATCH, now: float = None,
                       include_blocked: bool = False, services=None) -> dict:
    """Забрать у рерайта тексты, за которые уже заплачено. Ничего не заказывает.

    Соединение — кабинета, как его отдаёт `main.db()` (с `row_factory=sqlite3.Row`):
    исход записывается в журнал оплат, а он читается по именам колонок. Чтение
    кандидатов (`paid_text_candidates`, `paid_texts_waiting`) работает с любым
    соединением — экран не должен зависеть от того, кто его открыл.

    Аргументы:
      tenant_id       ограничить кабинетом (пусто — все);
      release_ids     ограничить списком выпусков (пусто — все подходящие);
      limit           сколько задач опросить за проход; 0 или меньше — без предела;
      now             момент времени (для тестов);
      include_blocked брать и выпуски в разборе (по умолчанию нет, см.
                      `paid_text_candidates`);
      services        готовый `PubServices`, если он уже собран вызывающим.

    Ответ:
      {"candidates": сколько выпусков подходит под проход,
       "checked":    у скольких спросили статус,
       "taken":      сколько текстов забрано и фаз двинуто,
       "skipped":    сколько опрошенных остались без текста,
       "left":       сколько подходящих не опрошено (предел или остановка),
       "stopped":    "" или причина, по которой проход прекратился,
       "by_reason":  {код: сколько раз} — коды из COLLECT_REASONS,
       "items":      [{"release_id","channel","job_id","lang","result","note","chars"}]}
    """
    moment = time.time() if now is None else now
    out = {"candidates": 0, "checked": 0, "taken": 0, "skipped": 0, "left": 0,
           "stopped": "", "by_reason": {}, "items": []}

    key = get_key(c, "rewrite")
    if not key:
        # Не отказ строки, а ненастроенный кабинет: сказать словами и не делать вид,
        # что проход отработал.
        out["stopped"] = "не сохранён ключ доступа к рерайту"
        return out
    client = RewriteClient(key)
    svc = services or _text_only_services()

    rows = paid_text_candidates(c, tenant_id=tenant_id, release_ids=release_ids,
                                include_blocked=include_blocked)
    out["candidates"] = len(rows)
    todo = rows if limit is None or limit <= 0 else rows[:limit]
    out["left"] = len(rows) - len(todo)

    for item in todo:
        try:
            reason, note, chars = _take_paid_text(c, svc, client, item, moment)
        except ResourceDown as exc:
            # Сосед лёг, ключ не принят, упёрлись в его лимит запросов. Стучаться
            # оставшимися сотнями бессмысленно и вредно: ждёт весь проход целиком.
            out["stopped"] = str(exc)
            out["left"] = len(rows) - out["checked"]
            break
        except Exception as exc:                                   # noqa: BLE001
            # Одна строка не имеет права уронить проход: остальные тексты оплачены
            # так же, как эта.
            reason, note, chars = "ask_failed", str(exc)[:200], 0
        out["checked"] += 1
        out["by_reason"][reason] = out["by_reason"].get(reason, 0) + 1
        if reason == "taken":
            out["taken"] += 1
        else:
            out["skipped"] += 1
        out["items"].append({"release_id": item["release_id"],
                             "channel": item["ch_name"] or "",
                             "job_id": str(item["job_id"]), "lang": item["lang"] or "ru",
                             "result": reason, "note": note, "chars": chars})
    # Ветки «ещё не готово» ничего не пишут, но подхваченная из журнала оплат строка
    # о задаче — пишет, и потерять её из-за незакрытой записи было бы обидно: на
    # следующем проходе выпуск снова искали бы длинным путём.
    c.commit()
    return out
