#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Провайдер лёг — это не наша поломка, и повторяет машина (02.10.2026).

Повод. Озвучка упала с `rc=1`. В журнале стояло «Lumean: /orders: HTTP 503: Сервис
временно недоступен: идут технические работы», а в карточку и в тревогу уезжало голое
`rc=1`. Владелец: «давай прямо так и писать эту ошибку в студии, чтобы у меня не было
ощущения, что проблема с нашей стороны, плюс раз сервис недоступен — делать ретрай
автоматический через 10 минут, если не получилось — уведомлять».

Здесь проверяется распознавание и его границы — то, на чём всё держится:

1. **Что считается простоем провайдера.** 502/503/504 и его собственные слова про
   технические работы.
2. **Что НЕ считается.** Кончились деньги, отказ ключа, нет прав. Эти повтором не
   лечатся, и автоповтор по ним жёг бы деньги по кругу. Проверяется отдельно и
   придирчиво: строка «HTTP 503» рядом со словом «quota» простоем НЕ считается.
3. **Слова провайдера важнее кода возврата** в тексте ошибки задачи, но ниже отказа
   ключа: ключ чинить нам, простой — не нам.
4. **Деньги сильнее всего.** Оплачен хоть один вызов этапа — автоповтора нет, зовём
   человека. Не смогли посчитать — считаем, что оплачено: ошибиться в эту сторону
   дешевле.

    .venv/bin/python tests/test_provider_down_retry.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

os.environ.setdefault("VS_SERVER_DISABLE_BACKGROUND", "1")
# Воркеры лежат отдельным каталогом, а кабинет ищет их по ~/VideoStudio/src — так его
# запускают и стенд, и боевая машина (dev/run.sh, deploy/start-studio.sh кладут туда
# ссылку). Для проверки распознавателя поднимать весь HOME незачем: достаточно показать
# каталог воркеров.
sys.path.insert(0, os.path.join(BASE, "workers"))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def main() -> int:
    import main as M                                              # noqa: PLC0415

    print("Простой провайдера узнаётся по его же словам")
    живое = [
        ("Lumean: /orders: HTTP 503: Сервис временно недоступен: идут технические "
         "работы. Попробуйте позже.; reason=", "строка из боя 02.10.2026"),
        ("FastGen: HTTP 502 Bad Gateway", "502"),
        ("provider: HTTP 504 gateway timeout", "504"),
        ("Service Unavailable", "словами"),
        ("API temporarily unavailable, try later", "temporarily unavailable"),
        # Каждый признак отдельно, без соседей. Иначе проверка держится на том, что в
        # строке совпало ЧТО-ТО, и выпавший признак она не заметит: строка из боя несёт
        # сразу и «HTTP 503», и «технические работы».
        ("провайдер ответил HTTP 503", "только код 503"),
        ("Сервис на технических работах, зайдите позже", "только слова про работы"),
        ("Идут техработы до 18:00", "только корень «техработ»"),
        # 03.10.2026, бой: пять озвучек FastGen встали в разбор с этим ответом.
        ("⚠️ FastGen: блок 8: генерация gen_0432d0f5f93946a08f55453d6896469f — failed: "
         "No accounts available, please try again later", "FastGen: нет свободных аккаунтов"),
        ("No accounts available", "только «нет аккаунтов»"),
        ("upstream busy, please try again later", "только «try again later»"),
    ]
    for строка, почему in живое:
        check(f"узнан простой ({почему})", bool(M._provider_down_line(строка)), True)
        # В карточку уходит СТРОКА ПРОВАЙДЕРА, а не наш пересказ.
        check(f"и возвращены его слова ({почему})",
              M._provider_down_line(строка)[:20], " ".join(строка.split())[:20])

    print("\nЧужая беда, которая повтором не лечится — не простой")
    мимо = [
        ("HTTP 402: insufficient balance", "кончились деньги"),
        ("API key token allowance exhausted", "лимит ключа выбран"),
        ("HTTP 401 Unauthorized", "ключ отвергнут"),
        ("HTTP 403 Forbidden", "нет прав"),
        ("HTTP 429: quota exceeded", "квота"),
        ("quota exceeded, please try again later", "квота, хоть и «try again later»"),
        ("ЛИМИТ АККАУНТА ПРОВАЙДЕРА исчерпан", "лимит аккаунта"),
        ("ключ истёк", "про ключ"),
        ("", "пустая строка"),
        ("   ", "пробелы"),
        ("всё хорошо, озвучка готова", "обычная строка"),
    ]
    for строка, почему in мимо:
        check(f"не принято за простой ({почему})", M._provider_down_line(строка), "")

    print("\nСамое важное: деньги рядом с 503 отменяют простой")
    # Провайдер вполне может ответить 503 и при этом сказать, что дело в деньгах.
    # Повторять такое — жечь попытки в никуда.
    смесь = "HTTP 503 Service Unavailable: insufficient funds on account"
    check("503 со словом про деньги простоем не считается",
          M._provider_down_line(смесь), "")

    print("\nСлова провайдера важнее кода возврата, но ниже отказа ключа")
    было = M._key_refusal_text
    M._key_refusal_text = lambda *a, **k: ""
    try:
        check("без иных причин в карточку идут слова провайдера",
              M._worker_error_text(1, 1, "", "", "", "HTTP 503: технические работы"),
              "HTTP 503: технические работы")
        check("голого rc не остаётся", "rc=1" in M._worker_error_text(
            1, 1, "", "", "", "HTTP 503: технические работы"), False)
        check("без провайдера остаётся rc", M._worker_error_text(1, 7), "rc=7")
        M._key_refusal_text = lambda *a, **k: "ключ истёк"
        check("отказ ключа сильнее простоя",
              M._worker_error_text(1, 1, "k", "", "", "HTTP 503"), "ключ истёк")
    finally:
        M._key_refusal_text = было

    print("\nПриговор по каналу промптов доезжает до карточки")
    # Было: воркер печатает «У канала промптов «openai» исчерпан счёт», а в тревогу
    # уезжает `rc=2` — по нему не понять, что кончились деньги (владелец, 02.10.2026).
    приговор = 'У канала промптов «openai» исчерпан счёт/лимит: no credits remaining'
    check("приговор узнан", bool(M._prompt_channel_line("❌ " + приговор)), True)
    check("и украшения лога убраны",
          M._prompt_channel_line("❌ " + приговор).startswith("У канала"), True)
    # Ход работы приговором не считается: иначе в карточку уедет не причина, а течение.
    # Строки ниже ОБЯЗАНЫ содержать «канал промптов» — иначе проверка держалась бы на
    # том, что слова про канал в них вообще нет, и охрану от «хода работы» не трогала.
    for строка, почему in (
            ("ℹ️ канал промптов «openai»: ключ sk-…oiAA не паркую — отказ 1 из 3",
             "не паркую"),
            ("↔️ канал промптов «openai» продолжает работать: живых ключей 2",
             "продолжает работать"),
            ("😴 канал промптов «openai»: ключ отдыхает до 14:00", "отдыхает"),
            ("⏱ канала промптов попытка 2 из 3", "попытка"),
            ("обычная строка воркера", "не про канал")):
        check(f"не принято за приговор ({почему})", M._prompt_channel_line(строка), "")
    было = M._key_refusal_text
    M._key_refusal_text = lambda *a, **k: ""
    try:
        check("в карточку идёт приговор, а не rc",
              M._worker_error_text(1, 2, "", "", "", "", приговор), приговор)
        # Но отказ ключа генерации всё равно сильнее: его чинить нам и в другом месте.
        M._key_refusal_text = lambda *a, **k: "ключ истёк"
        check("отказ ключа сильнее приговора по каналу",
              M._worker_error_text(1, 2, "k", "", "", "", приговор), "ключ истёк")
    finally:
        M._key_refusal_text = было

    print("\nРасписание повторов — то, что просил владелец")
    check("три попытки", len(M._PROVIDER_DOWN_DELAYS), 3)
    check("по десять минут", list(M._PROVIDER_DOWN_DELAYS), [600, 600, 600])

    print("\nСлед прошлой попытки узнаётся отдельно от простоя провайдера")
    # Воркер говорит «orphan receipt не подтверждает точный оплаченный заказ» и
    # останавливается — и правильно: решать про деньги по косвенным признакам не его
    # дело. Узнаём эту строку, чтобы спросить журнал.
    for текст, почему in (
            ("Lumean full-text: orphan receipt не подтверждает точный оплаченный заказ; "
             "новый заказ автоматически не создан.", "строка из боя"),
            ("orphan receipt", "коротко")):
        check(f"узнан след прошлой попытки ({почему})",
              bool(M._stale_receipt_line(текст)), True)
    for текст, почему in (("HTTP 503 Service Unavailable", "простой провайдера"),
                          ("rc=1", "голый код"), ("", "пусто")):
        check(f"не принято за след ({почему})", M._stale_receipt_line(текст), "")
    # И наоборот: след прошлой попытки простоем провайдера не считается.
    check("след не выдаётся за простой",
          M._provider_down_line("orphan receipt не подтверждает точный оплаченный заказ"), "")

    print("\nСлед первой попытки убирается, иначе повтор упрётся в себя")
    # Найдено на боевой машине 02.10.2026: первая попытка пишет «собираюсь заказать»
    # ДО обращения к провайдеру, провайдер отвечает 503, запись остаётся — и ВТОРАЯ
    # попытка падает уже не на 503, а на собственный след: «orphan receipt».
    import io as _io, json as _json, tempfile                      # noqa: PLC0415
    гнездо = tempfile.mkdtemp(prefix="receipts-")
    папка = os.path.join(гнездо, "_FINAL_AUDIO_LUMEAN")
    os.makedirs(папка)

    def квитанция(имя, **поля):
        with _io.open(os.path.join(папка, имя), "w", encoding="utf-8") as f:
            _json.dump(поля, f)

    квитанция("1.mp3.lumean.json", provider="lumean", state="creating")
    check("квитанция без номера отодвинута", M._park_unordered_receipts(гнездо), 1)
    check("и файл больше не мешает воркеру",
          os.path.exists(os.path.join(папка, "1.mp3.lumean.json")), False)
    # Но сам файл остался: квитанция — свидетельство, выбрасывать его нельзя.
    check("свидетельство осталось на диске",
          any(f.startswith("1.mp3.lumean.json.not-ordered-") for f in os.listdir(папка)), True)

    квитанция("2.mp3.lumean.json", provider="lumean", state="created", order_id="ord-77")
    check("квитанцию С НОМЕРОМ не трогаем", M._park_unordered_receipts(гнездо), 0)
    check("она на месте", os.path.exists(os.path.join(папка, "2.mp3.lumean.json")), True)

    готовое = os.path.join(гнездо, "_FINAL_AUDIO_LUMEAN2")
    os.makedirs(готовое)
    with _io.open(os.path.join(готовое, "1.mp3"), "w") as f:
        f.write("звук")
    with _io.open(os.path.join(готовое, "1.mp3.lumean.json"), "w", encoding="utf-8") as f:
        _json.dump({"provider": "lumean", "state": "creating"}, f)
    # Рядом лежит готовый звук — значит заказ был и оплачен, что бы ни стояло в состоянии.
    check("где рядом есть mp3 — не трогаем", M._park_unordered_receipts(готовое), 0)

    print("\nБронь снимается только та, по которой заказа не было")
    # Второй след первой попытки — строка журнала платных вызовов. Снимать её можно
    # ровно тогда, когда журнал САМ говорит, что заказа не было: состояние `reserved`
    # без номера. В `ordered` переводит только получение номера от провайдера, поэтому
    # «reserved без номера» — не догадка про 5xx, а состояние машины состояний.
    import sqlite3 as _sq, tempfile as _tf                          # noqa: PLC0415
    import pub_calls                                               # noqa: PLC0415
    файл = os.path.join(_tf.mkdtemp(prefix="calls-"), "vsgen.db")

    def связь():
        """Как настоящая `db()`: НОВОЕ соединение на каждый вызов. Проверяемый код
        закрывает его за собой, и общий объект на всех тут не годится."""
        c = _sq.connect(файл)
        c.row_factory = _sq.Row
        return c

    c0 = связь()
    pub_calls.ensure_calls_schema(c0)
    c0.execute("INSERT INTO provider_calls(tenant_id,project_id,release_id,kind,unit,"
               "fingerprint,provider,status,created,updated) "
               "VALUES('t','p1','r','tts','all','f1','lumean','reserved',1,1)")
    c0.execute("INSERT INTO provider_calls(tenant_id,project_id,release_id,kind,unit,"
               "fingerprint,provider,status,remote_order_id,created,updated) "
               "VALUES('t','p1','r','tts','two','f2','lumean','reserved','ord-9',1,1)")
    c0.commit(); c0.close()

    было_db = M.db
    M.db = связь
    try:
        check("журнал опровергает заказ по одной брони",
              M._unordered_reservations("p1", "tts"), 1)
        # Решение про деньги проверяется отдельно от всего остального: убрать след
        # можно, только если журнал заказ опровергает.
        check("след можно убрать, журнал заказ опровергает",
              M._stale_trace_clearable("p1", "tts"), True)
        check("снята одна бронь", M._clear_unordered_reservation("p1", "tts"), 1)
        c1 = связь()
        состояния = {r["fingerprint"]: r["status"] for r in
                     c1.execute("SELECT fingerprint, status FROM provider_calls")}
        c1.close()
        check("бронь без номера закрыта", состояния["f1"], "failed")
        # Строку с номером не трогаем: там заказ есть, его надо проверять, а не создавать
        # второй. Ровно та разница, ради которой журнал и заведён.
        check("бронь С НОМЕРОМ осталась", состояния["f2"], "reserved")
        check("повторный проход уже ничего не снимает",
              M._clear_unordered_reservation("p1", "tts"), 0)
        # Спросить «можно ли» и снять — разные действия. После снятия журнал больше не
        # опровергает заказ, и след прошлой попытки уходит человеку, а не машине.
        check("журнал больше не опровергает заказ",
              M._unordered_reservations("p1", "tts"), 0)
        check("и след убирать больше нельзя — разбирает человек",
              M._stale_trace_clearable("p1", "tts"), False)
        check("без проекта тоже нельзя", M._stale_trace_clearable("", "tts"), False)
        check("без проекта не трогаем ничего", M._clear_unordered_reservation("", "tts"), 0)
    finally:
        M.db = было_db

    print("\nНе смогли посчитать деньги — считаем, что они были")
    # Осторожность асимметрична намеренно: ошибка в эту сторону стоит звонка человеку,
    # в обратную — повторно оплаченного этапа.
    check("без проекта считаем, что оплачено", M._paid_calls_done("", "tts") > 0, True)

    print("\nПриговор воркера после «try again later» по ходу — он и причина (11.10.2026)")
    # Бой: картинки падали на «Недостающих кадров 5 из 242 — больше 2%», а в карточку
    # уезжала промежуточная «[103] Попытка 2 неудачна … please try again later».
    журнал = [
        "[03:50:36.011] ⚠️ ⚠️ [103] Попытка 2 неудачна (Nano Banana 2 Lite (Flow)): "
        "Nano Banana 2 Lite (Flow): Generation failed, please try again later",
        "[03:58:10.128] ❌ ⛔ [96] Контент-фильтр провайдера — пропускаю картинку (смягчений: 3)",
        "[03:58:10.821] ⚠️ ⚠️ Не хватает 5 кадров из 242: сцены 22, 28, 94, 95, 96",
        "[03:58:11.001] ❌ ⛔ Недостающих кадров 5 из 242 — больше 4 (2%): кадрами "
        "предыдущих сцен не заменяю, нужен разбор",
        "[03:58:11.097] ❌ Контракт референсов отклонён: authoritative image receipt "
        "is not exact for frame 022",
    ]
    down, verdict = "", ""
    for строка in журнал:          # тот же порядок, что в _run_task
        d = M._provider_down_line(строка)
        if d:
            down, verdict = d, ""
        elif down and not verdict:
            verdict = M._own_verdict_line(строка)
    err = M._worker_error_text(0, 1, provider_down_msg=M._down_or_verdict(down, verdict))
    check("причина — итог этапа, а не попытка кадра",
          err.startswith("Недостающих кадров 5 из 242"), True)
    check("слова провайдера остались в скобках", "try again later" in err, True)
    check("строка кадра [N] приговором не считается",
          M._own_verdict_line(журнал[1]), "")
    check("итоговая строка сервера — тоже",
          M._own_verdict_line("❌ ошибка (код 1): rc"), "")
    check("простой и есть итог — текст прежний",
          M._down_or_verdict("HTTP 503: техработы", ""), "HTTP 503: техработы")
    # Простой ПОСЛЕ приговора снова главный: приговор сбрасывается.
    down, verdict = "", ""
    for строка in журнал[3:] + ["[04:00:00.000] ❌ Lumean: HTTP 503 Service Unavailable"]:
        d = M._provider_down_line(строка)
        if d:
            down, verdict = d, ""
        elif down and not verdict:
            verdict = M._own_verdict_line(строка)
    check("последним был простой — он и причина",
          M._down_or_verdict(down, verdict).startswith("[04:00:00.000] ❌ Lumean"), True)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
