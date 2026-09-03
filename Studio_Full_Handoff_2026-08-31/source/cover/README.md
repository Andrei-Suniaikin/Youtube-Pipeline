# Cover Generator — генерация YouTube-обложек

Веб-сервис: пакетная генерация локализованных YouTube-обложек. Скачивает превью
оригинала, анализирует его vision-моделью, адаптирует текст на целевые языки и
генерирует новые обложки через Fast Gen (Nano Banana Pro / Flower / Grok / OpenAI),
VeoNonStop (Nano Banana Pro/2/2 Lite, ключ `veo_…` / env `COVER_VEO_KEY`) или
напрямую OpenAI gpt-image-2 low (~$0.006/шт, ключ OpenAI, без референсов).

## Установка

```
pip install -r requirements.txt
```

Vendor-ключи хранятся только в закрытом `.env` сервера и никогда не
принимаются от пользователя:

- **OpenAI API** — https://platform.openai.com/api-keys (анализ обложки, адаптация текста, проверка результата)
- **Fast Gen API** — https://fast-gen.ai (генерация картинок)
- **VeoNonStop** — `COVER_VEO_KEY` для соответствующих моделей

Пользователь входит существующим логином/паролем (REST-клиент использует
выданный `cvr_*` ключ самого сервиса). Общие vendor credentials не попадают
в браузер, payload, SQLite job queue или ответы API.

## Запуск

```
uvicorn cover_server:app --host 0.0.0.0 --port 8900
# или ./run_cover_server.sh, в проде — systemd unit cover-server.service
```

Веб-UI: `http://<host>:8900/` (вход по логину/паролю).
Партнёрский REST API: `/api/v1/*` — см. `docs/API_COVER_PUBLIC.md`.

### Одноразовая установка пароля

Если пароли скомпрометированы (разосланы в открытом виде), админ поднимает
аккаунтам флаг — после этого на экране входа работает ссылка «задайте новый
по своему логину»: человек вводит свой логин и новый пароль, сразу входит,
флаг гаснет (одно срабатывание на аккаунт), прежние сессии этого пользователя
убиваются.

```
# выдать (админской сессией): конкретным логинам или всем — {"all": true}
curl -X POST http://<host>:8900/auth/require-password-reset \
  -H "X-Session-Id: <admin_session>" -H 'Content-Type: application/json' \
  -d '{"usernames":["user_alpha","user_beta"]}'
# отозвать: добавить "cancel": true
```

Текущее состояние видно в `GET /auth/accounts` — поле `pw_reset_pending`.

## Режимы генерации (url-mode)

1. **Обычный** — vision-анализ превью (текст, шрифт, сцена, композиция, стиль),
   GPT-адаптация текста на язык, сборка промпта, генерация с нуля, vision-проверка текста.
2. **⚡ Простая генерация (img2img)** — превью отдаётся модели на редактирование:
   она сама переводит текст и слегка меняет позы. Без анализа и адаптации. Флаг `simple_mode`.
3. **🎨 Стиль-референс** — двухкартиночный style-transfer: смысл и текст из обложки,
   стиль (шрифт, палитра, атмосфера) из прикреплённого референса. Поле `reference_image`
   (file_id из `POST /api/v1/files`), сохраняется в пресете.

Есть также **title-mode** — генерация без YouTube-ссылки: сцена придумывается из
названия, текст на каждый язык задаётся вручную.

## Пайплайн (обычный режим)

```
Скачивание превью → GPT-vision анализ (текст/сцена/стиль)
→ GPT-адаптация текста на язык → сборка промпта
→ Fast Gen генерация → GPT-vision проверка текста → retry/фолбэк без текста
```

Все промпты редактируются per-user через UI («📝 Промпты») или лежат в
`default_prompts.json`.

## Очередь

`job_queue_cover.py` использует один work-conserving fair dispatcher:

- FIFO внутри одного пользователя и provider-группы;
- round-robin между пользователями;
- если пользователь один, он заполняет все доступные slots своего provider;
- новый пользователь получает ближайший освободившийся slot;
- насыщенный Veo/GPT/FastGen не занимает admission-slots других providers;
- старое поле `parallel` игнорируется.

Физические пределы задаёт только сервер: `COVER_JOB_SLOTS`,
`COVER_MAX_THREADS_PER_JOB`, `COVER_MAX_GENERATION_RETRIES`,
`COVER_MAX_PENDING` (технический pending-cap, по умолчанию 100),
`COVER_FASTGEN_JOB_SLOTS`, `COVER_VEO_JOB_SLOTS`,
`COVER_GPT_IMAGE2_JOB_SLOTS`,
`COVER_OPENAI_INFLIGHT`, `COVER_VEO_INFLIGHT`,
`COVER_GPT_IMAGE2_INFLIGHT`. FastGen дополнительно использует существующий
weighted key budget.

## Структура на сервере

```
/root/cover_tool/
├── cover_server.py       ← FastAPI: UI-endpoints + воркер очереди
├── api_cover.py          ← партнёрский REST /api/v1
├── cover_core.py         ← ядро: анализ, промпты, генерация (Fast Gen v6)
├── job_queue_cover.py    ← очередь задач (SQLite)
├── static/index.html     ← веб-UI
├── presets/<user>/       ← пресеты и промпты пользователей
├── uploads/<user>/       ← загруженные референсы
└── outputs/<user>/       ← готовые обложки
```

## Деплой

`./deploy.sh` — rsync с безопасным exclude-списком (не трогает `.env`, БД,
uploads/outputs/presets) + рестарт с гейтом «нет running-джоб».
