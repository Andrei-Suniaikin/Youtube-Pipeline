---
name: VideoStudio.exe build — Lenovo PyInstaller onefile
description: Где и как собирается desktop VideoStudio.exe (legacy app_main_new.py). Build-набор живёт на Lenovo, не локально.
type: tool
date: 2026-05-09
originSessionId: redacted
---
> Санитизированная историческая справка. Адреса ниже относятся к TEST-NET, команды доступа
> намеренно нерабочие, а фактический build host при подготовке пакета был недоступен.

## TL;DR
Desktop-приложение `VideoStudio.exe` (legacy `app_main_new.py` + воркеры) собирается **только на Lenovo PC**.
Локально в `~/Downloads/Telegram Lite/софт/` build-скриптов НЕТ. Не путать с серверным task_runner (который subprocess-driven и без сборки).

## Местоположение на Lenovo
Формат доступа: `ssh Builder@192.0.2.12` (пример; адрес и учётная запись выдаются отдельно).
Второй маршрут в WSL существовал исторически, но его адрес также не включён в пакет.

Папка сборки:
```
C:\Users\Builder\Downloads\Telegram Desktop\Новая папка\
```

Содержимое:
| Файл | Размер | Роль |
|---|---|---|
| `BUILD_NOW.bat` | 112 B | точка входа, вызывает `python build_helper.py` |
| `build_helper.py` | 9 407 B | реальная сборка через PyInstaller |
| `portable_paths.py` | 9 081 B | missing-import из `app_main_new.py` / `render_worker.py` |
| `license_guard.py` | — | HWID-проверка (тоже missing локально) |
| `logger_config.py` | — | логгер (тоже missing локально) |
| `app_main_new.py` + воркеры | — | версии от 11 апреля 2026 |
| `bin/ffmpeg.exe`, `bin/ffprobe.exe`, `bin/yt-dlp.exe` | — | встраиваются через `--add-data` |
| `RELEASE_FINAL/VideoStudio.exe` | ~400 MB | output, единственный exe |

## Что делает build_helper.py
1. Проверяет наличие `app_main_new.py` + воркеров (`render`/`googler`/`voicer`/`tts`/`edgetts`/`image`/`youtube_slide_extractor`/`i18n`/`logger_config`).
2. Проверяет deps: `PIL, cv2, numpy, imagehash, edge_tts, icrawler`.
3. Ставит PyInstaller если нет.
4. Очищает старые `RELEASE_FINAL/`, `build/`, `dist/`.
5. Запускает PyInstaller:
   ```
   --noconsole --onefile --name VideoStudio
   --hidden-import: PIL, cv2, requests, numpy, imagehash, tkinter, i18n,
                    logger_config, portable_paths, license_guard,
                    tts/edgetts/voicer/render/image/googler/youtube_slide_extractor,
                    imageio_ffmpeg, six
   --collect-all: langdetect, edge_tts, whisper, icrawler
   --distpath RELEASE_FINAL
   --add-data bin/ffmpeg.exe;bin
   --add-data bin/ffprobe.exe;bin
   --add-data bin/yt-dlp.exe;bin
   app_main_new.py
   ```
6. Чистит `build/`, `dist/`, `*.spec`, удаляет .py из `RELEASE_FINAL/`.
7. Печатает размер `VideoStudio.exe`.

## Архитектура desktop-версии (отличие от серверной)
`app_main_new.py` запускает воркеры **in-process** через `run_worker_inprocess()` (см. line ~237):
- импортирует модуль воркера
- подменяет `sys.argv = [...]`
- вызывает `main()` модуля

Это противоположность серверному `task_runner.py`, который запускает воркеры через `subprocess.run([VENV_PYTHON, worker_path, ...])`. Поэтому код воркеров двойной таргет: на серверах — CLI-скрипт, в desktop — модуль с `main()`.

## Архив старых батников
`Telegram Desktop\Архив\` — ~30 батников за январь-февраль 2026: `FIX_*`, `PARALLEL_BUILD.bat`, `КОПИРОВАТЬ_В_RELEASE_FINAL.bat`. Старые fix-скрипты, для текущей сборки не нужны. `PARALLEL_BUILD.bat` запускал `FIX_VIDEOSTUDIO_NOW.bat` в отдельном окне.

## Как пересобрать
1. SSH на Lenovo (Windows host или WSL).
2. Открыть Windows cmd / PowerShell в `C:\Users\Builder\Downloads\Telegram Desktop\Новая папка\`.
3. Запустить `BUILD_NOW.bat` (или `python build_helper.py` напрямую).
4. Ждать 3-5 минут. Output: `RELEASE_FINAL\VideoStudio.exe`.

## ⚠️ КРИТИЧНО: Локальная папка `софт/` ≠ Lenovo build-папка
**Файлы в `/Users/owner/Downloads/Telegram Lite/софт/` НЕ являются source-of-truth для desktop build.**
Это устаревшие копии (часто от марта-апреля 2026). Источник правды для desktop VideoStudio.exe — папка на Lenovo:
`C:\Users\Builder\Downloads\Telegram Desktop\Новая папка\`

**Перед патчингом desktop-воркеров (`voicer`/`googler`/`render`/etc.) ОБЯЗАТЕЛЬНО:**
1. `scp 'Builder@192.0.2.12:C:\Users\...\<file>.py' ./builder_sync/<file>.py` — пример pull актуальной версии после выдачи доступа
2. Применить правки на pulled-копии
3. py_compile + статический grep
4. scp обратно
5. `BUILD_NOW.bat`

**НЕ редактируй `софт/<worker>.py` напрямую и НЕ scp его на Lenovo** — затрёшь свежие версии с потерей функционала. Локальный `софт/` годится только для чтения референса.

Серверные воркеры (Mac/Minsk/Lenovo task_runner) — это **третья** ветка, тоже отдельная от desktop. Не путать.

## Gotchas
- Build идёт только под Windows (PyInstaller specifics + .exe для ffmpeg/ffprobe/yt-dlp).
- `license_guard.py` делает HWID-привязку — собранный exe работает только на той машине где запускался первый раз без перепрошивки лицензии.
- Три источника воркеров: (1) desktop build-папка Lenovo, (2) серверные task_runner, (3) локальная `софт/` устаревшая. Перед правкой — определить какой targeted.
- **`bin/{ffmpeg,ffprobe,yt-dlp}.exe` ОБЯЗАТЕЛЬНЫ в build-папке** для self-contained exe (~430 MB final). Без них build_helper warning'нёт, скипнет `--add-data`, exe соберётся "пустой" (280 MB) — при первом render у юзера упадёт. Источник бинарей (на Lenovo): `Telegram Desktop\Архив\bin\ffmpeg.exe` (~195 MB), `Архив\bin\ffprobe.exe` (~195 MB), `Архив\yt-dlp.exe` (~18 MB, новее чем в `Архив\bin\`). LZMA в PyInstaller сжимает 410 MB raw → +150 MB к exe.
- **subprocess cmd-всплывайки на Windows.** При вызове внешних .exe из Python (ffmpeg, yt-dlp, etc.) на Windows без `creationflags=CREATE_NO_WINDOW` ИЛИ `startupinfo=STARTUPINFO(SW_HIDE)` — выскакивает чёрное cmd-окно на каждый запуск. В этом проекте паттерн: константа `_SUBPROCESS_HIDE_WINDOW` (dict с creationflags + startupinfo) разворачивается через `**_SUBPROCESS_HIDE_WINDOW` в kwargs subprocess.run/Popen. **При правках воркера: грепнуть `subprocess\.(run|Popen)` vs `_SUBPROCESS_HIDE_WINDOW` — счётчики должны совпадать.** Исторически в `youtube_slide_extractor.py` 8 init-точек (yt-dlp version/update probes) пропускали const → тестер видел 8 окон при каждом запуске extractor'а.

## TODO (если понадобится)
- Перенести build-набор в локальный git репо (чтобы править portable_paths.py / license_guard.py локально + push на Lenovo для пересборки).
- Документировать что именно делает `license_guard.py` (HWID-алгоритм).
