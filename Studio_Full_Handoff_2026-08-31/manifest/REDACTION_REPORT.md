# Отчёт о санитизации

Перед упаковкой source/manual snapshots изменены только для безопасной передачи. Санитизация
не является production patch и дополнительно подтверждает, почему source нельзя deploy-ить
напрямую.

Заменены безопасными примерами или маркерами:

- production/public/Tailscale addresses и временные hostnames;
- личные Unix/Windows usernames и contact email;
- hardcoded admin tenant IDs и реальные identifiers в UI/операционных примерах;
- исторический expired provider key sentinel;
- приватный license whitelist URL и embedded cache HMAC;
- owner-specific TTS template/voice resource IDs;
- исторический SSH password и session provenance ID.

Дельта VSGen 31.08 применена к уже очищенному `main.py`, а не заменена raw-файлом. Проверка
сопоставила current raw с baseline raw и подтвердила ровно те же четыре redaction transforms.
В новом `tts_worker.py` tenant-specific default template UUID снова заменён маркером.

Исключены allowlist-ом:

- `.env`, key/config credential stores;
- SQLite DB, WAL/SHM, dumps;
- logs, tracebacks/evidence bundles;
- browser cookies, AdsPower profiles и sessions;
- user uploads/outputs/presets/favorites;
- videos, audio, images и compiled binaries;
- backups, git metadata, caches и virtual environments.

В `config/runtime/current_env_names.example` оставлены только имена переменных и
`PLACEHOLDER`; значения production env, allowlist IDs и effective secrets не читались и не
копировались. `pip-list` содержит только названия и версии установленных пакетов.

`tools/package_check.py` проверяет запрещённые файлы, real-looking network/account literals,
типовые token/key formats, credential literals Python, JSON, Python syntax и SHA-256. Проверка
не доказывает, что исторический код безопасен или корректен; она доказывает только свойства
передаваемого пакета, перечисленные выше.
