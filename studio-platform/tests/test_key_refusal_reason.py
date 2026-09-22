#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отказ ключа доезжает до человека ПЕРВОЙ причиной, а не последней строкой лога.

Какой дефект ловится. Упавшая задача показывала владельцу `rc=3`. Всё, что кабинет
умел вынуть из вывода воркера, — это ПОСЛЕДНЮЮ опознанную строку: `_scan_gen_limit_line`
запоминает последний «ЛИМИТ АККАУНТА ПРОВАЙДЕРА», `channel_incident` перезаписывается при
каждом новом отказе канала, а `_judge_success` в ветке rc=0 берёт последнюю строку с «❌».

Для канала промптов это правильно: там безликая «Command failed with exit code 1»
прилетает раньше, чем «You've hit your limit». Для КЛЮЧА — наоборот. Провайдер называет
причину один раз, в первом же отказе («HTTP 401 key expired»), а дальше сыплются
следствия: оборванные скачивания, пустые результаты, «все попытки исчерпаны». Человек
видел последнее следствие и не мог понять, что чинить: менять ключ, чинить права,
подождать окно или пополнить счёт — это четыре разных действия.

Что проверяется здесь:

  * первая опознанная причина не затирается ни следствиями, ни повторами;
  * строки про ОЖИДАНИЕ и ВОССТАНОВЛЕНИЕ («жду сброса окна», «повтор через 30с»,
    «канал продолжает работать») причиной не считаются, хотя и содержат и «429»,
    и слово «лимит» — иначе они заняли бы место настоящего отказа;
  * четыре вида различаются словами: истёк/отозван, отказ авторизации, лимит аккаунта,
    исчерпан счёт;
  * HTTP-код сохраняется в тексте, а ключ назван маской, а не целиком;
  * `rc=N` остаётся только тогда, когда причину не опознал никто;
  * секрет из ответа провайдера не доезжает ни до карточки, ни до колонки `error`
    журнала вызовов — второй путь записи (POST /api/calls/{id}/failed) до этой работы
    редактуры не имел вовсе; в том числе ОБРЕЗАННЫЙ по дороге ключ и случай, когда
    кабинет вообще не импортирован (тогда журнал режет по образцу сам);
  * строка про ЧУЖОЙ ключ (канал промптов, квота YouTube Data API) не записывается
    как отказ ключа задачи и не занимает место настоящей причины;
  * разовый «мигающий» 401 не становится вечным вердиктом: успешный платный кадр тем
    же ключом освобождает место под настоящую причину.

Сети нет, воркеров нет: скану скармливаются строки, которые воркер печатает в бою.

    .venv/bin/python tests/test_key_refusal_reason.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="key-refusal-")

# Своя база и свой каталог тенантов: рядом работают чужие стенды на dev/vsgen.dev.db.
os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_SERVER_DB"] = os.path.join(TMP, "vsgen.db")
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import main as M                                                  # noqa: E402
import pub_calls as pc                                            # noqa: E402

FAILS = []

# Ключ-образец: длинный, чтобы маска показывала голову и хвост, как в бою.
KEY = "sk-proj-AAAABBBBCCCCDDDDEEEEFFFF1111222233334444"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def feed(jid, lines):
    """Прогнать поток строк воркера через скан ровно так, как это делает _run_task."""
    M._clear_key_refusal(jid)
    for line in lines:
        M._scan_key_refusal(jid, line)


def test_first_reason_wins():
    print("\nПервая причина побеждает следствия и последнюю строку лога")
    jid = "job-first"
    feed(jid, [
        "▶ старт googler_worker",
        "⚠️ [1] Непонятный транзиент: Connection aborted",
        '❌ [1] v6 generation отклонён: HTTP 401: {"error":"api key expired"}',
        "❌ [2] v6: не скачался download_url (3 попытки): IncompleteRead",
        "❌ [7] Все 12 попыток исчерпаны (NARWHAL): NARWHAL: v6 генерация не удалась",
    ])
    txt = M._key_refusal_text(jid, KEY)
    check("вид опознан как «истёк или отозван»",
          M._key_refusal_reason(jid)["kind"], "expired")

    # ГЛАВНОЕ: следом идёт ДРУГОЙ опознаваемый отказ. Ровно он и затирал первую причину
    # до правки. Заменить setdefault-семантику _scan_key_refusal на update (то есть
    # убрать `if str(jid) in _KEY_REFUSAL: return None`) — эти три проверки падают.
    M._scan_key_refusal(jid, '❌ [9] v6 generation отклонён: HTTP 403: {"error":"unauthorized"}')
    after = M._key_refusal_reason(jid)
    check("второй опознанный отказ первую причину НЕ затирает", after["kind"], "expired")
    check("и код остался от первой причины", after["http"], 401)
    check("и текст тоже", "api key expired" in after["text"], True)
    txt = M._key_refusal_text(jid, KEY)
    check_true("в карточке слова про истёкший ключ", "истёк или отозван" in txt)
    check_true("HTTP-код сохранён", "(HTTP 401)" in txt)
    check_true("ключ назван маской", "sk-proj-AAAA…4444" in txt)
    check("целиком ключ наружу не уходит", KEY in txt, False)
    check("последняя строка лога карточку не занимает",
          "попыток исчерпаны" in txt, False)

    err = M._worker_error_text(jid, 3, KEY)
    check("в карточке причина, а не код выхода", err.startswith("ключ sk-proj-AAAA…4444:"), True)
    check("«rc=3» вытеснено", err == "rc=3", False)
    check("текст влезает в колонку ошибки задачи (200)", len(err) <= 200, True)


def test_recovery_lines_do_not_take_the_slot():
    print("\nОжидание и восстановление причиной не считаются")
    for line in ("⏳ Часовая квота ключа исчерпана (HTTP 429 rate_limit.hourly_exceeded) — "
                 "потоки ждут сброса окна, запросы приостановлены",
                 "⚠️ план сцен: временная ошибка (1/3): HTTP 429: too many requests; "
                 "повтор через 30с",
                 "↔️ канал «claude» продолжает работать: живых ключей 2",
                 "⚠️ Похоже, у канала промптов «openai» исчерпан счёт/лимит: 429 — "
                 "проверяю ещё раз",
                 "✅ [3] Изображение через NARWHAL (v6)",
                 "🔄 Повтор через 8с..."):
        check(f"не причина: {line[:46]}…", M._scan_key_refusal("job-rec", line), None)

    # И главное: такая строка не должна мешать настоящей, пришедшей следом.
    jid = "job-rec2"
    feed(jid, [
        "⏳ Часовая квота ключа исчерпана (HTTP 429) — потоки ждут сброса окна",
        '❌ [4] v6 generation отклонён: HTTP 401: {"error":"invalid api key"}',
    ])
    check("настоящая причина записалась после строки ожидания",
          M._key_refusal_reason(jid).get("kind"), "auth")


def test_four_kinds_are_told_apart():
    print("\nЧетыре вида отказа различаются словами")
    cases = [
        ("expired", "истёк или отозван",
         '❌ [1] v6 generation отклонён: HTTP 401: {"error":{"code":"token_expired",'
         '"message":"API key has expired"}}'),
        ("auth", "отказ авторизации при создании заказа",
         "❌ [1] v6 generation отклонён: HTTP 403: {\"error\":\"unauthorized\"}"),
        ("account_limit", "исчерпан лимит аккаунта провайдера",
         "⛔ [5] ЛИМИТ АККАУНТА ПРОВАЙДЕРА (VEO_BANANA): кончился лимит аккаунта — "
         "смените модель или подождите 20-30 минут. Ответ провайдера: HTTP 429 RATE_LIMIT"),
        ("quota", "исчерпан счёт или квота ключа",
         "❌ сцена 3: HTTP 429: {\"error\":{\"code\":\"insufficient_quota\"}}"),
    ]
    for kind, words, line in cases:
        jid = f"job-{kind}"
        feed(jid, [line])
        info = M._key_refusal_reason(jid)
        check(f"{kind}: вид", info.get("kind"), kind)
        check_true(f"{kind}: слова в карточке", words in M._key_refusal_text(jid, KEY))

    # Родовой троттл отказом ключа НЕ является: секундный 429 не повод объявлять
    # ключ мёртвым (та же дисциплина, что у воркера).
    check("родовой троттл не отказ ключа",
          M._scan_key_refusal("job-throttle",
                              "⚠️ [2] HTTP 429: rate limit exceeded, retrying"), None)
    # Протухшая ссылка на скачивание УЖЕ оплаченного кадра — не отказ ключа.
    check("протухший download_url не отказ ключа",
          M._scan_key_refusal("job-url",
                              "❌ [2] v6: не скачался download_url: signed url expired"), None)


def test_account_limit_keeps_its_own_text():
    print("\nУ лимита аккаунта остаётся его выверенный текст, а маска добавляется")
    jid = "job-acc"
    line = ("⛔ ЛИМИТ АККАУНТА ПРОВАЙДЕРА (VEO_BANANA): кончился лимит аккаунта — "
            "смените модель или подождите 20-30 минут.")
    feed(jid, [line])
    limit_msg = M._scan_gen_limit_line(jid, line)
    err = M._worker_error_text(jid, 3, KEY, limit_msg)
    check_true("совет сменить модель сохранён", "смените модель" in err)
    check_true("и видно, КАКОЙ ключ исчерпан", "sk-proj-AAAA…4444" in err)


def test_unknown_failure_still_shows_the_exit_code():
    print("\nНичего не опознано — в карточке остаётся код выхода")
    jid = "job-none"
    feed(jid, ["⚠️ [1] Попытка 1 неудачна (NARWHAL): NARWHAL: v6 генерация не удалась",
               "❌ [1] Все 12 попыток исчерпаны (NARWHAL)"])
    check("причины нет", M._key_refusal_text(jid, KEY), "")
    check("карточка показывает код выхода", M._worker_error_text(jid, 3, KEY), "rc=3")
    check("и обёртка воркера, если она была",
          M._worker_error_text(jid, 3, KEY, "", "card transient"), "card transient")


def test_secret_never_reaches_the_card_or_the_journal():
    print("\nКлюч из тела ответа провайдера не уходит ни в карточку, ни в журнал")
    leaky = ('❌ [1] v6 generation отклонён: HTTP 401: {"error":"invalid key ' + KEY + '"}')

    # Путь первый — стрим логов кабинета. Редактура там уже стоит; скан обязан работать
    # ПОСЛЕ неё, иначе секрет переедет в карточку вместе с причиной.
    clean = M._redact_worker_output(leaky, extra_secrets=[KEY])
    check("редактор вырезал ключ из строки лога", KEY in clean, False)
    jid = "job-leak"
    feed(jid, [clean])
    txt = M._key_refusal_text(jid, KEY)
    check("в карточке ключа нет", KEY in txt, False)
    check_true("но причина названа", "(HTTP 401)" in txt)

    # Путь второй — журнал вызовов. Текст приходит от движка (в том числе с другой
    # машины) и до этой работы ложился в SQLite как есть. Редактура стоит в
    # ЕДИНСТВЕННОМ месте записи колонки, поэтому проверяем именно mark_failed.
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pc.ensure_calls_schema(c)
    row = pc.reserve(c, tenant_id="dev", project_id="p1", kind="images", unit="007",
                     fp=pc.fingerprint(model="NARWHAL", prompt="кот"),
                     provider="recrafter", model="NARWHAL")
    saved = M._calls_redaction_secrets
    M._calls_redaction_secrets = lambda: [KEY]     # ключ кабинета, не передаваемый движком
    try:
        pc.mark_failed(c, row["call_id"], leaky, ordered=False)
    finally:
        M._calls_redaction_secrets = saved
    stored = c.execute("SELECT error, status FROM provider_calls WHERE id=?",
                       (row["call_id"],)).fetchone()
    check("секрета в колонке error нет", KEY in (stored["error"] or ""), False)
    check_true("на его месте метка редактуры", "[REDACTED]" in (stored["error"] or ""))
    check_true("а причина отказа сохранена", "HTTP 401" in (stored["error"] or ""))
    check("состояние строки не изменилось", stored["status"], pc.FAILED)


def test_redaction_list_covers_byok_keys():
    print("\nВ список редактуры попадают и свои ключи юзеров, не только серверные")
    own = "byok-ZZZZ999988887777666655554444"
    c = M.db()
    try:
        c.execute("INSERT INTO users(id, name, token, role, created, keys) "
                  "VALUES(?,?,?,?,?,?)",
                  (4242, "партнёр", "tok-4242", "user", "2026-09-19",
                   '{"googler": "%s"}' % own))
        c.commit()
    finally:
        c.close()
    M._CALLS_SECRETS["at"] = 0.0          # кэш 10 с — для теста сбрасываем
    values = M._calls_redaction_secrets()
    check_true("свой ключ партнёра виден редактору", own in values)
    check("и он действительно вырезается",
          own in M._redact_calls_error("HTTP 401: bad key " + own), False)


def test_redactor_failure_hides_the_text():
    print("\nСломанный редактор прячет текст, а не пропускает его сырым")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pc.ensure_calls_schema(c)
    row = pc.reserve(c, tenant_id="dev", project_id="p2", kind="images", unit="001",
                     fp=pc.fingerprint(model="X", prompt="y"))
    saved = pc._ERROR_REDACTOR

    def boom(_text):
        raise RuntimeError("редактор упал")

    pc.set_error_redactor(boom)
    try:
        pc.mark_failed(c, row["call_id"], "HTTP 401 " + KEY, ordered=False)
    finally:
        pc.set_error_redactor(saved)
    stored = c.execute("SELECT error FROM provider_calls WHERE id=?",
                       (row["call_id"],)).fetchone()["error"]
    check("сырой текст в базу не попал", KEY in stored, False)
    check_true("строка журнала осталась и объясняет пропажу", "редактура" in stored)


def test_foreign_key_lines_do_not_borrow_our_mask():
    """Чужой ключ не записывается как отказ НАШЕГО ключа.

    Дефект был зеркальным задуманному: `_KEY_QUOTA_WORDS` взяты из списка воркера про
    КАНАЛ ПРОМПТОВ (_LLM_EXHAUSTED_PATTERNS), поэтому строка «Канал промптов «claude»
    недоступен (исчерпан счёт/лимит: You've hit your usage limit)» и квота YouTube Data
    API приезжали в карточку как отказ ключа генерации, да ещё с ЕГО маской. Владелец
    шёл менять невиновный ключ, а настоящая причина место уже не получала — «первая
    побеждает».
    """
    print("\nСтрока про чужой ключ не становится отказом ключа задачи")
    jid = "job-foreign"
    M._clear_key_refusal(jid)
    boevaya = ("⚠️ Канал промптов «claude» недоступен (исчерпан счёт/лимит: "
               "You've hit your usage limit · resets 2:10pm)")
    check("канал промптов — не наш ключ", M._scan_key_refusal(jid, boevaya), None)
    # Та же беда со стороны исключения воркера: в его тексте есть и «HTTP 429», и
    # слова про исчерпанный счёт — то есть все признаки отказа ключа, кроме одного:
    # ключ там ЧУЖОЙ (googler_worker.py:388).
    check("исключение канала промптов — тоже не наш ключ",
          M._scan_key_refusal(
              jid, "❌ OpenAIKeyError: У канала промптов «claude» исчерпан счёт/лимит "
                   "(HTTP 429): You've hit your usage limit — останавливаюсь"), None)
    check("квота YouTube Data API — тоже не наш ключ",
          M._scan_key_refusal(
              jid, "⛔ YouTube Data API: HTTP 403 quotaExceeded — суточная квота"), None)
    check("и место осталось свободным", M._key_refusal_text(jid, KEY), "")

    # И теперь настоящая причина ДОЕЗЖАЕТ — до правки она не записалась бы никогда.
    M._scan_key_refusal(jid, '❌ [3] v6 generation отклонён: HTTP 401: {"error":"api key revoked"}')
    check("настоящий отказ ключа записан",
          M._key_refusal_reason(jid).get("kind"), "expired")
    check_true("и в карточке именно он",
               "api key revoked" in M._key_refusal_text(jid, KEY))


def test_quota_needs_the_providers_code():
    """«Квота» без кода ответа — это чаще чужие слова, чем отказ нашего ключа."""
    print("\nВид «исчерпан счёт» опознаётся только вместе с кодом 402/429")
    check("голые слова про квоту без кода — не вердикт",
          M._scan_key_refusal("job-q1", "❌ сцена 3: exceeded your current quota"), None)
    M._clear_key_refusal("job-q2")
    M._scan_key_refusal("job-q2", '❌ сцена 3: HTTP 402: {"error":"no credits remaining"}')
    check("а с кодом — вердикт", M._key_refusal_reason("job-q2").get("kind"), "quota")


def test_blinking_refusal_is_not_a_verdict():
    """Ключ, которым через секунду оплачен кадр, не объявляется отозванным навсегда.

    401 добавлен в список «без ретрая» правильно, но в паре с правилом «первая
    побеждает» ОДИН мигнувший отказ становился окончательным вердиктом. Доказательство
    обратного — успешный ПЛАТНЫЙ вызов тем же ключом: после него место освобождается,
    и настоящая причина, пришедшая позже, наконец записывается.
    """
    print("\nМигнувший 401 не хоронит живой ключ")
    jid = "job-blink"
    feed(jid, [
        '❌ [1] v6 generation отклонён: HTTP 401: {"error":"temporary auth glitch"}',
        "✅ [1] Изображение через NARWHAL (v6)",
    ])
    check("после успешного платного кадра вердикта нет",
          M._key_refusal_text(jid, KEY), "")
    M._scan_key_refusal(
        jid, "⛔ [5] ЛИМИТ АККАУНТА ПРОВАЙДЕРА (VEO_BANANA): кончился лимит аккаунта")
    check("а настоящая причина, пришедшая позже, записалась",
          M._key_refusal_reason(jid).get("kind"), "account_limit")


def test_rc0_branch_puts_the_reason_before_the_last_line():
    """Ветка rc=0: успех по коду, но без результата.

    `_judge_success` отдаёт сюда ПОСЛЕДНЮЮ ❌-строку лога, а ключ называет себя в
    первой. Композиция `_key_refusal_text(...) or why` — это и есть место подключения;
    убрать её, и в карточку вернётся следствие вместо причины.
    """
    print("\nrc=0 без результата: в карточке причина, а не последняя строка")
    jid = "job-rc0"
    feed(jid, ['❌ [1] v6 generation отклонён: HTTP 401: {"error":"api key expired"}',
               "❌ [7] Все 12 попыток исчерпаны (NARWHAL)"])
    why = "Все 12 попыток исчерпаны (NARWHAL)"     # то, что вернул бы _judge_success
    composed = M._key_refusal_text(jid, KEY) or why
    check_true("в карточке причина ключа", composed.startswith("ключ sk-proj-AAAA…4444:"))
    check("а следствие уступило место", composed == why, False)


def test_truncated_secret_is_cut_too():
    """Ключ, обрезанный по дороге, не оседает в базе огрызком.

    Текст отказа режут четверо (воркер [:300], call_journal [:500], pub_calls_api [:500],
    pub_services [:400]). Ключ, попавший на границу любой из обрезок, приезжает началом
    самого себя, и точная редактура по значению его уже не находит.
    """
    print("\nОбрезанный по дороге ключ тоже вырезается")
    cut = KEY[:28]                                  # ровно то, что остаётся после [:N]
    line = "HTTP 401: {\"error\":\"invalid key " + cut + "\"}"
    clean = M._redact_worker_output(line, extra_secrets=[KEY])
    check("огрызка ключа в тексте нет", cut in clean, False)
    check_true("метка редактуры на месте", "[REDACTED]" in clean)
    check_true("а причина отказа сохранена", "HTTP 401" in clean)
    short = "смените модель"                        # обычный текст не трогаем
    check("осмысленный текст не режется",
          M._redact_worker_output(short, extra_secrets=[KEY]), short)


def test_journal_redacts_without_the_cabinet():
    """Журнал не должен течь только потому, что кабинет не импортирован.

    Редактор ставится ПОБОЧНЫМ ЭФФЕКТОМ импорта server/main.py. Отдельный процесс
    обслуживания, тест или будущий сервис, поднявший pub_services без кабинета, писал
    бы сырой ответ провайдера в SQLite — ровно то, что правка закрывает.
    """
    print("\nБез кабинета журнал режет секрет сам — по образцу")
    saved = pc._ERROR_REDACTOR
    pc.set_error_redactor(None)                     # «pub_calls импортировали одиноко»
    try:
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        pc.ensure_calls_schema(c)
        row = pc.reserve(c, tenant_id="dev", project_id="p3", kind="images", unit="002",
                         fp=pc.fingerprint(model="X", prompt="z"))
        pc.mark_failed(c, row["call_id"],
                       'HTTP 401: {"error":"invalid key ' + KEY + '"}', ordered=False)
        stored = c.execute("SELECT error FROM provider_calls WHERE id=?",
                           (row["call_id"],)).fetchone()["error"]
    finally:
        pc.set_error_redactor(saved)
    check("ключа в колонке нет", KEY in stored, False)
    check_true("метка редактуры на месте", "[REDACTED]" in stored)
    check_true("причина отказа сохранена", "HTTP 401" in stored)


def main() -> int:
    try:
        test_first_reason_wins()
        test_recovery_lines_do_not_take_the_slot()
        test_foreign_key_lines_do_not_borrow_our_mask()
        test_quota_needs_the_providers_code()
        test_blinking_refusal_is_not_a_verdict()
        test_rc0_branch_puts_the_reason_before_the_last_line()
        test_four_kinds_are_told_apart()
        test_account_limit_keeps_its_own_text()
        test_unknown_failure_still_shows_the_exit_code()
        test_secret_never_reaches_the_card_or_the_journal()
        test_redaction_list_covers_byok_keys()
        test_truncated_secret_is_cut_too()
        test_journal_redacts_without_the_cabinet()
        test_redactor_failure_hides_the_text()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
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
