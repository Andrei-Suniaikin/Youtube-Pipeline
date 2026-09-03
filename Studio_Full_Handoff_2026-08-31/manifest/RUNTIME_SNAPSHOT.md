# Runtime snapshot без секретов

Дата последней проверки: 2026-08-31.

| Service | systemd unit | Loopback | Working directory role |
|---|---|---:|---|
| VSGen | `vsgen.service` | 8800 | VideoStudio server/control plane |
| Rewrite | `rewrite-server.service` | 8899 | Rewrite application |
| Cover | `cover-server.service` | 8900 | Cover application |
| Reverse proxy | `caddy.service` | 80/443 | TLS и маршрутизация hostnames |

VSGen и Caddy были active; VSGen `/healthz`, `/openapi.json` и `/login` отвечали HTTP 200.
Route sets трёх приложений повторно сверены с `contracts/openapi`. Это route inventory,
а не нагрузочная или disaster-recovery проверка.

Production Python: 3.10.12.

- VSGen/Rewrite environment: FastAPI 0.135.1, Uvicorn 0.41.0, Pydantic 2.12.5,
  requests 2.34.2, Pillow 12.2.0.
- Cover environment: FastAPI 0.139.2, Uvicorn 0.51.0, Pydantic 2.13.4,
  OpenAI 2.46.0, requests 2.34.2, Pillow 12.3.0.

Три сайта работают на одной production machine, но в разных environments. Точные hostnames и
адреса санитизированы. Unit/Caddy credentials и `.env` не копировались.

VSGen и Rewrite используют общий system Python без venv. Cover использует отдельный venv.
Полный `pip list` обоих environments находится в `config/runtime/`; это снимок, не lockfile.
VSGen и Rewrite реально запущены от root, а безопасные systemd-файлы в `config/systemd/`
показывают рекомендуемую форму с отдельным пользователем и hardening, не побайтовую копию unit.

Основные persistent stores: одна VSGen SQLite DB, две Rewrite DB и две Cover DB плюс project,
uploads/outputs/presets folders. В пакете только DDL. DB quick checks были read-only; backup и
restore rehearsal в рамках подготовки ZIP не выполнялись.

На 31.08 два Rewrite allowlist-параметра в env-файле отличались от окружения уже работающего
процесса. Значения исключены. До миграции нельзя считать on-disk конфиг активным: нужен
контролируемый restart и повторная проверка доступа/health.
