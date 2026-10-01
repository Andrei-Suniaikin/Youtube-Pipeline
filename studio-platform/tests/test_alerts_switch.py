#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тестовый стенд не шлёт тревоги владельцу: `VS_ALERTS=0` выключает цикл.

21.09.2026 владелец попросил прекратить спам в телеграм. Половина его — тестовые
стенды на копии базы: каждый `dev/run.sh` с настроенным `.env` заводил свой цикл
тревог, и тревоги про копию шли в тот же чат, что и боевые.

Тест вырезает из `dev/run.sh` сам блок запуска тревог и выполняет его в bash с
поддельным `.env`, где телеграм настроен. Вместо Python подставлен `echo` — сети нет,
видно только, запустился бы цикл или нет.

Вторая половина — экран «Админ» того же стенда: с выключенным циклом он не имеет
права показывать «Телеграм настроен ✓», иначе владелец решит, что тревоги уходят.

    .venv/bin/python tests/test_alerts_switch.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402

ADMIN = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push(e.message));
  try {
    let ДОЛГИ = [];
    const A = off => ({active: [], owed_recovered: ДОЛГИ, thresholds: [],
      transport: {configured: true, token: '12…ab', chat: '-100', how: '', loop_off: off}});
    let OFF = true;
    window.api = async p => {
      if (p === '/ui/alerts') return A(OFF);
      // Остальные плитки экрана — минимально честной формы: проверяется только
      // карточка тревог, но экран обязан открыться целиком.
      if (p === '/ui/disk') return {policy_set: false, auto: false, policy: {},
        env: {strip: 'VS_RETENTION_STRIP_DAYS', drop: 'VS_RETENTION_DROP_DAYS'},
        last: null, how: '', projects: [], total: 0, freeable: 0};
      if (p === '/ui/public') return {how: '', key_set: false, key: '', confirmed: 0,
        scheduled: 0, due: [], checked: [], grace_min: 0, blind_spot: ''};
      if (p === '/ui/subs-exits') return {read_error: 'нет'};
      if (p === '/ui/accounts') throw new Error('не администратор');  // раздела нет
      return {};
    };
    const name = () => {
      const sec = [...document.querySelectorAll('.sec')].find(
          x => x.textContent.includes('Тревоги в телеграм'));
      const st = sec && sec.parentElement.querySelector('.stage');
      return st ? {name: st.querySelector('.name').textContent.trim(),
                   ok: st.querySelector('.dot').classList.contains('s-done')} : null;
    };
    try { await viewAdmin(); } catch (e) { out.errs.push('viewAdmin: ' + e.message + ' @ ' + String(e.stack).split('\n')[1]); }
    out.выключен = name();
    OFF = false;
    try { await viewAdmin(); } catch (e) { out.errs.push('viewAdmin: ' + e.message + ' @ ' + String(e.stack).split('\n')[1]); }
    out.включён = name();

    // Строка «ждут сообщения «починилось»» появляется только когда долги есть, и
    // именно поэтому её отступы однажды уехали незамеченными: пустой список её не
    // рисует, а замер отступов (tests/test_spacing_browser.py) ловит её только на тех
    // данных стенда, где долг случился. Здесь долг задан нарочно.
    ДОЛГИ = [{key: 'fastgen', title: 'Ключ FastGen кончился'}];
    try { await viewAdmin(); } catch (e) { out.errs.push('viewAdmin: ' + e.message); }
    {
      const нота = [...document.querySelectorAll('.note')].find(
          x => (x.textContent || '').includes('Ждут сообщения'));
      out.долгВиден = !!нота;
      out.долгНазван = нота ? (нота.textContent || '').includes('Ключ FastGen') : false;
      // Отступ берёт обёртка .secbody — то же правило, что у остального содержимого
      // карточек. Без неё строка стоит вплотную к рамке и к низу карточки.
      out.долгВОбёртке = !!(нота && нота.closest('.secbody'));
    }
  } catch (e) { out.errs.push(String(e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def run_block(env_extra: dict) -> str:
    src = open(os.path.join(BASE, "dev", "run.sh"), encoding="utf-8").read()
    start = src.index('if [ "${VS_ALERTS:-1}" = "0" ]; then')
    end = src.index("\nfi\n", start) + 4
    block = src[start:end]
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, ".env"), "w", encoding="utf-8") as f:
            f.write("VS_TG_TOKEN=token\nVS_TG_CHAT=chat\n")
        script = f'ROOT="{root}"; PY="echo"\n{block}\nwait\necho "$ALERTS"\n'
        env = {k: v for k, v in os.environ.items() if k != "VS_ALERTS"}
        env.update(env_extra)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env=env, timeout=20).stdout


def main() -> int:
    print("Проверка: VS_ALERTS=0 не заводит цикл тревог")
    off = run_block({"VS_ALERTS": "0"})
    check("с VS_ALERTS=0 цикл не запускается", "dev/alerts.py" in off, False)
    check("и сказано почему", "VS_ALERTS=0" in off, True)
    on = run_block({})
    check("без переключателя и с телеграмом — запускается, как раньше",
          "dev/alerts.py --loop" in on, True)

    print("\nЭкран «Админ» того же стенда")
    seed = os.path.join(BASE, "dev", "vsgen.dev.db")
    if not os.path.exists(seed):
        print(f"  НЕ ПРОВЕРЕНО: нет базы-образца {seed}")
        FAILS.append("нет базы-образца")
    else:
        work = tempfile.mkdtemp(prefix="alerts-switch-")
        try:
            db_path = os.path.join(work, "vsgen.db")
            src = sqlite3.connect(seed)
            dst = sqlite3.connect(db_path)
            src.backup(dst)
            dst.close()
            src.close()
            os.environ["VS_DEV_DB"] = db_path
            os.environ["VS_TENANTS_DIR"] = os.path.join(work, "tenants")
            sys.path.insert(0, BASE)
            sys.path.insert(0, os.path.join(BASE, "server"))
            import web.app as pult                                 # noqa: E402
            os.environ["VS_ALERTS"] = "0"
            check("ручка тревог говорит, что цикл выключен",
                  pult.ui_alerts()["transport"].get("loop_off"), True)
            os.environ.pop("VS_ALERTS", None)
            check("без переключателя — не выключен",
                  pult.ui_alerts()["transport"].get("loop_off"), False)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    res = browser_harness.run_scenario(ADMIN, budget_ms=9000)
    check("экран открылся без ошибок", res.get("errs"), [])
    check("выключенный цикл назван прямо и без зелёной галочки", res.get("выключен"),
          {"name": "Тревоги на этом стенде выключены (VS_ALERTS=0) — никуда не уходят",
           "ok": False})
    check("включённый — как раньше", res.get("включён"),
          {"name": "Телеграм настроен", "ok": True})

    print("\nДолг «починилось» видно, и он не слипается с рамкой")
    check("строка про долг показана", res.get("долгВиден"), True)
    check("и названа тревога", res.get("долгНазван"), True)
    # Отступ даёт обёртка `.secbody` — то же правило, что у остального содержимого
    # карточек. Без неё строка стояла вплотную к левой рамке и к низу карточки, и
    # поймал это только замер отступов на тех данных стенда, где долг случился.
    check("отступ есть — строка в .secbody", res.get("долгВОбёртке"), True)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
