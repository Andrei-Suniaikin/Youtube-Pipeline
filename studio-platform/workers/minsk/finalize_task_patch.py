#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Патч пуллера заливки на Минске: задача ДОВЕДЕНИЯ черновика по номеру (18.10.2026).

ЗАЧЕМ. Третий шаг «скрипта удержанных окон». Ролик залит, номер известен (вердикт
«залит, но не подтверждён» или номер донесён из удержанного окна), а в расписании его
нет. Кабинет теперь выдаёт такое задание той же ручкой клейма с полями
`finalize: true, finalize_video_id: <id>` — и ждёт, что машина НЕ зальёт файл второй
раз, а доведёт черновик: `upload_worker.py --finalize-draft --finalize-video-id <id>`
в строгом режиме (`VIDEO_UPLOAD_STRICT=1`) сам открывает профиль, находит строку с
этим номером в Studio (`_strict_finalize_row`) и дописывает ей расписание. Файл ролика
сервер для такого задания не отдаёт (409) — качается только обложка: строгий режим без
неё не стартует.

Пуллер умел это только как «продолжение» в уже открытом окне (continuation с портом) и
только для манхвы (`m_`, `_known_resume_scope`). Для обычных `r_` заданий — нет.

ЧТО МЕНЯЕТ (пять якорных замен, всё — добавление):
  1. `_run_upload_once`: при `task["finalize_video_id"]` — флаги доведения, строгий
     режим, БЕЗ присоединения к порту (своё окно) и БЕЗ автоподтверждения (как у
     continuation без resume_auth_ok: пароль второй раз не вводим);
  2. там же: автоподтверждение не включается для задачи доведения;
  3. там же, сторож брони: потеря брони при доведении снимает заливщика — файл он не
     передаёт, черновик останется черновиком;
  4. там же: фаза с ДРУГИМ номером видео — остановка, не трогаем чужой ролик;
  5. `run_upload`: задача доведения идёт прямо в `_run_upload_once`, мимо журнала
     возобновлений манхвы (ему нужен файл, которого нет);
  6. `handle`: при доведении ролик не качается — путь к видео заглушка (заливщик
     требует `--video`, но при `--finalize-draft` файл не читает), качается обложка.

Применить (на Минске, от vova):   python3 finalize_task_patch.py [~/upload_puller.py]
Проверить без записи:              python3 finalize_task_patch.py --check
Бэкап рядом: upload_puller.py.bak-ГГГГММДД-ЧЧММСС-finalize. После патча — py_compile.
Перезапуск службы пуллера — не во время идущей заливки (см. памятку «Заливщик — свобода
правок 15.10»).
"""
import os
import py_compile
import shutil
import sys
import time

DEFAULT = os.path.expanduser("~/upload_puller.py")
MARK = 'task.get("finalize_video_id")'


def replace_once(text, old, new, where):
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"{where}: якорь не найден или не уникален ({n}): {old[:70]!r}")
    return text.replace(old, new)


# 1. Флаги доведения в _run_upload_once — сразу после блока continuation, до owner_route.
OLD_1 = """        if not resume_auth_ok:
            env.pop("VIDEO_UPLOAD_AUTO_AUTH", None)
            env.pop("VIDEO_UPLOAD_AUTH_PROTOCOL", None)
    if task.get("owner_route"):
        import owner_fallback
        owner_fallback.validate_task(task, time.time())
"""
NEW_1 = """        if not resume_auth_ok:
            env.pop("VIDEO_UPLOAD_AUTO_AUTH", None)
            env.pop("VIDEO_UPLOAD_AUTH_PROTOCOL", None)
    # 18.10.2026: задача ДОВЕДЕНИЯ от кабинета — ролик уже на канале под этим номером,
    # вердикта «в расписании» нет. Файл не передаём: заливщик в строгом режиме находит
    # черновик по номеру в Studio и дописывает расписание. Окно своё (порт не задан),
    # автоподтверждение выключено — как у continuation без resume_auth_ok: второй ввод
    # пароля на том же профиле — путь к локауту Google.
    finalize_id = str(task.get("finalize_video_id") or "")
    if finalize_id and not continuation:
        argv += ["--finalize-draft", "--finalize-video-id", finalize_id]
        env["VIDEO_UPLOAD_STRICT"] = "1"
        env.pop("VIDEO_UPLOAD_FINALIZE_EXISTING_PORT", None)
        env.pop("VIDEO_UPLOAD_THUMBNAIL_PROOF_PATH", None)
        env.pop("VIDEO_UPLOAD_AUTO_AUTH", None)
        env.pop("VIDEO_UPLOAD_AUTH_PROTOCOL", None)
    if task.get("owner_route"):
        import owner_fallback
        owner_fallback.validate_task(task, time.time())
"""

# 2. Автоподтверждение не включается для задачи доведения.
OLD_2 = """    if ((not continuation and str(task["profile"]) in auth_profiles) or resume_auth_ok) \\
            and env.get("VIDEO_UPLOAD_AUTO_AUTH") == "1":
"""
NEW_2 = """    if ((not continuation and not finalize_id and str(task["profile"]) in auth_profiles)
            or resume_auth_ok) and env.get("VIDEO_UPLOAD_AUTO_AUTH") == "1":
"""

# 3. Сторож брони: при доведении снять заливщика безопасно.
OLD_3 = """            if continuation:
                # Killing only the attachment child cannot stop/re-upload Chrome's MP4.
                lease_note["killed"] = lease.lost_error or "LEASE_LOST during attach-only continuation"
                proc.kill()
                return
"""
NEW_3 = """            if continuation:
                # Killing only the attachment child cannot stop/re-upload Chrome's MP4.
                lease_note["killed"] = lease.lost_error or "LEASE_LOST during attach-only continuation"
                proc.kill()
                return
            if finalize_id:
                # Доведение по номеру файл не передаёт: снятый заливщик оставляет черновик
                # черновиком, кабинет повторит попытку не раньше чем через 6 часов.
                lease_note["killed"] = lease.lost_error or "LEASE_LOST during finalize-by-id"
                proc.kill()
                return
"""

# 4. Фаза с другим номером видео при доведении — остановка.
OLD_4 = """        if line.startswith("__UPLOAD_RETAINED__|"):
            try:
                retained = json.loads(line.split("|", 1)[1])
"""
NEW_4 = """        if finalize_id and not continuation and line.startswith(PHASE_MARK):
            # Заливщик обязан работать с тем же номером, что дал кабинет. Другой номер в
            # фазе — чужой ролик (похожее название на соседнем выпуске): останавливаемся,
            # кабинету уходит отказ с нашим номером, повтор заливки запрещён.
            try:
                _ev = json.loads(line[len(PHASE_MARK):]) or {}
            except Exception:
                _ev = {}
            _seen = str(_ev.get("video_id") or "") if isinstance(_ev, dict) else ""
            if _seen and _seen != finalize_id:
                proc.kill()
                result = {"success": False, "video_id": finalize_id, "do_not_reupload": True,
                          "phase": "video_id_known",
                          "error": f"FINALIZE_BY_ID_IDENTITY_CHANGED: заливщик назвал {_seen}"}
                break
        if line.startswith("__UPLOAD_RETAINED__|"):
            try:
                retained = json.loads(line.split("|", 1)[1])
"""

# 5. run_upload: доведение — мимо журнала возобновлений манхвы.
OLD_5 = """def run_upload(task: dict, projdir: str, video: str, cover: str, lease=None) -> dict:
    scope = _known_resume_scope(task)
"""
NEW_5 = """def run_upload(task: dict, projdir: str, video: str, cover: str, lease=None) -> dict:
    if task.get("finalize_video_id"):
        # Доведение по номеру: файла нет, журнал возобновлений манхвы ему не нужен и
        # не смог бы его опознать (sha256 файла). Прямо в заливщик.
        return _run_upload_once(task, projdir, video, cover, lease=lease)
    scope = _known_resume_scope(task)
"""

# 6. handle: ролик не качаем, обложку — да.
OLD_6 = """        video = download(rid, "video", claim, os.path.join(projdir, "video.mp4"))
        if not video:
            out = {"result": {"success": False, "error": "сервер не отдал ролик"}}
            return
        cover = download(rid, "cover", claim, os.path.join(projdir, "cover.jpg"))
        size = os.path.getsize(video) // (1024 * 1024)
        say(rid, claim, stdout=f"привёз ролик {size} МБ" + (", обложка есть" if cover else ""))
"""
NEW_6 = """        finalize_id = str(task.get("finalize_video_id") or "")
        if finalize_id:
            # Доведение по номеру: ролик уже на канале, файл не качаем (сервер его и не
            # отдаст — 409). Путь к видео — заглушка: заливщик требует --video, но при
            # --finalize-draft файл не читает. Обложка нужна: строгий режим без неё не
            # стартует, а черновику её как раз и дописываем.
            video = os.path.join(projdir, "video.mp4")
            cover = download(rid, "cover", claim, os.path.join(projdir, "cover.jpg"))
            if not cover:
                out = {"result": {"success": False, "video_id": finalize_id, "do_not_reupload": True,
                                  "phase": "video_id_known",
                                  "error": "доведение по номеру: сервер не отдал обложку, "
                                           "а строгий режим без неё черновик не доводит"}}
                return
            say(rid, claim, stdout=f"доведение черновика {finalize_id} по номеру: файл не качаю, обложка есть")
        else:
            video = download(rid, "video", claim, os.path.join(projdir, "video.mp4"))
            if not video:
                out = {"result": {"success": False, "error": "сервер не отдал ролик"}}
                return
            cover = download(rid, "cover", claim, os.path.join(projdir, "cover.jpg"))
            size = os.path.getsize(video) // (1024 * 1024)
            say(rid, claim, stdout=f"привёз ролик {size} МБ" + (", обложка есть" if cover else ""))
"""

STEPS = ((OLD_1, NEW_1, "1 флаги доведения"), (OLD_2, NEW_2, "2 автоподтверждение"),
         (OLD_3, NEW_3, "3 сторож брони"), (OLD_4, NEW_4, "4 чужой номер в фазе"),
         (OLD_5, NEW_5, "5 run_upload"), (OLD_6, NEW_6, "6 handle без файла"))


def patched(text: str) -> str:
    for old, new, where in STEPS:
        text = replace_once(text, old, new, where)
    return text


def main(argv) -> int:
    check_only = "--check" in argv
    paths = [a for a in argv if a != "--check"]
    path = paths[0] if paths else DEFAULT
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if MARK in src:
        print(f"{path}: патч уже применён — ничего не делаю")
        return 0
    out = patched(src)
    if check_only:
        print(f"{path}: все {len(STEPS)} якорей на месте, патч применим (без записи)")
        return 0
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}-finalize"
    shutil.copy2(path, backup)
    tmp = path + ".finalize-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(out)
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as exc:
        os.unlink(tmp)
        raise SystemExit(f"патч не компилируется, файл не тронут: {exc}")
    os.replace(tmp, path)
    print(f"готово: {path}\nбэкап: {backup}\nдальше: перезапуск службы пуллера — не во время заливки")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
