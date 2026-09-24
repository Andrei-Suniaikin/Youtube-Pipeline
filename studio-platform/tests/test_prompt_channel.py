#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Канал промптов для картинок в «Ключах» пульта + общая модель текста по умолчанию
(пункт 4 ТЗ, задача subscriptions-2026-09-22).

Пульт получает тот же выбор канала промптов, что уже есть у кабинета в «Мои ключи»
(OpenAI / Claude-подписка / Codex-подписка / альтернативный Claude API), и четыре
места, где раньше был зашит литерал `"gpt-4.1-nano"`, теперь читают модель по
умолчанию у Rewrite (`GET /models`, флаг `default`) через одну общую функцию
`_default_text_model`. Кабинетный обработчик `/api/my/keys/web` и сама проверка
credential'ов — чужая сторона (`server/main.py`), здесь не трогается и не дублируется;
проверяется только то, что пульт честно читает и честно пробрасывает.

Проверяется:
  * GET /ui/keys отдаёт `prompt`: выбранный канал и «задан/не задан» по каждому из
    четырёх каналов — без единого значения ключа в ответе (AC-14);
  * POST /ui/keys с `prompt.*` пробрасывает те же поля формой в кабинетный
    /api/my/keys/web — без второй проверки на стороне пульта (AC-15);
  * `_default_text_model` — общая модель по умолчанию: берёт `default`-модель из
    /models, откатывается на литерал при отказе/пустом списке/отсутствии флага;
  * три из четырёх мест используют именно эту функцию, а не свой литерал (AC-17/18):
    `ui_text_create` (режимы topic и text) и форма/заведение пресета рерайта.
    Четвёртое место — `_batch_text_plan` — проверяется в `test_batch_text_cover.py`
    (там же живёт вся фикстура платных пачек; заводить вторую было бы дублем);
  * ревью 22.09.2026, Issue 3: `prompt.claude_oauth` нормализуется `normalize_claude_token`
    ДО пересылки в кабинет — ключ API Anthropic и мусор отклоняются без единого
    сетевого вызова, тем же приёмом, что уже стоит у `/ui/keys/rewrite`;
  * ревью 22.09.2026, Issue 4: `own_set`/`selected` считает кабинет через
    `sys.modules.get("main")` (`_own_prompt_channels`/`_selected_prompt_channel`),
    а не своя вторая нумерация на пульте — копия серверного ключа, «мусорный»
    codex_auth и устаревший выбор канала это ловят.

Ни сети, ни стенда: `_rw` и HTTP-вызов к кабинету (`urllib.request.urlopen`)
подменены, база — своя копия `dev/vsgen.dev.db` во временном каталоге, после себя
тест её убирает. Секция про Issue 4 загружает настоящий `server/main.py` в процесс
(как `tests/test_own_key_pool.py`) — только чтение файла ключей сервера, БД не трогает.

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_prompt_channel.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
SEED = os.path.join(BASE, "dev", "vsgen.dev.db")

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakeReq:
    """Запрос ровно в том объёме, в каком его читают ручки: кука и `state.user`
    (полная строка `users`, как её кладёт `web/pult_auth.who` — см. `web/app.py:160`)."""

    def __init__(self, sid="sid-1", user=None):
        self.state = type("St", (), {"sid": sid, "user": user})()


class FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeRW:
    """Подменяет `_rw`: `/models` — список Rewrite (что и проверяется здесь),
    остальные пути — минимальные честные ответы, без которых вызываемые ручки
    не могут отработать (загрузка файла, чтение/запись пресетов, заказ)."""

    def __init__(self, models=None, models_code=200, models_boom=False):
        self.calls = []
        self.models = models if models is not None else []
        self.models_code = models_code
        self.models_boom = models_boom
        self.job = 0

    def __call__(self, path, sid, payload=None, method=None, raw=None, ctype=None,
                extra=None):
        self.calls.append((path, sid, payload))
        if path == "/models":
            if self.models_boom:
                raise RuntimeError("рерайт не отвечает")
            return self.models_code, self.models
        if path == "/upload":
            return 200, {"filename": "stored.txt"}
        if path == "/settings/keys":
            return 200, {"rew_presets": {}}
        if path.startswith("/jobs/"):
            self.job += 1
            return 200, {"job_id": f"rw-{self.job}"}
        return 200, {"ok": True}


def main() -> int:
    if not os.path.exists(SEED):
        print(f"НЕ ПРОВЕРЕНО: нет базы-образца {SEED}")
        return 1
    work = tempfile.mkdtemp(prefix="prompt-channel-")
    try:
        db_path = os.path.join(work, "vsgen.db")
        shutil.copy(SEED, db_path)
        os.environ["VS_DEV_DB"] = db_path
        os.environ["VS_TENANTS_DIR"] = os.path.join(work, "tenants")
        os.environ["VS_DEV_TENANT"] = "dev"
        sys.path.insert(0, BASE)
        sys.path.insert(0, os.path.join(BASE, "server"))
        import urllib.request as real_urlreq                         # noqa: E402
        import web.app as pult                                       # noqa: E402
        from fastapi import HTTPException                            # noqa: E402

        DEFAULT_MODEL = "codex:gpt-5.6-terra"
        MODELS_WITH_DEFAULT = [
            {"value": "gpt-4o-mini", "label": "GPT-4o Mini"},
            {"value": DEFAULT_MODEL, "label": "gpt-5.6-terra — Codex", "default": True},
        ]
        # Настоящая (не подменённая) `_rw` — для структурной проверки в самом конце:
        # по ходу теста `pult._rw` многократно подменяется на `FakeRW(...)`, и к концу
        # файла `pult._rw` — это уже не функция модуля, а объект с другим `__name__`.
        real_rw_fn = pult._rw

        # ── GET /ui/keys: раздел prompt (AC-14) ──────────────────────────────
        # `main` (кабинет) сюда ещё не загружен ни этим процессом, ни `web.app`
        # (тот делает `sys.modules.get("main")`, а не `import main`) — секции ниже,
        # ДО явного `import main as cabinet` (в самом конце файла), намеренно бьют
        # по запасному пути `_prompt_state` (прямое чтение `keys`/`key_opts`), а не
        # заходят туда случайно из-за порядка импортов где-то ещё в процессе.
        check("main ещё не загружен — секции ниже идут запасным путём `_prompt_state`",
              sys.modules.get("main"), None)
        print("GET /ui/keys — раздел prompt")
        pult._rw = FakeRW()
        secret_claude = "sk-ant-oat01-" + "z" * 40
        user_row = {
            "id": 1, "name": "тест", "role": "user", "tenant_id": "dev",
            "keys": json.dumps({"claude_oauth": secret_claude, "openai": "sk-proj-xxxx"}),
            "key_opts": json.dumps({"_prompt": {"provider": "claude"}}),
        }
        out = pult.ui_keys(FakeReq(user=user_row))
        # Мутация: убрать `selected` из `_prompt_state` (вернуть "" всегда) — эта
        # проверка падает первой.
        check("канал выбран — claude", out["prompt"]["selected"], "claude")
        check("claude own_set", out["prompt"]["channels"]["claude"]["own_set"], True)
        check("openai own_set (общее семейство с обычным ключом)",
              out["prompt"]["channels"]["openai"]["own_set"], True)
        check("codex own_set — не задан", out["prompt"]["channels"]["codex"]["own_set"],
              False)
        check("altclaude own_set — не задан",
              out["prompt"]["channels"]["altclaude"]["own_set"], False)
        # Мутация: в `_prompt_state` вернуть `own` целиком вместо только `own_set` —
        # эта проверка ловит утечку значения.
        dump = json.dumps(out, ensure_ascii=False, default=str)
        check("секрет claude не утёк в ответ /ui/keys", secret_claude in dump, False)
        check("ключ openai не утёк в ответ /ui/keys", "sk-proj-xxxx" in dump, False)

        print("GET /ui/keys — битые keys/key_opts не роняют ручку, канал — «Авто»")
        broken_user = {"id": 2, "name": "т2", "role": "user", "tenant_id": "dev",
                       "keys": "не json", "key_opts": None}
        out2 = pult.ui_keys(FakeReq(user=broken_user))
        check("канал по умолчанию — Авто", out2["prompt"]["selected"], "")
        check("ни один канал не задан",
              any(v["own_set"] for v in out2["prompt"]["channels"].values()), False)

        print("GET /ui/keys — неизвестное значение канала в key_opts не проходит наружу")
        weird_user = {"id": 3, "name": "т3", "role": "user", "tenant_id": "dev",
                      "keys": "{}", "key_opts": json.dumps({"_prompt": {"provider": "чужое"}})}
        out3 = pult.ui_keys(FakeReq(user=weird_user))
        # Мутация: убрать проверку `selected not in _PROMPT_CHANNELS` — эта падает.
        check("неизвестный канал не отдаётся как есть", out3["prompt"]["selected"], "")

        print("GET /ui/keys — узнаваемый секрет во ВСЕХ четырёх семействах не утекает")
        # AC-14 буквально требует проверки по всем семействам, не только по двум:
        # codex_auth/alt_llm устроены не строкой-паролем (codex — материализованный
        # auth.json, alt — ключ + база + модель), и утечка именно там незаметнее всего,
        # потому что глазами их реже перечитывают в диффе ответа.
        secret_codex = "codex-secret-token-" + "y" * 30
        secret_alt = "alt-secret-token-" + "w" * 30
        secret_openai2 = "sk-proj-leak-" + "q" * 30
        secret_claude2 = "sk-ant-oat01-leak-" + "e" * 30
        all_families_user = {
            "id": 4, "name": "т4", "role": "user", "tenant_id": "dev",
            "keys": json.dumps({"claude_oauth": secret_claude2, "codex_auth": secret_codex,
                                "openai": secret_openai2, "alt_llm": secret_alt}),
            "key_opts": json.dumps({"_prompt": {"provider": "codex"}}),
        }
        out_all = pult.ui_keys(FakeReq(user=all_families_user))
        check("все четыре канала — own_set=True",
              [out_all["prompt"]["channels"][ch]["own_set"]
               for ch in ("openai", "claude", "codex", "altclaude")],
              [True, True, True, True])
        dump_all = json.dumps(out_all, ensure_ascii=False, default=str)
        # Мутация: в `_prompt_state` заменить `bool([v for v in items if v])` на сам
        # `val`/`items` (отдать значение как есть) — любая из четырёх проверок ниже ловит.
        for secret in (secret_codex, secret_alt, secret_openai2, secret_claude2):
            check(f"секрет «{secret[:14]}…» не утёк в ответ /ui/keys",
                  secret in dump_all, False)

        # ── POST /ui/keys: прокидывает prompt.* в кабинет (AC-15) ────────────
        print("POST /ui/keys — prompt.* уходит в кабинетный /api/my/keys/web")
        sent = {}

        def fake_urlopen_ok(req, timeout=None):
            sent["url"] = req.full_url
            sent["data"] = dict(urllib.parse.parse_qsl(req.data.decode("utf-8")))
            sent["headers"] = dict(req.header_items())
            return FakeResp(json.dumps({"ok": True}).encode("utf-8"))

        real_urlreq.urlopen = fake_urlopen_ok
        out4 = pult.ui_keys_save(
            pult.KeysIn(prompt=pult.PromptChannelIn(
                channel="claude", claude_oauth=secret_claude)),
            FakeReq(sid="sid-42"))
        check("ответ ok", out4, {"ok": True})
        check("ушло на /api/my/keys/web кабинета",
              sent["url"], pult.CABINET_BASE + "/api/my/keys/web")
        # Мутация: не добавлять `prompt_channel`/`claude_oauth` в `fields` при
        # `body.prompt is not None` — обе проверки ниже падают.
        check("канал дошёл тем же именем поля, что и форма кабинета",
              sent["data"].get("prompt_channel"), "claude")
        check("токен дошёл как есть, без изменений",
              sent["data"].get("claude_oauth"), secret_claude)
        check("это fetch-запрос — кабинет отвечает JSON, а не редиректом",
              sent["headers"].get("X-requested-with"), "fetch")

        print("POST /ui/keys — все семь полей prompt.* доходят под своими именами")
        real_urlreq.urlopen = fake_urlopen_ok
        pult.ui_keys_save(
            pult.KeysIn(prompt=pult.PromptChannelIn(
                channel="altclaude", codex_auth="{}", prompt_key_openai="sk-proj-y",
                alt_llm="sk-ant-alt", alt_llm_base="https://x.example/v1",
                alt_llm_model="claude-x")),
            FakeReq())
        check("codex_auth", sent["data"].get("codex_auth"), "{}")
        check("prompt_key_openai", sent["data"].get("prompt_key_openai"), "sk-proj-y")
        check("alt_llm", sent["data"].get("alt_llm"), "sk-ant-alt")
        check("alt_llm_base", sent["data"].get("alt_llm_base"), "https://x.example/v1")
        check("alt_llm_model", sent["data"].get("alt_llm_model"), "claude-x")

        print("POST /ui/keys — bad_prompt_channel кабинета пробрасывается отказом 400")
        def fake_urlopen_bad(req, timeout=None):
            return FakeResp(json.dumps(
                {"ok": False, "bad_prompt_channel": True}).encode("utf-8"))
        real_urlreq.urlopen = fake_urlopen_bad
        try:
            pult.ui_keys_save(pult.KeysIn(prompt=pult.PromptChannelIn(channel="codex")),
                              FakeReq())
            check("недоступный канал отклонён", "прошло без отказа", "HTTPException 400")
        except HTTPException as exc:
            check("код 400 (bad_prompt_channel)", exc.status_code, 400)

        print("POST /ui/keys — пустое тело (ни add/drop, ни prompt) отказывает без сети")
        def fake_urlopen_forbidden(req, timeout=None):
            raise AssertionError("сеть не должна вызываться на пустом теле")
        real_urlreq.urlopen = fake_urlopen_forbidden
        try:
            pult.ui_keys_save(pult.KeysIn(), FakeReq())
            check("пустое тело отклонено", "прошло без отказа", "HTTPException 400")
        except HTTPException as exc:
            check("код 400 (пусто)", exc.status_code, 400)

        print("POST /ui/keys — prompt со всеми полями None (ничего не менять) — тоже отказ")
        # `PromptChannelIn()` без единого поля — тот же пустой заказ, что и `prompt=None`
        # выше, просто с другой стороны конструктора; тоже не должен ходить в сеть.
        try:
            pult.ui_keys_save(pult.KeysIn(prompt=pult.PromptChannelIn()), FakeReq())
            check("prompt из одних None отклонён", "прошло без отказа", "HTTPException 400")
        except HTTPException as exc:
            check("код 400 (prompt из одних None)", exc.status_code, 400)

        print("POST /ui/keys — не заданное явно поле НЕ пробрасывается вовсе (не «»)")
        # Édge-case «пусто vs «-»»: поле, которое клиент не тронул, должно отсутствовать
        # в форме кабинета, а не приезжать пустой строкой — иначе `prompt_channel`
        # (у кабинета отличает "in d" от отсутствия, см. `server/main.py:17805`) сбросил
        # бы выбранный канал на «Авто» ВМЕЗАПНО, хотя человек его не трогал.
        sent2 = {}
        def fake_urlopen_capture(req, timeout=None):
            # `keep_blank_values=True` обязателен: без него `parse_qsl` тихо выбрасывает
            # поля вида `prompt_channel=` (пустое значение) — здесь именно эта форма
            # ("поле есть, но пустое") отличает "не менять"/"сброс" от "поля не было".
            sent2["data"] = dict(urllib.parse.parse_qsl(req.data.decode("utf-8"),
                                                        keep_blank_values=True))
            return FakeResp(json.dumps({"ok": True}).encode("utf-8"))
        real_urlreq.urlopen = fake_urlopen_capture
        # Валидный по форме токен подписки: с "только этот токен" (не похож на
        # sk-ant-oat) с 22.09.2026 сама ручка отказала бы 422 ДО пересылки (Issue 3) —
        # здесь проверяется другое (что НЕ заданные поля не пробрасываются), поэтому
        # значение должно пройти нормализацию, а не упасть на ней.
        only_field_token = "sk-ant-oat01-" + "q" * 40
        pult.ui_keys_save(
            pult.KeysIn(prompt=pult.PromptChannelIn(claude_oauth=only_field_token)),
            FakeReq())
        # Мутация: в `ui_keys_save` заменить `if val is not None` на `if val` (или
        # добавлять поле безусловно) — обе проверки ниже ловят.
        check("канал не тронут — prompt_channel не отправлен вовсе",
              "prompt_channel" in sent2["data"], False)
        check("codex_auth не тронут — не отправлен вовсе",
              "codex_auth" in sent2["data"], False)
        check("claude_oauth — единственное заданное поле — доехало",
              sent2["data"].get("claude_oauth"), only_field_token)

        print("POST /ui/keys — явная пустая строка ДОХОДИТ как пустая строка (не омисс)")
        # У `prompt_channel` пустая строка — это «сбросить на Авто» (кабинет отличает
        # `in d`), у остальных шести — «не менять» тем же кабинетным соглашением
        # (`server/main.py:17701`); пульту в обоих случаях достаточно не проглотить ""
        # и не превратить её молча в омисс. Именно оба поля пустыми, не третьего
        # непустого рядом: мутация `if val is not None` → `if val` (пустая строка —
        # ложна в Python) превращает оба в омисс, `fields` пустеет, и запрос ловит
        # СВОЙ ЖЕ отказ «нечего сохранять» — это и есть сигнал мутации, а не сеть.
        try:
            pult.ui_keys_save(
                pult.KeysIn(prompt=pult.PromptChannelIn(channel="", claude_oauth="")),
                FakeReq())
        except HTTPException as exc:
            check("явная пустая строка (сброс канала + «не менять» claude) — не 400",
                  f"{exc.status_code}: {exc.detail}", "запрос должен был пройти")
        else:
            # Мутация: в `ui_keys_save` заменить `if val is not None` на `if val` —
            # обе проверки ниже ловят прямо (без падения теста трейсбеком).
            check("явная пустая строка канала доехала как пустая строка (сброс на «Авто»)",
                  sent2["data"].get("prompt_channel"), "")
            check("явная пустая строка claude_oauth доехала как пустая строка («не менять»)",
                  sent2["data"].get("claude_oauth"), "")

        print("POST /ui/keys — «-» доходит буквально во всех шести credential-полях")
        pult.ui_keys_save(pult.KeysIn(prompt=pult.PromptChannelIn(
            claude_oauth="-", codex_auth="-", prompt_key_openai="-",
            alt_llm="-", alt_llm_base="-", alt_llm_model="-")), FakeReq())
        # Мутация: заменить пересылаемое значение "-" на "" где угодно в цикле полей —
        # эта проверка ловит (кабинет отличает "-"=очистить от ""=не менять, и подмена
        # молча отменила бы очистку).
        for fname in ("claude_oauth", "codex_auth", "prompt_key_openai",
                     "alt_llm", "alt_llm_base", "alt_llm_model"):
            check(f"{fname}='-' доехало буквально", sent2["data"].get(fname), "-")

        print("POST /ui/keys — пустые alt_llm_base/alt_llm_model НЕ пересылаются "
              "(кабинет понял бы «поле есть, но пусто» как «очистить», а не «не менять»)")
        # Ревью, итерация 2, Issue 1: у ВСЕХ остальных полей "" доходит буквально
        # (кабинетное соглашение «пусто = не менять» разбирает сам кабинет — проверки
        # выше). Но `alt_llm_base`/`alt_llm_model` — исключение: кабинет
        # (`server/main.py:17783-17788`) для этих двух полей читает «поле пришло
        # пустым» как «очистить», а не «не менять». Поэтому пульт сам отсекает их
        # ДО пересылки, если после `strip()` пусто, вместо того чтобы полагаться на
        # кабинетное соглашение, которое здесь не действует.
        sent3 = {}
        def fake_urlopen_capture3(req, timeout=None):
            sent3["data"] = dict(urllib.parse.parse_qsl(req.data.decode("utf-8"),
                                                        keep_blank_values=True))
            return FakeResp(json.dumps({"ok": True}).encode("utf-8"))
        real_urlreq.urlopen = fake_urlopen_capture3
        pult.ui_keys_save(
            pult.KeysIn(prompt=pult.PromptChannelIn(
                alt_llm="sk-ant-alt-kept", alt_llm_base="", alt_llm_model="  ")),
            FakeReq())
        # Мутация: в `ui_keys_save` убрать отдельный отсев для `alt_llm_base`/
        # `alt_llm_model` (пересылать их по общему правилу `if val is not None`,
        # как остальные пять полей) — обе проверки «не отправлен вовсе» ниже ловят:
        # пустая строка и строка из одних пробелов доехали бы до кабинета как «поле
        # есть, но пусто» и стёрли бы сохранённые адрес и модель.
        check("alt_llm — непустое поле рядом доехало как есть",
              sent3["data"].get("alt_llm"), "sk-ant-alt-kept")
        check("alt_llm_base='' не отправлен вовсе",
              "alt_llm_base" in sent3["data"], False)
        check("alt_llm_model='  ' (только пробелы) не отправлен вовсе",
              "alt_llm_model" in sent3["data"], False)

        # ── _default_text_model: общий резолвер (используется всеми сайтами) ─
        print("_default_text_model — модель с флагом default")
        pult._rw = FakeRW(models=MODELS_WITH_DEFAULT)
        # Мутация: в `_default_text_model` перестать проверять `item.get("default")` —
        # эта проверка ловит (вернулась бы первая модель списка, "gpt-4o-mini").
        check("взята модель с default:true", pult._default_text_model("sid-x"),
              DEFAULT_MODEL)

        print("_default_text_model — ни одна модель не помечена default → литерал")
        pult._rw = FakeRW(models=[{"value": "gpt-4o-mini", "label": "GPT-4o Mini"}])
        check("откат на литерал (нет default)", pult._default_text_model("sid-x"),
              pult.TEXT_MODEL_FALLBACK)

        print("_default_text_model — пустой список моделей → литерал")
        pult._rw = FakeRW(models=[])
        check("откат на литерал (пусто)", pult._default_text_model("sid-x"),
              pult.TEXT_MODEL_FALLBACK)

        print("_default_text_model — рерайт ответил не 200 → литерал")
        pult._rw = FakeRW(models=MODELS_WITH_DEFAULT, models_code=500)
        check("откат на литерал (код 500)", pult._default_text_model("sid-x"),
              pult.TEXT_MODEL_FALLBACK)

        print("_default_text_model — рерайт недоступен (исключение) → литерал, не падение")
        pult._rw = FakeRW(models=MODELS_WITH_DEFAULT, models_boom=True)
        check("откат на литерал (исключение _rw)", pult._default_text_model("sid-x"),
              pult.TEXT_MODEL_FALLBACK)

        # ── Сайт 1 — ui_text_create(mode="topic") ────────────────────────────
        print("Сайт 1 — ui_text_create(topic): пустая модель карточки → default рерайта")
        fake = FakeRW(models=MODELS_WITH_DEFAULT)
        pult._rw = fake
        pult.ui_text_create(pult.TextIn(mode="topic", source="тема", preset="Пресет",
                                        languages=["ru"], model="", project_id=""),
                            FakeReq())
        gen_calls = [c for c in fake.calls if c[0] == "/jobs/generation"]
        # Мутация: вернуть строку 4210 (`web/app.py`) к литералу `"gpt-4.1-nano"` —
        # эта проверка падает (модель окажется не DEFAULT_MODEL).
        check("модель по умолчанию ушла в заказ генерации",
              gen_calls[-1][2]["model"], DEFAULT_MODEL)

        print("Сайт 1 — ui_text_create(topic): явную модель карточки не подменяем")
        fake = FakeRW(models=MODELS_WITH_DEFAULT)
        pult._rw = fake
        pult.ui_text_create(pult.TextIn(mode="topic", source="тема", preset="Пресет",
                                        languages=["ru"], model="gpt-5", project_id=""),
                            FakeReq())
        gen_calls = [c for c in fake.calls if c[0] == "/jobs/generation"]
        check("явная модель карточки уехала как есть", gen_calls[-1][2]["model"], "gpt-5")
        check("список моделей не спрашивался — явная модель уже есть",
              any(c[0] == "/models" for c in fake.calls), False)

        print("Сайт 1 — ui_text_create(topic): список моделей есть, но default нигде "
             "не флагом → литерал")
        # Отдельно от «пустой список» ниже (сайт 2): здесь модели ЕСТЬ, просто ни у одной
        # нет `default: true` — Edge case #9 ТЗ («Claude-only юзер, у Codex-моделей нет
        # флага»), и это должно вести себя как «недоступно», а не как «взять первую».
        fake = FakeRW(models=[{"value": "gpt-4o-mini", "label": "GPT-4o Mini"},
                              {"value": "claude-x", "label": "Claude X"}])
        pult._rw = fake
        pult.ui_text_create(pult.TextIn(mode="topic", source="тема", preset="Пресет",
                                        languages=["ru"], model="", project_id=""),
                            FakeReq())
        gen_calls = [c for c in fake.calls if c[0] == "/jobs/generation"]
        # Мутация: в `_default_text_model` подставить первую модель списка вместо литерала,
        # когда ни одна не флагом `default`, — эта проверка ловит («gpt-4o-mini» вместо
        # ожидаемого литерала).
        check("моделей несколько, default нет ни у одной — уехал литерал, не первая из списка",
              gen_calls[-1][2]["model"], pult.TEXT_MODEL_FALLBACK)

        # ── Сайт 2 — ui_text_create(mode="text") ─────────────────────────────
        print("Сайт 2 — ui_text_create(text): default рерайта уходит в заказ")
        fake = FakeRW(models=MODELS_WITH_DEFAULT)
        pult._rw = fake
        pult.ui_text_create(pult.TextIn(mode="text", source="просто вставленный текст",
                                        languages=["ru"], model="", project_id=""),
                            FakeReq())
        rw_calls = [c for c in fake.calls if c[0] == "/jobs/rewrite"]
        # Мутация: вернуть строку 4286 (`web/app.py`) к литералу `"gpt-4.1-nano"` — эта
        # проверка падает (модель окажется не DEFAULT_MODEL). До этой проверки сайт 2
        # проверялся только на откате (пустой список ниже), где литерал и резолвер без
        # default дают один и тот же результат и не отличают «зовёт резолвер» от «зашит
        # литерал» — этой проверки не хватало.
        check("модель по умолчанию ушла в заказ рерайта (режим text)",
              rw_calls[-1][2]["model"], DEFAULT_MODEL)

        print("Сайт 2 — ui_text_create(text): нет default у рерайта → литерал")
        fake = FakeRW(models=[])
        pult._rw = fake
        pult.ui_text_create(pult.TextIn(mode="text", source="просто вставленный текст",
                                        languages=["ru"], model="", project_id=""),
                            FakeReq())
        rw_calls = [c for c in fake.calls if c[0] == "/jobs/rewrite"]
        # Мутация: вернуть строку 4286 к литералу напрямую (без резолвера) — эта
        # проверка не отличит их (обе дают TEXT_MODEL_FALLBACK); ловит её проверка
        # выше с MODELS_WITH_DEFAULT через тот же путь (сайт 1), а здесь дополнительно
        # проверяем, что откат работает и на этом сайте отдельно от сайта 1.
        check("литерал ушёл в заказ рерайта, когда default не флагом нигде",
              rw_calls[-1][2]["model"], pult.TEXT_MODEL_FALLBACK)

        print("Сайт 2 — ui_text_create(text): /models отвечает 500 → литерал, не падение")
        fake = FakeRW(models=MODELS_WITH_DEFAULT, models_code=500)
        pult._rw = fake
        pult.ui_text_create(pult.TextIn(mode="text", source="ещё текст",
                                        languages=["ru"], model="", project_id=""),
                            FakeReq())
        rw_calls = [c for c in fake.calls if c[0] == "/jobs/rewrite"]
        # AC-18 буквально требует отката на литерал и при коде 500 на каждом из четырёх
        # сайтов, не только через прямой вызов `_default_text_model` — здесь это
        # проверяется через настоящий вызов сайта 2, а не только резолвера напрямую.
        check("код 500 у /models не роняет заказ — уехал литерал",
              rw_calls[-1][2]["model"], pult.TEXT_MODEL_FALLBACK)

        # ── Сайт 3 — пресет рерайта: форма нового + заведение ────────────────
        print("Сайт 3 — форма нового пресета рерайта: поле «Модель» — из _default_text_model")
        fake = FakeRW(models=MODELS_WITH_DEFAULT)
        pult._rw = fake
        form = pult.ui_neighbour_preset_new_form("rewrite", FakeReq())
        model_field = next(f for f in form["fields"] if f["key"] == "model")
        # Мутация: вернуть `NEIGHBOUR_PRESET_DEFAULTS["rewrite"]["model"] = "gpt-4.1-nano"`
        # литералом и убрать `_neighbour_preset_defaults` — эта проверка падает.
        check("умолчание поля формы — модель с default:true",
              model_field["value"], DEFAULT_MODEL)

        print("Сайт 3 — заведение пресета рерайта: cfg['model'] — из _default_text_model")
        fake = FakeRW(models=MODELS_WITH_DEFAULT)
        pult._rw = fake
        pult.ui_neighbour_preset_create(
            "rewrite", pult.NeighbourPresetNew(name="Новый пресет", values={}), FakeReq())
        sync_calls = [c for c in fake.calls if c[0] == "/settings/presets"]
        saved_cfg = sync_calls[-1][2]["rew_presets"]["Новый пресет"]
        check("модель заведённого пресета — с default:true", saved_cfg["model"],
              DEFAULT_MODEL)

        print("Сайт 3 — заведение пресета рерайта: нет default нигде → литерал")
        fake = FakeRW(models=[])
        pult._rw = fake
        pult.ui_neighbour_preset_create(
            "rewrite", pult.NeighbourPresetNew(name="Пресет без умолчания", values={}),
            FakeReq())
        sync_calls = [c for c in fake.calls if c[0] == "/settings/presets"]
        saved_cfg2 = sync_calls[-1][2]["rew_presets"]["Пресет без умолчания"]
        check("модель заведённого пресета — литерал", saved_cfg2["model"],
              pult.TEXT_MODEL_FALLBACK)

        # Литерал живёт ровно в одном месте (TEXT_MODEL_FALLBACK), а не второй копией
        # в статическом словаре умолчаний пресета.
        check("в статическом словаре умолчаний рерайта модели больше нет",
              pult.NEIGHBOUR_PRESET_DEFAULTS["rewrite"].get("model"), None)

        # ── Структурно: литерал живёт РОВНО в одном месте кода ───────────────
        # Табличные проверки выше ловят поведение (какая модель уехала), но не ловят
        # «починили один сайт с виду через резолвер, а рядом тихо оставили свою копию
        # литерала» — для этого нужен взгляд прямо в исходник вызывающей функции.
        print("Структурно — «gpt-4.1-nano» не зашит в код мест вызова напрямую")
        import inspect
        src_topic = inspect.getsource(pult.ui_text_create)
        # Мутация: вернуть в `ui_text_create` любую из двух веток (topic/text) на
        # литерал `"gpt-4.1-nano"` напрямую вместо `_default_text_model(sid)` — падает.
        check("ui_text_create не содержит литерал напрямую",
              "gpt-4.1-nano" in src_topic, False)
        check("ui_text_create зовёт общий резолвер в обеих ветках (topic и text)",
              src_topic.count("_default_text_model(sid)") >= 2, True)

        src_defaults = inspect.getsource(pult._neighbour_preset_defaults)
        check("_neighbour_preset_defaults не содержит литерал напрямую",
              "gpt-4.1-nano" in src_defaults, False)
        check("_neighbour_preset_defaults зовёт общий резолвер",
              "_default_text_model" in src_defaults, True)

        src_batch = inspect.getsource(pult._batch_text_plan)
        check("_batch_text_plan не содержит литерал напрямую",
              "gpt-4.1-nano" in src_batch, False)
        check("_batch_text_plan зовёт общий резолвер на настоящем прогоне",
              "_default_text_model" in src_batch, True)

        check("единственный оставшийся литерал — сам TEXT_MODEL_FALLBACK",
              pult.TEXT_MODEL_FALLBACK, "gpt-4.1-nano")

        # ── POST /ui/keys — claude_oauth канала промптов нормализуется (ревью 22.09.2026,
        #    Issue 3): та же проверка, что уже стоит у /ui/keys/rewrite, здесь её не было —
        #    ключ API Anthropic (sk-ant-api…) сохранялся бы как «подписка» и либо ломал
        #    задачи промптов, либо платил за каждое слово вместо подписки. ────────────────
        print("POST /ui/keys — prompt.claude_oauth: ключ API Anthropic отклонён без сети")
        real_urlreq.urlopen = fake_urlopen_forbidden   # сеть не должна вызываться вовсе
        secret_api_key = "sk-ant-api03-" + "x" * 40
        try:
            pult.ui_keys_save(
                pult.KeysIn(prompt=pult.PromptChannelIn(claude_oauth=secret_api_key)),
                FakeReq())
            check("ключ API Anthropic в поле подписки промптов отклонён",
                  "прошло без отказа", "HTTPException 422")
        except HTTPException as exc:
            # Мутация: убрать нормализацию claude_oauth в `ui_keys_save` (пересылать
            # значение сырым) — этот блок не бросает исключение, тест ловит на try/except.
            check("код 422 (ключ API Anthropic)", exc.status_code, 422)
            check("текст объясняет разницу с подпиской",
                  "ключ API Anthropic" in str(exc.detail), True)
            check("сам ключ не утёк в текст ошибки", secret_api_key in str(exc.detail), False)

        print("POST /ui/keys — prompt.claude_oauth: мусор без sk-ant-oat тоже отклонён")
        try:
            pult.ui_keys_save(
                pult.KeysIn(prompt=pult.PromptChannelIn(claude_oauth="просто мусор")),
                FakeReq())
            check("мусор в поле подписки промптов отклонён", "прошло без отказа",
                  "HTTPException 422")
        except HTTPException as exc:
            check("код 422 (мусор)", exc.status_code, 422)

        print("POST /ui/keys — prompt.claude_oauth: валидный токен нормализуется "
             "(Bearer/переносы срезаны) перед пересылкой в кабинет")
        real_urlreq.urlopen = fake_urlopen_ok
        raw_token = f"  Bearer {secret_claude}\n"
        pult.ui_keys_save(
            pult.KeysIn(prompt=pult.PromptChannelIn(channel="claude",
                                                    claude_oauth=raw_token)),
            FakeReq())
        check("токен ушёл в кабинет нормализованным, без Bearer/переносов",
              sent["data"].get("claude_oauth"), secret_claude)

        print("POST /ui/keys — prompt.claude_oauth: «-» и пустая строка идут как есть, "
             "без нормализации")
        real_urlreq.urlopen = fake_urlopen_capture
        pult.ui_keys_save(pult.KeysIn(prompt=pult.PromptChannelIn(claude_oauth="-")),
                          FakeReq())
        # Мутация: нормализовать и «-» — `normalize_claude_token("-")` бросил бы
        # `SubscriptionError`, и этот вызов упал бы вместо тихой пересылки «-».
        check("«-» дошло буквально, не нормализовано", sent2["data"].get("claude_oauth"), "-")
        pult.ui_keys_save(pult.KeysIn(prompt=pult.PromptChannelIn(claude_oauth="")),
                          FakeReq())
        check("пустая строка дошла как есть", sent2["data"].get("claude_oauth"), "")

        # ── /ui/keys: own_set/selected считает кабинет через sys.modules["main"]
        #    (ревью 22.09.2026, Issue 4) — своей второй нумерации на пульте больше нет.
        #    Загружаем настоящий `server/main.py` в этот же процесс: тем же приёмом, что
        #    `tests/test_own_key_pool.py`, и специально В КОНЦЕ файла — секции выше уже
        #    отработали на запасном пути (main ещё не был загружен), и порядок им не
        #    важен: поведение фиксируется в момент ВЫЗОВА ручки, а не импорта. ──────────
        print("GET /ui/keys — own_set/selected делегированы кабинету (main загружен)")
        # HOME — ЯВНО, не setdefault: он уже пришёл из оболочки, и `~/VideoStudio/src`
        # (`server/main.py:143`, `import googler_worker`) без этого целился бы в
        # настоящий домашний каталог, а не в тестовый (тот же урок, что в
        # `tests/test_own_key_pool.py`).
        os.environ["HOME"] = os.path.join(BASE, "dev", "home")
        os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
        os.environ.setdefault("VS_SERVER_DB", db_path)
        server_keys_path = os.path.join(work, "server_keys.json")
        SERVER_CLAUDE = "server-shared-claude-token"
        with open(server_keys_path, "w", encoding="utf-8") as f:
            json.dump({"claude_oauth": SERVER_CLAUDE}, f)
        os.environ["VS_SERVER_KEYS_PATH"] = server_keys_path
        sys.path.insert(0, os.path.join(BASE, "server"))
        import main as cabinet                                       # noqa: E402
        check("main.py загрузился и виден пульту через sys.modules",
              sys.modules.get("main") is cabinet, True)

        # (а) копия серверного ключа своей не считается — own_set=false, хотя значение
        #     в users.keys непустое.
        copy_of_server_user = {
            "id": 10, "name": "т10", "role": "user", "tenant_id": "dev",
            "keys": json.dumps({"claude_oauth": SERVER_CLAUDE}),
            "key_opts": "{}",
        }
        out5 = pult.ui_keys(FakeReq(user=copy_of_server_user))
        # Мутация: в `_prompt_state` не делегировать кабинету (вернуть старый прямой
        # разбор JSON) — эта проверка падает: старый код считал бы own_set=true, у него
        # нет понятия «копия серверного ключа».
        check("(а) копия серверного ключа — own_set=false у claude",
              out5["prompt"]["channels"]["claude"]["own_set"], False)

        # (б) codex_auth, не похожий на JSON auth.json — own_set=false.
        garbage_codex_user = {
            "id": 11, "name": "т11", "role": "user", "tenant_id": "dev",
            "keys": json.dumps({"codex_auth": "просто путь или мусор, не auth.json"}),
            "key_opts": "{}",
        }
        out6 = pult.ui_keys(FakeReq(user=garbage_codex_user))
        check("(б) codex_auth не похож на auth.json — own_set=false",
              out6["prompt"]["channels"]["codex"]["own_set"], False)

        # (в) устаревший выбор: канал выбран, но своего credential нет, а серверного
        #     credential этого канала тоже нет (в server_keys.json только claude_oauth) —
        #     обычному юзеру без admin выбор недоступен, selected возвращается «Авто».
        stale_user = {
            "id": 12, "name": "т12", "role": "user", "tenant_id": "dev",
            "keys": "{}",
            "key_opts": json.dumps({"_prompt": {"provider": "codex"}}),
        }
        out7 = pult.ui_keys(FakeReq(user=stale_user))
        # Мутация: в `_prompt_state` вернуть `selected` сырым `key_opts["_prompt"]`
        # без проверки через кабинет — эта проверка ловит (старый код отдал бы "codex").
        check("(в) устаревший выбор канала — selected сброшен на «Авто»",
              out7["prompt"]["selected"], "")

        # Секрет по-прежнему не утекает и в делегированном пути.
        dump5 = json.dumps([out5, out6, out7], ensure_ascii=False, default=str)
        check("секрет не утёк в делегированном пути /ui/keys", SERVER_CLAUDE in dump5, False)

        # (г) main загружен, но сам вызов кабинета падает (баг в кабинете, несовместимая
        #     версия и т. п.) — честный запасной путь, а не 500 на всей странице «Ключи».
        #     Это ДРУГОЙ путь к запасному чтению, чем секции в начале файла (там `main`
        #     ещё не был загружен вовсе): здесь `mod is not None`, но try/except внутри
        #     `_prompt_state` обязан поймать исключение САМОГО вызова.
        print("GET /ui/keys — main загружен, но _own_prompt_channels падает → запасной путь")
        def _boom(u):
            raise RuntimeError("кабинет временно недоступен")
        orig_own_channels = cabinet._own_prompt_channels
        cabinet._own_prompt_channels = _boom
        try:
            raising_user = {
                "id": 13, "name": "т13", "role": "user", "tenant_id": "dev",
                "keys": json.dumps({"claude_oauth": "own-claude-token-zzzz"}),
                "key_opts": json.dumps({"_prompt": {"provider": "claude"}}),
            }
            out8 = pult.ui_keys(FakeReq(user=raising_user))
        finally:
            cabinet._own_prompt_channels = orig_own_channels
        # Мутация: убрать `try/except Exception: pass` вокруг вызова кабинета в
        # `_prompt_state` (дать исключению всплыть наружу) — эта ручка бросила бы
        # RuntimeError вместо ответа, и весь экран «Ключи» упал бы из-за одной
        # сломавшейся функции кабинета, а не только раздел `prompt`.
        check("исключение кабинета не роняет /ui/keys — ответ есть",
              isinstance(out8, dict) and "prompt" in out8, True)
        check("запасной путь при падении кабинета: свой claude_oauth даёт own_set=true "
             "(честное прямое чтение, не делегирование)",
              out8["prompt"]["channels"]["claude"]["own_set"], True)
        check("запасной путь при падении кабинета: выбор канала — прямое чтение key_opts",
              out8["prompt"]["selected"], "claude")

        # ── Структурно: резолвер не зовётся из цикла событий ──────────────────
        # Требование задачи прямым текстом: `_default_text_model` блокирующий (ходит
        # в `_rw`), и вызывающая его ручка обязана быть `def`, а не `async def` — иначе
        # FastAPI не унесёт вызов в threadpool, и блокирующий поход в Rewrite встанет
        # прямо в event loop и заблокирует ВЕСЬ процесс на время ответа/таймаута.
        print("Структурно — _default_text_model не вызывается из async-обработчика")
        for fn in (pult.ui_text_create, pult.ui_neighbour_preset_new_form,
                  pult.ui_neighbour_preset_create, pult.ui_projects_batch_text,
                  pult._default_text_model, real_rw_fn):
            # Мутация: пометить любую из этих функций `async def` — падает здесь.
            check(f"{fn.__name__} — синхронная функция (def, не async def)",
                  inspect.iscoroutinefunction(fn), False)

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} проверок не прошли: {FAILS}")
        return 1
    print("OK: все проверки прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
