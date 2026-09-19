#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ключ повтора заказа рерайта: «тот же запрос» против «409 навсегда» (19.09.2026).

Дефект появляется в момент ребейза форка рерайта с 31.08 на боевой 18.09 (задача B7).

До ребейза наш форк заголовок `Idempotency-Key` просто не замечал, и кабинет слал
`release:<release_id>` в никуда. Бой 18.09 его ПРОВЕРЯЕТ (`job_idempotency.identity`
+ `JobQueue.lookup_create`), и правило у него жёсткое:

    тот же ключ + то же тело      → вернуть ТУ ЖЕ задачу (второй раз не платим)
    тот же ключ + ДРУГОЕ тело     → 409 «ключ уже использован с другими параметрами»
    тот же ключ, задача удалена   → 410

Второе и есть ловушка. Выпуск живёт неделями, а тело заказа за это время меняется
от вещей, которых никто не считает новым заказом: поправили размер куска или число
потоков в том же пресете, переименовали канал, перекачали субтитры донора, сосед
подсказал другую модель на пустое `model` в пресете. С ключом `release:<id>` каждый
такой случай — 409 НАВСЕГДА: выпуск краснеет и сам уже не чинится, потому что ключ
не меняется никогда.

Решение: ключ = выпуск + отпечаток РОВНО ТОГО, что сосед сравнивает, то есть тела
заказа с `files` по содержимому донора (`pub_remote._order_digest`). Брать сюда
`pub_calls.fingerprint` нельзя, и это проверяется ниже отдельно: он намеренно считает
по ИМЕНИ пресета и модели ИЗ пресета — он отвечает на вопрос «за это уже платили?»,
а не «это тот же самый запрос?».

Главная проверка в конце — не про отдельные поля, а про само свойство: ни одна пара
заказов не даёт «наш ключ совпал, а тело соседа разное». Считается это настоящим
кодом соседа, а не его пересказом.

Сети здесь нет: `pub_remote._call` подменён.

    .venv/bin/python tests/test_rewrite_order_key.py
"""
from __future__ import annotations

import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))
# rewrite/ и shared/ — чтобы спрашивать НАСТОЯЩИЙ код соседа, а не его пересказ.
# shared/ обязателен: боевой rewrite_core импортирует credential_budget без try.
sys.path.insert(0, os.path.join(BASE, "rewrite"))
sys.path.insert(0, os.path.join(BASE, "shared"))

import pub_calls                                                  # noqa: E402
import pub_remote                                                 # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# ───────────────────────────── сосед на бумажке ─────────────────────────────

class Resp:
    status_code = 200

    def json(self):
        return {"job_id": "job-1"}


SENT = []          # [(headers, body)] — что кабинет реально отправил


def fake_call(method, url, key, service, **kw):
    SENT.append((kw.get("headers") or {}, kw.get("json") or {}))
    return Resp()


PRESET = {"model": "", "chunk": 3500, "threads": 1, "address": "vy",
          "preserve_paragraphs": True, "safe_text": False,
          "languages": [{"code": "pl", "channel": "Демо", "host": "Иван"}]}

DONOR = "Текст донора. " * 100


def order(**over):
    """Один заказ рерайта: вернуть (ключ повтора, тело запроса, текст донора).

    Умолчания — обычный заказ одного выпуска. Каждый именованный аргумент меняет
    ровно одно обстоятельство, чтобы в проверке было видно, что именно поехало.
    """
    preset = dict(PRESET, **(over.get("preset") or {}))
    text = over.get("text", DONOR)
    SENT.clear()
    client = pub_remote.RewriteClient("k")
    client.create_job(
        over.get("file_id", "file-1"),
        over.get("lang", "pl"),
        preset,
        channel_name=over.get("channel", "Демо"),
        idempotency_key=f'release:{over.get("release", "rel-1")}',
        model=over.get("model", "terra"),
        source_sha=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    headers, body = SENT[-1]
    return headers.get("Idempotency-Key", ""), body, text


# ──────────────────────────── настоящий код соседа ────────────────────────────

class FakeRequest:
    def __init__(self, key):
        self.headers = {"Idempotency-Key": key}


def neighbour_intent(key, body, text):
    """Что сосед запомнит под этим ключом: (scope, хэш ключа, хэш тела).

    Повторяет `api.create_job` боевого рерайта строка в строку: тело проходит через
    ту же модель pydantic, `files` подменяется хэшем СОДЕРЖИМОГО (у соседа это
    `file_hash`, у нас — sha256 того же текста), и всё это уходит в `identity`.
    """
    import api as rewrite_api
    import job_idempotency

    parsed = rewrite_api.ApiJobCreate(**body).model_dump()
    parsed["files"] = [hashlib.sha256(text.encode("utf-8")).hexdigest()]
    return job_idempotency.identity(FakeRequest(key), parsed, "api-rewrite")


def main() -> int:
    saved_call = pub_remote._call
    pub_remote._call = fake_call
    try:
        print("Тот же заказ — тот же ключ: второй раз не платим")
        key_a, body_a, _ = order()
        key_b, body_b, _ = order()
        check("ключ повторяется", key_a, key_b)
        check("и тело то же", body_a, body_b)
        check("выпуск в ключе виден человеком", key_a.startswith("release:rel-1:"), True)

        print("\nНовая загрузка того же текста ключ НЕ меняет")
        # `upload_text` выдаёт новый file_id на каждый тик. Если бы ключ считался от
        # file_id, защита от второй оплаты не сработала бы ни разу — а именно ради
        # неё всё это и делается.
        key_other_file = order(file_id="file-999")[0]
        check("ключ тот же", key_other_file, key_a)

        print("\nДругой донор — другой заказ, а не 409")
        key_text = order(text=DONOR + "и ещё абзац")[0]
        check("ключ другой", key_text != key_a, True)

        print("\nПравка пресета НА МЕСТЕ — другой заказ, а не 409")
        # Здесь и видно, почему pub_calls.fingerprint в ключ не годится: имя пресета
        # и модель из пресета не изменились, значит отпечаток оплаты тот же — он
        # и должен быть тем же, он про деньги. А тело заказа изменилось.
        key_chunk, body_chunk, _ = order(preset={"chunk": 2000})
        check("ключ другой", key_chunk != key_a, True)
        check("а тело и правда поехало",
              (body_a["chunk_size"], body_chunk["chunk_size"]), (3500, 2000))
        # А теперь то же самое ключом «по отпечатку оплаты», как предлагалось сначала.
        # Отпечаток считается от текста, языка, ИМЕНИ пресета и модели ИЗ пресета —
        # размера куска среди них нет, и быть не должно: иначе правка настройки
        # объявила бы новыми все уже оплаченные строки. Но как ключ повтора он тогда
        # даёт ровно запрещённое: тот же ключ на другом теле, то есть 409 навсегда.
        def by_fingerprint(preset):
            """Ключ, если бы его считали отпечатком оплаты, — из ТОГО ЖЕ пресета,
            которым сделан заказ. Аргумент здесь настоящий, а не декоративный: ровно
            так и видно, что отпечаток правку внутри пресета не замечает."""
            return "release:rel-1:" + pub_calls.fingerprint(
                text=hashlib.sha256(DONOR.encode("utf-8")).hexdigest(),
                lang="pl", preset="Базовый", model=(preset or {}).get("model") or "")

        # Два РАЗНЫХ пресета (в одном кусок 3500, в другом 2000) под одним именем.
        check("отпечаток оплаты на обоих телах один и тот же",
              by_fingerprint(PRESET), by_fingerprint(dict(PRESET, chunk=2000)))
        # И это не «оба пустые»: тела у соседа действительно разные.
        check("а сосед видит два разных заказа",
              neighbour_intent(key_a, body_a, DONOR)[2]
              != neighbour_intent(key_chunk, body_chunk, DONOR)[2], True)
        check("а наш ключ разницу заметил", key_chunk != key_a, True)

        print("\nПереименовали канал — другой заказ, а не 409")
        key_ch = order(channel="Демо-2")[0]
        check("ключ другой", key_ch != key_a, True)

        print("\nСосед подсказал другую модель — другой заказ, а не 409")
        key_model = order(model="luna")[0]
        check("ключ другой", key_model != key_a, True)

        print("\nДругой язык и другой выпуск — тоже другие заказы")
        check("язык", order(lang="cs")[0] != key_a, True)
        check("выпуск", order(release="rel-2")[0] != key_a, True)

        print("\nКлюч без отпечатка исходника — громкий отказ до обращения к соседу")
        # Молча ослабить ключ было бы хуже всего: он остался бы прежним при смене
        # донора, то есть вернул бы ровно тот 409-навсегда, от которого мы уходим.
        SENT.clear()
        try:
            pub_remote.RewriteClient("k").create_job(
                "file-1", "pl", PRESET, idempotency_key="release:rel-1",
                model="terra")
            check("отказ", "не отказал", "ValueError")
        except ValueError as exc:
            check("отказ", "ValueError", "ValueError")
            check("сказано про отпечаток исходника", "отпечат" in str(exc), True)
        check("к соседу при этом не ходили", len(SENT), 0)

        print("\nГраницы зажима взяты у соседа, а не придуманы")
        # Наш ключ обязан схлопывать ровно то же, что схлопывает сосед. Числа поэтому
        # не сверяются с литералами в тесте — они сверяются с САМИМ `rewrite/api.py`.
        # Поменяет сосед границы и забудет про нас — падать будет здесь, а не деньгами
        # на второй задаче.
        import api as rewrite_api
        check("потолок потоков — соседский",
              pub_remote.ORDER_THREADS_MAX, rewrite_api.API_MAX_THREADS_PER_JOB)
        probe = rewrite_api.ApiJobCreate(files=["f"], languages=[{"code": "pl"}],
                                         chunk_size=10 ** 9, max_threads=10 ** 9)
        check("верхняя граница куска — соседская",
              pub_remote.ORDER_CHUNK_MAX, probe.chunk_size)
        probe_low = rewrite_api.ApiJobCreate(files=["f"], languages=[{"code": "pl"}],
                                             chunk_size=1, max_threads=1)
        check("нижняя граница куска — соседская",
              pub_remote.ORDER_CHUNK_MIN, probe_low.chunk_size)

        print("\nКлюч влезает в правила соседа (1..160 видимых ASCII)")
        check("длина", len(key_a) <= 160, True)
        check("только видимый ASCII",
              key_a.isascii() and all(33 <= ord(c) <= 126 for c in key_a), True)

        print("\nГлавное: «тот же ключ с другим телом» не получается ни на чём")
        # Свойство, ради которого всё написано. Считаем НАСТОЯЩИМ кодом соседа:
        # если у двух заказов совпал хэш ключа, обязан совпасть и хэш тела, иначе
        # сосед ответит 409 и выпуск встанет навсегда.
        variants = {
            "как есть": order(),
            "новый file_id": order(file_id="file-777"),
            "другой донор": order(text=DONOR + "хвост"),
            "куски поменьше": order(preset={"chunk": 2000}),
            "больше потоков": order(preset={"threads": 4}),
            # Три пары ниже — про то, что сосед СХЛОПЫВАЕТ. Форма пульта разрешает
            # 1..8 потоков, сосед режет до ORDER_THREADS_MAX; кусок он зажимает в
            # 500..20000; пробелы у строк обрезает молча. Для него это один и тот же
            # заказ, значит и ключ обязан быть один: иначе после потерянного ответа
            # на POST он создаст ВТОРУЮ ОПЛАЧЕННУЮ задачу вместо возврата первой.
            "потоки 4 (сосед зажмёт)": order(preset={"threads": 4}),
            "потоки 8 (сосед зажмёт туда же)": order(preset={"threads": 8}),
            "кусок 25000 (выше потолка)": order(preset={"chunk": 25000}),
            "кусок 30000 (тот же потолок)": order(preset={"chunk": 30000}),
            "ведущий «Иван»": order(preset={"languages": [
                {"code": "pl", "channel": "Демо", "host": "Иван"}]}),
            "ведущий «Иван » с пробелом": order(preset={"languages": [
                {"code": "pl", "channel": "Демо", "host": "Иван "}]}),
            "другое обращение": order(preset={"address": "ty"}),
            "бережный текст": order(preset={"safe_text": True}),
            "абзацы не беречь": order(preset={"preserve_paragraphs": False}),
            "другой ведущий": order(preset={"languages": [
                {"code": "pl", "channel": "Демо", "host": "Пётр"}]}),
            "канал переименован": order(channel="Демо-2"),
            "другая модель": order(model="luna"),
            "другой язык": order(lang="cs"),
            "другой выпуск": order(release="rel-2"),
        }
        intents = {name: neighbour_intent(key, body, text)
                   for name, (key, body, text) in variants.items()}
        clashes = []
        names = sorted(intents)
        for i, one in enumerate(names):
            for two in names[i + 1:]:
                a, b = intents[one], intents[two]
                if a[1] == b[1] and a[2] != b[2]:
                    clashes.append(f"{one} ↔ {two}")
        check("пар «ключ тот же, тело другое» нет", clashes, [])

        # И обратно, но только ВНУТРИ одного выпуска: одинаковое тело обязано дать
        # одинаковый ключ, иначе идемпотентность не сработает и повторный тик оплатит
        # заказ второй раз. Между разными выпусками совпадение тел — наоборот, норма:
        # два выпуска на одном доноре это два заказа, и каждому свой ключ.
        misses = []
        same_release = [n for n in names if n != "другой выпуск"]
        for i, one in enumerate(same_release):
            for two in same_release[i + 1:]:
                a, b = intents[one], intents[two]
                if a[2] == b[2] and a[1] != b[1]:
                    misses.append(f"{one} ↔ {two}")
        check("внутри выпуска пар «тело то же, ключ другой» нет", misses, [])
        other = intents["другой выпуск"]
        check("а другому выпуску на том же теле положен свой ключ",
              (other[2] == intents["как есть"][2], other[1] != intents["как есть"][1]),
              (True, True))
        check("вариантов проверено", len(variants), 19)
    finally:
        pub_remote._call = saved_call

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
