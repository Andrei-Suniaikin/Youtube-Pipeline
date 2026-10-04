#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выкатка ждёт идущие заливки и не рвёт их перезапуском (04.10.2026).

В 13:04 выкатка перезапустила студию, пока минская машина забирала файл ролика: нода
получила 502 и сорвала заливку. Посреди передачи было бы хуже — ролик ушёл бы в YouTube,
а отчёт о нём нет. Проверяется на поддельном корне (systemctl, git, curl — заглушки):

  • заливка идёт и не кончается — выкатки нет: ни перехода на новый код, ни перезапуска;
  • заливка кончилась во время ожидания — выкатка идёт;
  • заливок нет — сразу;
  • STUDIO_FORCE_RESTART=1 — без ожидания.

    .venv/bin/python tests/test_deploy_waits_uploads.py
"""
from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import tempfile
import threading
import time

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
    "git": '''#!/bin/bash
echo "$*" >> "$FAKE/git.log"
case "$1 $2" in
  "rev-parse HEAD") [ -f "$FAKE/.after" ] && echo 2222222222222222222222222222222222222222 || echo 1111111111111111111111111111111111111111 ;;
  "rev-parse --short") echo 2222222 ;;
  "diff --name-only") echo "server/x.py" ;;
  *) case "$1" in
       pull) touch "$FAKE/.after" ;;
       checkout) touch "$FAKE/.after" ;;
       rev-parse) echo 2222222 ;;
     esac ;;
esac
exit 0
''',
    "systemctl": '#!/bin/bash\n[ "$1" = "restart" ] && touch "$FAKE/перезапуск"\nexit 0\n',
    "curl": '#!/bin/bash\necho 200\nexit 0\n',
    "journalctl": '#!/bin/bash\nexit 0\n',
}


def прогон(running: int, wait="3", poll="1", force=False, finish_after=None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        корень = os.path.join(tmp, "srv-studio")
        код = os.path.join(корень, "src", "studio-platform")
        os.makedirs(os.path.join(код, "deploy"))
        os.makedirs(os.path.join(корень, ".venv", "bin"))
        os.makedirs(os.path.join(корень, "data"))
        os.symlink(код, os.path.join(корень, "app"))
        io.open(os.path.join(код, "deploy", "update.sh"), "w", encoding="utf-8").write(
            io.open(UPDATE, encoding="utf-8").read())
        io.open(os.path.join(код, "deploy", "start-studio.sh"), "w").write("#!/bin/bash\n")
        io.open(os.path.join(корень, "start-studio.sh"), "w").write("#!/bin/bash\n")
        db = os.path.join(корень, "data", "vsgen.db")
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE pub_upload_jobs(id INTEGER PRIMARY KEY, status TEXT)")
        for _ in range(running):
            c.execute("INSERT INTO pub_upload_jobs(status) VALUES('running')")
        c.execute("INSERT INTO pub_upload_jobs(status) VALUES('done')")
        c.commit()
        c.close()
        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(bin_dir)
        for имя, тело in ЗАГЛУШКИ.items():
            путь = os.path.join(bin_dir, имя)
            io.open(путь, "w", encoding="utf-8").write(тело)
            os.chmod(путь, 0o755)
        окр = dict(os.environ)
        окр.update({"PATH": bin_dir + os.pathsep + окр.get("PATH", ""), "STUDIO_ROOT": корень,
                    "FAKE": tmp, "STUDIO_SERVICE": "studio-platform",
                    "STUDIO_UPLOAD_WAIT_SEC": wait, "STUDIO_UPLOAD_POLL_SEC": poll})
        if force:
            окр["STUDIO_FORCE_RESTART"] = "1"
        drain_seen = []
        if finish_after is not None:
            def finish():
                time.sleep(finish_after)
                drain_seen.append(os.path.exists(os.path.join(корень, "data", "upload-drain")))
                cc = sqlite3.connect(db)
                cc.execute("UPDATE pub_upload_jobs SET status='done'")
                cc.commit()
                cc.close()
            threading.Thread(target=finish, daemon=True).start()
        out = subprocess.run(["bash", os.path.join(код, "deploy", "update.sh"), "main"],
                             capture_output=True, text=True, env=окр, timeout=120, cwd=код)
        git_log = os.path.join(tmp, "git.log")
        return {"код": out.returncode, "вывод": out.stdout + out.stderr,
                "флаг_во_время": drain_seen[:1],
                "флаг_после": os.path.exists(os.path.join(корень, "data", "upload-drain")),
                "перезапуск": os.path.exists(os.path.join(tmp, "перезапуск")),
                "git": io.open(git_log).read() if os.path.exists(git_log) else ""}


def main() -> int:
    print("Заливка идёт и не кончается — выкатки нет")
    r = прогон(running=1)
    check("выход с отказом", r["код"], 2)
    check("перезапуска не было", r["перезапуск"], False)
    check("на новый код не переходили", "fetch" in r["git"] or "checkout" in r["git"], False)
    check("и при отказе флаг убран — заливка не стоит", r["флаг_после"], False)
    check("сказано почему и что делать",
          "выкатку НЕ делаю" in r["вывод"] and "STUDIO_FORCE_RESTART=1" in r["вывод"], True)

    print("\nЗаливка кончилась во время ожидания — выкатка идёт")
    r = прогон(running=1, wait="20", poll="1", finish_after=1.5)
    check("выкатка прошла", r["код"], 0)
    check("перезапуск был", r["перезапуск"], True)
    check("сказано, что ждали", "идёт заливка (1) — жду" in r["вывод"], True)
    check("пока ждали — новых заданий не выдавать (флаг стоял)", r["флаг_во_время"], [True])
    check("после выкатки флаг убран", r["флаг_после"], False)

    print("\nЗаливок нет — сразу")
    r = прогон(running=0)
    check("выкатка прошла", (r["код"], r["перезапуск"]), (0, True))
    check("про ожидание ни слова", "жду" in r["вывод"], False)

    print("\nSTUDIO_FORCE_RESTART=1 — без ожидания")
    r = прогон(running=2, force=True)
    check("выкатка прошла", (r["код"], r["перезапуск"]), (0, True))
    check("сказано, что без ожидания", "без ожидания заливок" in r["вывод"], True)
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
