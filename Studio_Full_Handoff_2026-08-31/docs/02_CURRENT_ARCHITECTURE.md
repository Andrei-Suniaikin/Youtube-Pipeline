# Текущая техническая архитектура

## Компоненты

| Компонент | Runtime | Состояние | Основная роль |
|---|---|---|---|
| Desktop VideoStudio | Python + Tkinter + PyInstaller onefile | локальные JSON и папки проектов | ручная/локальная сборка роликов |
| VSGen | FastAPI, systemd `vsgen`, localhost:8800 | SQLite `vsgen.db` + project filesystem | web-проекты, queue, admin, remote render, publication plane |
| Rewrite Studio | FastAPI, systemd `rewrite-server`, localhost:8899 | `sessions.db`, `job_queue.db`, uploads/outputs | генерация и рерайт текстов |
| Cover Studio | FastAPI, systemd `cover-server`, localhost:8900 | `cover_sessions.db`, `cover_job_queue.db`, outputs | генерация обложек |
| Reverse proxy | Caddy | TLS/routes | три публичных host → три localhost-порта |
| Render puller | Python на отдельной машине | claim/progress/result в VSGen | забирает assets и возвращает MP4 |
| Upload agent | Python + Selenium/CDP + AdsPower | local receipts + server publication state | загрузка и проверка YouTube |

На production VPS используется Python 3.10.12. VSGen/Rewrite и Cover работают в разных
Python-окружениях и имеют разные версии FastAPI/Uvicorn/Pydantic. Это ещё одна причина не
считать машину единым воспроизводимым runtime.

## VSGen

`source/vsgen_server/main.py` — большой FastAPI-монолит. В нём находятся HTML, auth,
пользователи, проекты, presets, scheduler, provider pools, worker command builder, admin и
много API endpoints.

Новые модули `pub_*` вынесли часть publication domain из `main.py`:

- `pub_schema.py` — каналы, план, releases, upload jobs и events;
- `pub_ui.py` — web routes и формы;
- `pub_engine.py` — продвижение release по стадиям;
- `pub_services.py` — адаптеры к проектам, Rewrite, Cover и артефактам;
- `pub_upload_api.py` — claim/result протокол upload agent;
- `pub_remote.py`, `pub_neighbors.py` — соседние сервисы и remote operations;
- `pub_log.py`, `pub_tags.py`, `pub_publish.py` — журнал, планирование и публикация.

В production-снимке эти модули реально импортируются, routes регистрируются, а publication
engine запускается из lifespan. Это не просто архивный WIP. При этом они всё ещё разделяют
процесс и БД с монолитом VSGen и вызывают соседние сервисы как отдельные системы.

### Основные таблицы VSGen

- legacy/API: `users`, `jobs`, `gen_jobs`;
- web projects: `tenants`, `sessions`, `projects`, `presets`, `voice_lib`;
- execution: `queue_tasks`, `job_logs`, `job_files`, `provider_rr_cursor`, `card_jobs`;
- admin: `admin_state`, `admin_audit`;
- publication: `pub_hosts`, `pub_profiles`, `pub_channels`, `pub_tags`,
  `pub_channel_tags`, `pub_plan_items`, `pub_releases`, `pub_release_events`,
  `pub_release_log`, `pub_upload_jobs`, `pub_release_jobs`.

Точная DDL находится в `contracts/schema/vsgen.schema.sql`.

### Выполнение jobs

Новая задача создаёт строку `gen_jobs` и `queue_tasks`. Scheduler определяет реального
провайдера из kind и параметров, применяет server/BYOK lane, fair scheduling и provider
capacity. Claim пишет `claim_token` и started time. Локальные стадии запускаются как
подпроцессы Python-воркеров. Render не выполняется локально: его claim-ит remote puller.

Провайдеры имеют независимые лимиты, но некоторые kind делят один реальный аккаунт. Например,
картинки и видео через один gateway должны делить его capacity независимо от названия stage.

## Rewrite Studio

`server.py` содержит HTTP/UI и orchestration; `rewrite_core.py` — LLM pipeline;
`job_queue.py` — durable queue; `api.py` — versioned REST API; `auth.py`/`api_auth.py` —
login/session/API-key.

Очередь имеет provider groups OpenAI, Claude и Codex. Dispatcher ограничивает общую и
provider-specific concurrency, делает round-robin по пользователям и FIFO внутри пользователя.
`run_token` защищает от stale writers. Cancel персистентный; interrupted jobs восстанавливаются.

Рерайт хранит chunk state, позволяет продолжать незавершённые части, проверяет структуру
абзацев и язык. Public log проходит allowlist-фильтр, полный технический лог остаётся на сервере.

## Cover Studio

`cover_server.py` содержит web/API и orchestration; `cover_core.py` — анализ, prompts и
генерация; `job_queue_cover.py` — durable fair queue; `provider_policy.py` сопоставляет UI
provider реальному credential/capacity group.

Есть три пользовательских режима:

- `url`: YouTube URL → thumbnail/title → анализ → локализованные обложки;
- `title`: названия по языкам + extra prompt;
- `seed`: подготовленное семя по языкам → несколько вариантов.

Job может завершиться `completed_partial`. Fallback-обложка без текста не считается полным
успехом; resume повторяет только пропущенное или fallback. Favorites хранят выбранные файлы
отдельно от временных результатов.

## Desktop VideoStudio

`app_main.py` — Tkinter-оркестратор. Тяжёлые стадии реализованы отдельными CLI modules, но
в onefile-сборке могут запускаться in-process с глобальной подменой `sys.argv` и stdout. Из-за
глобального lock это сериализует embedded workers и создаёт отличия от subprocess-режима.

Папка проекта одновременно является artifact store и частью state machine. GUI хранит часть
настроек в home JSON-файлах. Render/TTS queues Desktop не durable.

## Хранилище артефактов

Типичный проект содержит:

```text
project/
  source или внешний путь к source.txt
  _FINAL_AUDIO_EDGETTS/ или другой выбранный TTS-набор
  googler/
    scenes.json
    scenes.meta.json
    prompts.json
    NNN.jpg
    receipts/manifests
  googler video/
    NNN_video.mp4
  _ASSETS/
    reference bundles, story bible, render media
  render temp
  final_project_name.mp4
  upload_target.json / receipts
```

Имена и расположение менялись исторически. Новая система не должна определять authoritative
input только по рекурсивному содержимому этой папки.

## Авторизация и секреты

У трёх сайтов разные session/auth implementations. VSGen и Rewrite поддерживают server-owned
credential pools и пользовательские BYOK. Cover в основном использует серверные ключи.
Реальные credentials находятся в environment/config/закрытых DB-строках и не включены сюда.

Некоторые Claude/Codex аккаунты физически разделяются между сервисами. Отдельные schedulers
не являются полноценным глобальным quota manager: нагрузка одного продукта способна съесть
лимит соседнего.

## Деплой

VSGen, Rewrite и Cover запускаются systemd и проксируются Caddy. Деплой исторически делается
копированием файлов и restart конкретного сервиса. Production DB и outputs нельзя заменять
локальными копиями.

Desktop собирается PyInstaller только на Windows-машине; build scripts и бинарники FFmpeg,
FFprobe, yt-dlp не находятся в основном репозитории. Reproducible build пока отсутствует.

## Главные архитектурные долги

1. Три реализации auth, queue, quota и artifact lifecycle.
2. SQLite и filesystem как несколько конкурирующих источников истины.
3. Огромные modules с inline HTML и cross-cutting globals.
4. Нет одного versioned release contract, которому подчиняются все сервисы и agents.
5. Нет воспроизводимой цепочки commit → image/build → production.
6. Общие внешние аккаунты координируются не одним глобальным scheduler.
7. Browser automation не может быть полностью транзакционной и требует отдельной reconcile-модели.

