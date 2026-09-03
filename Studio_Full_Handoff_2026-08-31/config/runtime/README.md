# Снимок runtime 31.08.2026

`vsgen-rewrite.pip-list.txt` — exact sorted `pip freeze --all` общего системного Python,
которым реально запущены VSGen и Rewrite. Поэтому в нём есть пакеты ОС и утилиты,
не используемые приложениями.
Это forensic snapshot, а не рекомендуемый lockfile.

`cover.pip-list.txt` снят из отдельного production-venv Cover.

`current_env_names.example` содержит только обнаруженные имена переменных и `PLACEHOLDER`.
Production-значения, ключи и allowlist идентификаторов в пакет не включены.
