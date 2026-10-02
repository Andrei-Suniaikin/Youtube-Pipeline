#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выкатка обновляет и скрипт запуска, который зовёт systemd (02.10.2026).

Юнит запускает НЕ `$APP/deploy/start-studio.sh`, а копию `/srv/studio/start-studio.sh` —
так сделано нарочно: служба должна подниматься, даже когда каталог кода на перекладке.
Но до 02.10 эту копию никто не обновлял, и правка скрипта запуска молча НЕ выкатывалась.

Как нашлось: адрес чата движка уехал на FastGen в репозитории, выкатка прошла, служба
перезапустилась — а в живом процессе переменной не было. Копия на машине оказалась от
23.09, то есть расхождение жило девять дней и ничем не сигналило.

Это та же половинчатая выкатка, из-за которой автозвук считался рабочим: серверная
половина на месте, вторая нет, а интерфейс показывает рабочую настройку. Поэтому
проверяется не текст скрипта, а само СВОЙСТВО: проход выкатки обновляет копию, делает
это ДО перезапуска и кладёт рядом резервную.

Настоящая машина не нужна: `update.sh` гоняется на поддельном корне, а systemctl, git и
curl подменены заглушками в PATH.

    .venv/bin/python tests/test_deploy_start_script.py
"""
from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
UPDATE = os.path.join(BASE, "deploy", "update.sh")

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


ЗАГЛУШКИ = {
    # git: версия «меняется», чтобы проход дошёл до установки, а не вышел на «нечего».
    "git": '''#!/bin/bash
case "$1 $2" in
  "rev-parse HEAD") [ -f "$FAKE/.after" ] && echo 2222222222222222222222222222222222222222 || echo 1111111111111111111111111111111111111111 ;;
  "rev-parse --short") echo 2222222 ;;
  "diff --name-only") echo "deploy/start-studio.sh" ;;
  *) case "$1" in
       pull) touch "$FAKE/.after" ;;
       fetch|checkout|status) : ;;
       diff) exit 0 ;;
       rev-parse) echo 2222222 ;;
     esac ;;
esac
exit 0
''',
    "systemctl": '''#!/bin/bash
# Порядок важен: отмечаем момент перезапуска, чтобы проверить, что копия уже обновлена.
if [ "$1" = "restart" ]; then
  cp "$STUDIO_ROOT/start-studio.sh" "$FAKE/при-перезапуске" 2>/dev/null || true
fi
exit 0
''',
    "curl": '#!/bin/bash\necho 200\nexit 0\n',
    "journalctl": '#!/bin/bash\nexit 0\n',
}


def прогон(скрипт_в_репозитории: str) -> dict:
    """Гоняет update.sh на поддельном корне. Возвращает, что стало с копией."""
    with tempfile.TemporaryDirectory() as tmp:
        корень = os.path.join(tmp, "srv-studio")
        код = os.path.join(корень, "src", "studio-platform")
        os.makedirs(os.path.join(код, "deploy"))
        os.makedirs(os.path.join(корень, ".venv", "bin"))
        os.symlink(код, os.path.join(корень, "app"))
        io.open(os.path.join(код, "deploy", "start-studio.sh"), "w",
                encoding="utf-8").write(скрипт_в_репозитории)
        io.open(os.path.join(код, "deploy", "update.sh"), "w", encoding="utf-8").write(
            io.open(UPDATE, encoding="utf-8").read())
        # Копия, которую зовёт systemd: нарочно устаревшая.
        копия = os.path.join(корень, "start-studio.sh")
        io.open(копия, "w", encoding="utf-8").write("#!/bin/bash\n# версия от 23.09\n")
        os.chmod(копия, 0o755)
        io.open(os.path.join(корень, ".venv", "bin", "pip"), "w").write("#!/bin/bash\nexit 0\n")
        os.chmod(os.path.join(корень, ".venv", "bin", "pip"), 0o755)

        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(bin_dir)
        for имя, тело in ЗАГЛУШКИ.items():
            путь = os.path.join(bin_dir, имя)
            io.open(путь, "w", encoding="utf-8").write(тело)
            os.chmod(путь, 0o755)

        окр = dict(os.environ)
        окр["PATH"] = bin_dir + os.pathsep + окр.get("PATH", "")
        окр["STUDIO_ROOT"] = корень
        окр["FAKE"] = tmp
        окр["STUDIO_SERVICE"] = "studio-platform"
        out = subprocess.run(["bash", os.path.join(код, "deploy", "update.sh"), "main"],
                             capture_output=True, text=True, env=окр, timeout=180,
                             cwd=код)
        при_перезапуске = os.path.join(tmp, "при-перезапуске")
        резервные = [f for f in os.listdir(корень) if f.startswith("start-studio.sh.bak-")]
        return {
            "вывод": out.stdout + out.stderr,
            "копия": io.open(копия, encoding="utf-8").read(),
            "права": oct(stat.S_IMODE(os.stat(копия).st_mode)),
            "при_перезапуске": (io.open(при_перезапуске, encoding="utf-8").read()
                                if os.path.exists(при_перезапуске) else "(перезапуска не было)"),
            "резервных": len(резервные),
        }


def частью_скрипта_не_включено() -> None:
    """Чат движка на стороннем прокси по умолчанию ВЫКЛЮЧЕН.

    02.10.2026 он был включён и к вечеру выключен: FastGen снимает проблему пустого
    счёта, но `response_format` уважает не на всех моделях и возвращает свободный текст
    (это описано в самом воркере, `_alt_llm_call`). Планировщику сцен нужен строгий
    JSON, поэтому задача 154 дала 23 отказа схемы из 50 попыток и умерла, тогда как
    соседние задачи на прежнем адресе давали 0–2 из 12–53 и доходили.

    Включать обратно можно только вместе с текстовой директивой схемы. Проверка стоит
    здесь, чтобы включение было осознанным, а не вернулось правкой «по аналогии».
    """
    src = io.open(os.path.join(BASE, "deploy", "start-studio.sh"), encoding="utf-8").read()
    print("\nЧат движка на стороннем прокси по умолчанию выключен")
    check("адрес сам собой не подставляется",
          'VS_GPT_CHAT_URL="${VS_GPT_CHAT_URL:-https://api.fast-gen.ai' in src, False)
    check("но ручка осталась — включается одной переменной",
          'if [ -n "${VS_GPT_CHAT_URL:-}" ]; then' in src, True)
    check("и причина выключения записана рядом",
          "уважает `response_format` НЕ на всех" in src, True)


def main() -> int:
    НОВЫЙ = "#!/bin/bash\n# версия от 02.10\nexport VS_GPT_CHAT_URL=https://api.fast-gen.ai/v1/chat/completions\n"

    print("Копия, которую зовёт systemd, обновляется выкаткой")
    r = прогон(НОВЫЙ)
    check("копия стала новой", r["копия"], НОВЫЙ)
    check("и осталась исполняемой", r["права"], "0o755")
    check("сказано вслух, что копия обновлена",
          "скрипт запуска изменился" in r["вывод"], True)
    check("рядом легла резервная", r["резервных"], 1)
    # Порядок: перезапуск ПОСЛЕ подмены, иначе служба поднимется на старом скрипте и
    # расхождение проживёт до следующей выкатки — ровно то, что было с 23.09.
    check("перезапуск увидел уже новую копию", r["при_перезапуске"], НОВЫЙ)

    print("\nСовпадающую копию не трогаем — лишних резервных не плодим")
    r2 = прогон("#!/bin/bash\n# версия от 23.09\n")
    check("копия не менялась", r2["копия"], "#!/bin/bash\n# версия от 23.09\n")
    check("про обновление не сказано",
          "скрипт запуска изменился" in r2["вывод"], False)
    check("резервных не появилось", r2["резервных"], 0)

    частью_скрипта_не_включено()

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
