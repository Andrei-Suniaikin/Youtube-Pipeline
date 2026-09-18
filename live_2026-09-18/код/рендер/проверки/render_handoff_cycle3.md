# Минск C3: auto_sound parity

Отдельный candidate `render_cycle3_candidate/render_worker.py`, поверх frozen C2 worker `22d7774bf73e155321ad4acca496f2303ff8a7c3c23ef6050c4e72fa6bb09921`. Новый SHA256 `52583515486577c7a8310a6f16f9f660280655796fa977b3957954d02d4b5945`. Frozen C2 файлы не менялись; production не менялся. Manifest `render_cycle3_manifest.json`, diff `render_cycle3.diff`.

Изменения:

- Перенесены существующие local `_load_sound_plan` / `_apply_auto_sound` и вызов после основного background mix, перед audio ведущих video clips. Фоновые слои, fade на границах, sidechain ducking по чистой озвучке, PCM WAV перед финальным AAC.
- Применяется только при явном JSON `auto_sound:true` в render config. Выключение галки перестаёт применять старый лежащий sound_plan. Root должен добавить bool `auto_sound` в `_render_config_from_params`; server ownership/byok и offload подготовки плана находятся в root scope.
- **Доказанный baseline bug исправлен:** `_RENDER_MEDIA` исключён из recursive сканирования основной озвучки. Ранее `sfx/bed.mp3` попадал в narration concat: реальная fixture 6 сек речи + 2 сек bed давала 8.07 сек итогового аудио даже с эффектом off. Фон/intro/end берутся своими специальными путями, а не как абзацы.

`python3 research/launch_hardening_20260914/render_verify_autosound_cycle3.py` — PASS на настоящем renderer CLI/FFmpeg/ffprobe. Два итоговых MP4 по 6.000 сек с video+audio, 256×144/5fps (маленький функциональный smoke). PCM спектр 330Hz bed: off 0.00005394, on 0.01210812 (>224×); narration440Hz 0.05675676→0.05675740, громкость речи сохранена. Лог on подтверждает mix, off не содержит mix. Проверены чтение relative plan, malformed/missing files, clamp времени к длине речи. Результат `render_verify_autosound_cycle3_result.json`, лог рядом.

По существующему контракту autosound — best effort: отсутствующий/испорченный план или ошибка микширования дают warning и сохраняют основную дорожку. Эта политика локальной функции не ужесточалась; server preflight/готовность плана — root. Нет вызова сети или AI на Минске. Server уже передаёт `_RENDER_MEDIA` в tar, а `auto_sound.prepare` пишет basename файлов в план.

Deployment root: после server C2 и добавления config bool сверить применённый C2 SHA, backup worker, atomic replace на `/opt/video_agent/render_worker.py`; перед новым render процессом нужен завершённый drain старых процессов. Если ставить C2+C3 одним пакетом, worker сразу брать отсюда, puller/delivery — approved C2. Далее повторить tiny actual smoke на ноде. Rollback только worker к C2 возвращает отсутствие mix и дефект bed-as-narration, поэтому отключить autosound до forward fix при откате. C2 delivery protocol/receipts не менялись.

Для целевой производительности требуется отдельный C4: 3 параллельных реальных 90-минутных render с 300/400/500 настоящими картинками, 1080p30, KB/fade. Этот smoke доказывает только включение/выключение/корректную длительность и звук, не throughput.
