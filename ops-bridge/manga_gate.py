# -*- coding: utf-8 -*-
"""Ворота приёмки моста манхвы: кого вообще допускать в очередь заливки.

Зачем. До 01.10.2026 мост брал всё, под что подходил glob `projects/*/out/video.mp4`
и у чего читался паспорт проекта (`manga_bridge.ready_projects`). Ни одной проверки
приёмки: ролик, не прошедший сборку, ролик без названия, канал, опознанный по похожему
имени, — всё это уезжало в очередь одинаково. Единственным, что держало брак, был
отказ 402 от прокси названий: счёт пуст, `make_title` падает, задание не создаётся.
Это не предохранитель, а случайная пробка. Пополнение счёта выпустило бы на каналы
всё накопленное за месяц, по одному ролику в день на канал, без возможности отозвать.

Поэтому здесь два разных предмета:

* **допуск** (`admit`) — проверки, которые обязаны пройти ДО первого платного вызова.
  Порядок тут важнее самих проверок: проверка после вызова модели деньги уже не
  спасает;
* **паспорт принятой версии** (`passport`) — чем именно был ролик в момент допуска.
  Между допуском и отправкой проходят часы: задание лежит в очереди, пока за ним не
  придёт машина со свободным профилем AdsPower. Всё это время в задании записан
  только ПУТЬ к файлу, а путь у манхвы постоянный — `out/video.mp4`. Пересборка
  проекта подменяет файл, не меняя в задании ни строчки, и на канал уходит версия,
  которую никто не принимал. Отменить это нельзя: ролик уже в YouTube.

Идея паспорта взята у кабинета (`studio-platform/server/pub_artifact.py`), но не код:
у кабинета версию сборки подтверждает строка его очереди рендера, а у манхвы своя
служба и своя база, в которую мост не ходит. Версию сборки здесь подтверждает то, что
манхва оставляет на диске сама, — машинная приёмка `out/acceptance.json`.

Полный SHA многогигабайтного mp4 на каждом прогоне не считается: прогон идёт по cron
раз в два часа и видит десятки роликов, а сервер в это время рендерит. Отпечаток —
размер плюс по мегабайту с начала и с конца: в этих краях лежит moov, индекс кадров,
и он меняется от любой пересборки. Чего такой отпечаток не увидит: правку в середине
файла, не изменившую ни длины, ни краёв. От пересборки такого не бывает — она
переписывает файл целиком, — а стоило бы это чтения всех гигабайт на каждом прогоне.
Осознанный размен, не недосмотр.

Время записи файла в паспорт пишется, но в сверке НЕ участвует: восстановление из
резервной копии и обычный `cp` переставляют mtime, не трогая байты. Отказ здесь
дорогой — готовый ролик встанет и будет ждать человека, — а ложная тревога ничем не
отличается от настоящей. Время храним, чтобы человеку было что прочитать.

Пустое значение всюду ниже означает «не знаем» и никогда — «ноль» и не «прошло».
"""
import hashlib
import json
import os
import tempfile

# Сколько байт с начала и с конца файла берём в отпечаток.
EDGE_BYTES = 1 << 20


class GateError(ValueError):
    """Настройки или паспорта моста не читаются; текст — для владельца, по-русски."""


def _text(value) -> str:
    return str(value or "").strip()


def _norm(value) -> str:
    """Хендл к сравнимому виду: без @, без регистра. UC сравнивается как есть."""
    return _text(value).lstrip("@").lower()


def human_size(size) -> str:
    """Размер для человека. Не знаем — так и говорим, а не показываем ноль."""
    if size is None:
        return "неизвестно"
    size = int(size)
    if size >= 1024 ** 3:
        return ("%.1f ГБ" % (size / 1024 ** 3)).replace(".", ",")
    if size >= 1024 ** 2:
        return "%d МБ" % (size // 1024 ** 2)
    return "%d Б" % size


def file_mark(path: str) -> dict:
    """Отпечаток файла: размер, края содержимого, время записи. Нет файла — пусто.

    Края читаются двумя кусками по мегабайту; у файла короче двух мегабайт второй
    кусок перекрывается с первым, и это нормально — хэш от файла всё равно один.
    """
    if not path:
        return {}
    try:
        size = os.path.getsize(path)
        digest = hashlib.sha256(str(size).encode("ascii"))
        with open(path, "rb") as f:
            digest.update(f.read(EDGE_BYTES))
            if size > EDGE_BYTES:
                f.seek(size - EDGE_BYTES)
                digest.update(f.read(EDGE_BYTES))
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    return {"size": size, "edges": digest.hexdigest(), "mtime": mtime}


def build_mark(folder: str) -> dict:
    """Чем кончилась машинная приёмка сборки манхвы (out/acceptance.json).

    Пусто — «не знаем»: файла нет, он не читается или в нём не запись. Это не
    «прошла» и не «не прошла»: приёмку пишет отдельный шаг манхвы, и ролик вполне
    может лежать готовым, пока шаг до него не дошёл.

    Отпечаток считается от ВЕРДИКТА, а не от байтов файла: порядок ключей и отступы
    меняются от версии того, кто пишет json, а вердикт — только от новой сборки.
    """
    try:
        with open(os.path.join(folder, "out", "acceptance.json"), encoding="utf-8") as f:
            acc = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(acc, dict):
        return {}
    problems = acc.get("problems")
    problems = [_text(p) for p in problems] if isinstance(problems, list) else []
    body = [acc.get("ok") is True, acc.get("duration"), acc.get("width"),
            acc.get("height"), problems]
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    return {"ok": acc.get("ok") is True, "duration": acc.get("duration"),
            "problems": problems[:4], "digest": digest.hexdigest()[:16]}


def passport(folder: str, video: str, cover: str) -> dict:
    """Опознание принятой версии: отпечаток ролика, обложки и вердикт сборки."""
    return {"video": file_mark(video), "cover": file_mark(cover),
            "build": build_mark(folder)}


def is_accepted(entry) -> bool:
    """Проходил ли этот ролик приёмку. Пустота и мусор — «нет», а не «да».

    Отдельно про ролики, залитые прежней версией моста (решение владельца, пункт 7):
    у них паспорта нет и быть не может. Они помечаются `accepted: False` явно, чтобы
    пустота никогда не читалась как «проверено».
    """
    return bool(entry) and isinstance(entry, dict) and entry.get("accepted") is True


def unknown_version(now: float) -> dict:
    """Отметка «версия неизвестна» для ролика, залитого прежней версией моста."""
    return {"accepted": False, "version": "unknown", "at": now,
            "why": "залит прежней версией моста — какую версию файла приняли, не записано"}


def taken(release_id: str, uc: str, mark: dict, now: float) -> dict:
    """Паспорт допущенного ролика: что именно приняли и под каким номером выпуска."""
    return {"accepted": True, "version": "known", "at": now,
            "release_id": release_id, "uc": uc, **mark}


def mismatch(saved, fresh) -> str:
    """Чем ролик на диске отличается от принятого. Совпал — пустая строка.

    Порядок ответов важен: человеку нужна ПЕРВАЯ причина, по которой файл перестал
    быть тем самым, а не список всех следствий одной пересборки.

    Паспорта нет вовсе (задание создано прежней версией моста) — молчим: объявить
    старое задание пересобранным значило бы остановить выпуски, с которыми всё в
    порядке. «Не знаем» и «пересобрали» — разные ответы, и путать их здесь дорого.
    """
    if not is_accepted(saved):
        return ""
    old, new = (saved.get("video") or {}), (fresh.get("video") or {})
    if old and not new:
        return "файла ролика больше нет на месте — принятой версии не существует"
    if old and new:
        if old.get("size") != new.get("size"):
            return ("ролик пересобран после приёмки: при приёмке было %s, сейчас %s"
                    % (human_size(old.get("size")), human_size(new.get("size"))))
        if old.get("edges") != new.get("edges"):
            return "ролик пересобран после приёмки: размер прежний, а содержимое другое"
    # Обложка. Её шаг у манхвы идёт ПОСЛЕ сборки (web_queue.STEPS), поэтому обложка,
    # появившаяся после приёмки, — не повод останавливать выпуск: в задании её всё
    # равно нет. А вот ПРОПАВШАЯ принятая обложка — повод: задание обещает ноде файл,
    # которого больше нет, и она узнает об этом посреди заливки.
    old_cover, new_cover = (saved.get("cover") or {}), (fresh.get("cover") or {})
    if old_cover and not new_cover:
        return "обложка, принятая вместе с роликом, исчезла с диска"
    old_build, new_build = (saved.get("build") or {}), (fresh.get("build") or {})
    if old_build.get("digest") and not new_build.get("digest"):
        return "приёмка сборки (out/acceptance.json) исчезла — подтвердить версию нечем"
    if old_build.get("digest") and new_build.get("digest") \
            and old_build["digest"] != new_build["digest"]:
        return "проект пересобран после приёмки: машинная приёмка сборки дала другой ответ"
    return ""


# ── настройки моста ───────────────────────────────────────────────────────────────
#
# Почему своим файлом, а не колонкой в pub_channels. Таблица каналов чужая: её
# заводит и правит кабинет (`server/pub_schema.py`), мост в неё только пишет задания.
# ALTER чужой таблицы ставит мост в зависимость от схемы соседа, откатывается руками
# и переживает любой наш откат. Признак нужен одному мосту, ставится и снимается
# словом владельца — поэтому живёт в его собственном файле рядом с мостом: правка
# одной строки, схема соседа не трогается вовсе.

SETTINGS_DEFAULT = {"covers": set(), "covers_everywhere": False, "host_required": True}


def load_settings(path: str) -> dict:
    """Настройки моста из своего файла. Нет файла — как было раньше; битый — отказ.

    Битый файл не читается как пустой намеренно: пустые настройки выключают ворота
    обложки, и опечатка в json тихо вернула бы мост к поведению до 01.10.
    """
    if not path or not os.path.exists(path):
        return dict(SETTINGS_DEFAULT)
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except OSError as exc:
        raise GateError("не удалось прочитать %s: %s" % (path, exc)) from None
    except ValueError as exc:
        raise GateError("%s — не JSON: %s" % (path, exc)) from None
    if not isinstance(data, dict):
        raise GateError("%s: нужна запись вида {\"обложка_обязательна\": [ … ]}" % path)
    # Незнакомый ключ — отказ, а не тишина: опечатка в названии признака оставила бы
    # ворота открытыми, и владелец считал бы, что обложку он уже потребовал.
    known = ("обложка_обязательна", "обложка_обязательна_везде", "машина_обязательна")
    strange = [k for k in data if not k.startswith("_") and k not in known]
    if strange:
        raise GateError("%s: непонятные ключи %s; бывают только %s (пояснения — ключом "
                        "с подчёркиванием впереди)"
                        % (path, ", ".join(sorted(strange)), ", ".join(known)))
    marks = data.get("обложка_обязательна", [])
    if not isinstance(marks, list) or not all(isinstance(m, str) for m in marks):
        raise GateError("%s: обложка_обязательна — список UC или хендлов каналов" % path)
    out = {"covers": {_norm(m) for m in marks if _text(m)}}
    for key, field in (("covers_everywhere", "обложка_обязательна_везде"),
                       ("host_required", "машина_обязательна")):
        value = data.get(field, SETTINGS_DEFAULT[key])
        if not isinstance(value, bool):
            raise GateError("%s: %s — да или нет (true/false)" % (path, field))
        out[key] = value
    return out


def cover_required(settings: dict, ch) -> bool:
    """Нужна ли этому каналу обложка. Канал узнаём и по UC, и по хендлу.

    Два имени у одного признака — чтобы владельцу не приходилось искать UC: хендл
    он знает наизусть. Двусмысленности это не даёт, строка канала несёт оба.
    """
    if settings.get("covers_everywhere"):
        return True
    marks = settings.get("covers") or set()
    return _norm(ch["uc"]) in marks or _norm(ch["handle"]) in marks


# ── канал по идентификатору ───────────────────────────────────────────────────────

def _channels(c) -> list:
    return c.execute("SELECT * FROM pub_channels").fetchall()


def _many(what: str, hits: list) -> str:
    return ("%s в кабинете у %d каналов (%s) — какой из них имелся в виду, решает "
            "человек" % (what, len(hits), ", ".join(_text(h["uc"]) for h in hits[:3])))


def resolve_channel(c, meta: dict, name_to_uc: dict):
    """Канал ролика по идентификатору: (строка канала, причина отказа).

    Порядок связей — от самой надёжной к самой хрупкой:
      1. UC из паспорта проекта: он не меняется ни от переименования канала, ни от
         правки справочника;
      2. хендл — `project.json: channel`, ключ пресета манхвы (`манхва/channels.py`:
         «например хендл канала без @»); в кабинете это `pub_channels.handle`;
      3. название — последняя попытка, и только если оно опознаёт РОВНО один канал.

    Название — не связь, а подпись. Прежний мост делал `NAME_TO_UC.get(name)` и шёл
    дальше: имя, записанное в проекте манхвы руками, решало, на какой канал уедет
    ролик. Поэтому здесь любое множественное совпадение — отказ, а не первый из
    найденных: ролик на чужом канале руками не отзывается.
    """
    uc = _text(meta.get("channel_uc")) or _text(meta.get("uc"))
    if uc:
        row = c.execute("SELECT * FROM pub_channels WHERE uc=?", (uc,)).fetchone()
        if row is None:
            return None, "канала с UC %s в кабинете нет" % uc
        return row, ""
    rows = _channels(c)
    key = _norm(meta.get("channel"))
    name = _text(meta.get("channel_name"))
    if key:
        hits = [r for r in rows if _norm(r["handle"]) == key]
        if len(hits) > 1:
            return None, _many("хендл @%s" % key, hits)
        if hits:
            return hits[0], ""
    if name:
        uc = _text(name_to_uc.get(name))
        if uc:
            row = c.execute("SELECT * FROM pub_channels WHERE uc=?", (uc,)).fetchone()
            if row is None:
                return None, "канала «%s» (UC %s) в кабинете нет" % (name, uc)
            return row, ""
        hits = [r for r in rows if _text(r["name"]).casefold() == name.casefold()]
        if len(hits) > 1:
            return None, _many("название «%s»" % name, hits)
        if hits:
            return hits[0], ""
    said = " / ".join(x for x in ("@%s" % key if key else "",
                                  "«%s»" % name if name else "") if x)
    if said:
        return None, "канала %s в кабинете нет" % said
    return None, "в паспорте проекта канал не указан: ни UC, ни хендла, ни названия"


def channel_allowed(c, ch, settings: dict) -> str:
    """Разрешено ли вообще лить на этот канал. Пусто — разрешено.

    «Режим публикации разрешён» из ТЗ владелец прочитал как разрешение на КАНАЛ, а не
    как видимость ролика: видимость остаётся public. Паузу мост умел и раньше —
    каналы без подтверждённого телефона YouTube отбивает на длинном ролике
    (`manga_channels.py`). Здесь к ней добавлено остальное, без чего заливка всё
    равно не состоится, но состоится не сразу, а после брони профиля и качания
    гигабайтов на Минск.

    Про машину. В кабинете машина канала — `pub_channels.host_id`, а при пустой
    колонке — машина профиля (`pub_profiles.default_host_id`). Выдача задания
    (`pub_upload_api`, ручка claim) по машине не фильтрует вовсе: задание забирает
    любой, кто попросит нужный профиль. Поэтому требование машины — выключаемое:
    на боевой базе `manga_channels.py` host_id каналам не ставил, и жёсткая проверка
    запарковала бы все 20 каналов манхвы сразу. Решение — за владельцем, не за нами.
    """
    status = _text(ch["status"]) or "active"
    if status != "active":
        return ("канал не в работе (состояние «%s»): либо телефон не подтверждён, "
                "либо канал снят с заливки человеком" % status)
    if not _text(ch["uc"]):
        return "у канала в кабинете нет UC — отправлять ролик некуда"
    if ch["profile_id"] is None:
        return "у канала нет профиля AdsPower — входить в аккаунт нечем"
    if not settings.get("host_required"):
        return ""
    if ch["host_id"] is not None:
        return ""
    row = c.execute("SELECT default_host_id FROM pub_profiles WHERE id=?",
                    (ch["profile_id"],)).fetchone()
    if row is not None and row["default_host_id"] is not None:
        return ""
    return ("у канала не указана машина заливки: ни host_id у канала, ни машина по "
            "умолчанию у профиля %s. Поставить host_id в кабинете либо снять "
            "требование в manga_gate.json (машина_обязательна)" % ch["profile_id"])


# ── сам допуск ────────────────────────────────────────────────────────────────────

def build_refusal(mark: dict) -> str:
    """Отказ по сборке словами; пусто — сборка принята машинной приёмкой манхвы."""
    if not mark:
        return ("машинной приёмки сборки нет (out/acceptance.json): прошёл ли ролик "
                "обязательные проверки — неизвестно, а неизвестно это не «прошёл»")
    if not mark.get("ok"):
        beda = "; ".join(mark.get("problems") or []) or "причина в файле не записана"
        return "ролик не прошёл проверки сборки: " + beda
    return ""


def title_refusal(meta: dict) -> str:
    """Отказ по названию. Пустое название — это платный вызов не из чего."""
    if not _text(meta.get("title")):
        return ("у ролика нет названия в паспорте проекта: название каналу пишет "
                "платная модель, и писать ей было бы не из чего")
    return ""


def cover_refusal(mark: dict) -> str:
    """Отказ по обложке у канала, которому она обязательна."""
    if not mark:
        return ("канал требует обложку, а out/cover.png у ролика нет. Дождаться шага "
                "«Обложка» либо снять требование в manga_gate.json")
    if not mark.get("size"):
        return "обложка ролика пустая (0 байт) — заливать такую нечем"
    return ""


def admit(c, meta: dict, mark: dict, settings: dict, name_to_uc: dict):
    """Допуск ролика в очередь заливки: (канал, причина отказа).

    Причина пуста — ролик допущен. Порядок проверок — часть требования владельца:
    любая из них обязана случиться ДО вызова `make_title`, потому что платный вызов
    на непринятый ролик сжигает деньги и ничего не ловит. Поэтому первым идёт самый
    дешёвый вопрос — прошёл ли ролик сборку, — и ни одна проверка не смотрит в сеть.

    Паспорт версии (`mark`) считает вызывающий: он нужен и здесь, и при сверке уже
    стоящих в очереди роликов, а читать края гигабайтного файла дважды незачем.
    """
    why = build_refusal(mark.get("build") or {}) or title_refusal(meta)
    if why:
        return None, why
    ch, why = resolve_channel(c, meta, name_to_uc)
    if why:
        return None, why
    why = channel_allowed(c, ch, settings)
    if why:
        return None, why
    if cover_required(settings, ch):
        why = cover_refusal(mark.get("cover") or {})
        if why:
            return None, why
    return ch, ""


# ── хранилище паспортов ───────────────────────────────────────────────────────────

def load_store(path: str) -> dict:
    """Паспорта принятых версий. Нет файла — пусто; битый файл — отказ.

    Пустота и мусор здесь означают разное: пустота — «мост ещё никого не принимал»,
    мусор — «мы не знаем, что принимали». Прочитать мусор как пустоту значило бы
    сначала признать все стоящие в очереди задания непроверенными, а потом затереть
    файл поверх. Поэтому мост останавливается и говорит, какой файл починить.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except OSError as exc:
        raise GateError("не удалось прочитать %s: %s" % (path, exc)) from None
    except ValueError as exc:
        raise GateError("%s — не JSON: %s" % (path, exc)) from None
    if not isinstance(data, dict):
        raise GateError("%s: нужна запись «проект → паспорт принятой версии»" % path)
    return data


def save_store(path: str, data: dict) -> None:
    """Записать паспорта целиком. Через временный файл: прогон по cron могут убить.

    Запись на месте оставила бы обрезанный json, а обрезанный json — это остановка
    моста на следующем прогоне (см. load_store) вместо одной потерянной строки.
    """
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".manga_passports.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
