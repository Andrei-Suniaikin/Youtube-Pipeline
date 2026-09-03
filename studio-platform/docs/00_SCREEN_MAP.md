# Карта: экран → эндпоинт → таблица

Этап 0.2. Инвентарь того, что есть сегодня, и куда каждый кусок переезжает в единый пульт.

Источники: `live_2026-09-02/кабинет/*.py` (разбор роутов по коду),
`Studio_Full_Handoff_2026-08-31/contracts/openapi/*.json` (Rewrite и Cover — только контракты,
их код в живой срез не вошёл), `contracts/schema/vsgen.schema.sql` (30 таблиц).

## Сводка

| | Роутов | Из них страниц | Экранов для пользователя |
|---|---|---|---|
| VSGen (кабинет) | **102** | 18 | 11 |
| Rewrite | 40 | — | 4 вкладки |
| Cover | 42 | — | 3 вида |
| **Итого** | **184** | | **~18** |

Расхождение с OpenAPI-снимком (99 путей у VSGen против 102 найденных в коде) — разные
единицы счёта: OpenAPI считает пути, разбор кода считает декораторы, часть путей имеет
и GET, и POST.

---

## A. VSGen — 18 страниц

| Страница | Файл:строка | Строк | Таблицы | Переезжает в |
|---|---|---|---|---|
| `/` | `main.py:3851` | 224 | projects, presets, queue_tasks | **Выпуски** |
| `/p/{pid}` | `main.py:4470` | **872** | presets, queue_tasks, voice_lib | **Выпуски** (карточка) |
| `/queue` | `main.py:13678` | 174 | queue_tasks, gen_jobs, projects, users | **Очередь** |
| `/keys` | `main.py:14452` | 271 | — | **Ключи** |
| `/server-limits` | `main.py:14888` | 67 | — | **Ключи** |
| `/presets` | `main.py:12063` | 31 | presets | **Пресеты** |
| `/presets/{prid}/edit` | `main.py:12109` | 146 | presets, voice_lib | **Пресеты** |
| `/voices` | `main.py:14252` | 47 | voice_lib | **Пресеты** (по ТЗ раздел удаляется, голос уходит в пресет) |
| `/channels` | `pub_ui.py:533` | 288 | pub_channels, pub_tags, pub_channel_tags, pub_releases | **Каналы** |
| `/channels/new` | `pub_ui.py:821` | 21 | — | **Каналы** |
| `/channels/import` | `pub_ui.py:842` | 24 | pub_channels | **Каналы** |
| `/channels/{cid}` | `pub_ui.py:866` | 10 | — | **Каналы** |
| `/releases/{id}/log` | `pub_ui.py:876` | 57 | pub_releases, pub_release_events, pub_upload_jobs, pub_channels | **Выпуски** (лента этапа) |
| `/login` | `main.py:2333` | 7 | — | **Вход** |
| `/admin` | `main.py:16961` | 86 | admin_state | **Админ** |
| `/admin/legacy` | `main.py:15822` | 78 | gen_jobs, users | **Админ** |
| `/admin/user/{uid}` | `main.py:15900` | 34 | gen_jobs, users | **Админ** |
| `/admin/job/{jid}` | `main.py:15934` | 44 | gen_jobs, users | **Админ** |

`/p/{pid}` — 872 строки одной функции с вкладками `form`, `img`, `vid`, `rnd`. Это самый
тяжёлый экран системы и главный кандидат на разбор.

### API-роуты VSGen по группам

| Группа | Роутов | Таблицы | Переезжает в |
|---|---|---|---|
| `api/projects` | 21 | projects, queue_tasks, presets, card_jobs | Выпуски |
| `api/jobs` | 8 | queue_tasks, gen_jobs, job_logs, card_jobs | Выпуски + Очередь |
| `api/render` | 7 | queue_tasks, gen_jobs, projects, job_logs | Выпуски (этап «Сборка») |
| `api/batch` | 5 | projects, card_jobs | Выпуски |
| `api/presets` | 5 | presets, projects | Пресеты |
| `api/channels` | 5 | pub_channels, pub_tags | Каналы |
| `api/upload` | 5 | pub_upload_jobs, pub_releases, pub_release_events | Выпуски (этап «Публикация») |
| `api/cards` | 3 | card_jobs, gen_jobs, projects, provider_rr_cursor | Выпуски |
| `api/queue` | 2 | queue_tasks, job_logs | Очередь |
| `api/plan` | 2 | pub_plan_items | Каналы (календарь) |
| `api/voices` | 2 | voice_lib | Пресеты |
| `api/my`, `api/me` | 3 | users, gen_jobs | Вход / Ключи |
| `admin/*` | 12 | admin_state, admin_audit, jobs, gen_jobs, job_logs | Админ |

`api/upload` — контракт с агентом заливки: `claim`, `assets`, `file`, `progress`, `result`.
Это уже готовый протокол «пульт ↔ исполнитель», образец для остальных движков.

---

## B. Rewrite (40) и Cover (42) — по контрактам

| Rewrite | Путей | Переезжает в |
|---|---|---|
| `/jobs/*` (rewrite, generation, статусы) | 12 | Выпуски, этап «Сценарий» |
| `/api/*` (queue/pulse, v1/languages, v1/files) | 10 | Очередь, Выпуски |
| `/auth/*` | 7 | Вход (единая авторизация) |
| `/presets/*` + `/settings/presets` | 4 | Пресеты |
| `/subs/*` (fetch, download) | 2 | Выпуски — источник «ссылка на YouTube» из ТЗ §05 **уже есть на сервере**, не хватает только формы |
| `/settings/keys` | 1 | Ключи |
| `/models`, `/languages`, `/upload`, `/` | 4 | Выпуски |

| Cover | Путей | Переезжает в |
|---|---|---|
| `/auth/*` | 10 | Вход |
| `/jobs/*` | 10 | Выпуски, этап «Обложка» |
| `/api/*` (queue/pulse, v1/presets) | 8 | Очередь, Пресеты |
| `/favorites/*` | 4 | Выпуски (выбранный вариант обложки) |
| `/presets/*`, `/prompts/*` | 4 | Пресеты |
| `/api-keys/*`, `/usage` | 3 | Ключи |
| `/admin/gen-settings`, `/languages`, `/` | 3 | Админ / Выпуски |

---

## C. Целевые семь экранов

### 1. Выпуски
Список + карточка выпуска (вертикальный степпер из семи стадий по ТЗ §14).

Собирается из: `/`, `/p/{pid}`, `/releases/{id}/log`, `api/projects`, `api/jobs`,
`api/render`, `api/batch`, `api/cards`, `api/upload`, Rewrite `/jobs/*` + `/subs/*`,
Cover `/jobs/*` + `/favorites/*`.

Таблицы: `pub_releases`, `pub_release_jobs`, `pub_release_events`, `pub_release_log`,
`pub_upload_jobs`, `projects`, `queue_tasks`, `gen_jobs`, `card_jobs`, `job_files`, `job_logs`.

Готовые помощники для переиспользования: `pub_ui.release_steps()` уже строит цепочку
этапов, `release_look()` — статус и цвет, `release_state()` — состояние выпуска.

### 2. Каналы
Карточки по статусу + календарь + история + пул ссылок.

Собирается из: `/channels*`, `api/channels`, `api/plan`.
Таблицы: `pub_channels`, `pub_tags`, `pub_channel_tags`, `pub_plan_items`, `pub_profiles`, `pub_hosts`.
Помощники: `channel_tile()`, `schedule_text()`, `per_day()`, `tile_menu()`.

### 3. Очередь
Сквозная по трём сервисам, фильтры и действия в строке.

Собирается из: `/queue`, `api/queue`, Rewrite `/api/queue/pulse`, Cover `/api/queue/pulse`.
Таблицы: `queue_tasks`, `gen_jobs`, `job_logs`.

### 4. Ключи
Собирается из: `/keys`, `/server-limits`, `api/server`, Rewrite `/settings/keys`,
Cover `/api-keys` + `/usage`.
Таблицы: `users.keys`, `users.key_opts`, `provider_rr_cursor`, `tenants.daily_quota`.

### 5. Пресеты
Собирается из: `/presets*`, `/voices`, `api/presets`, `api/voices`, Rewrite `/presets*`,
Cover `/presets*` + `/prompts*`.

### 6. Админ
Собирается из: `/admin*` (12 роутов), Cover `/admin/gen-settings`.

### 7. Вход
Собирается из: `/login`, Rewrite `/auth/*` (7), Cover `/auth/*` (10).

---

## D. Что схлопывается

Пять понятий существуют в трёх экземплярах. Три из них хранятся ещё и по-разному:

| Понятие | VSGen | Rewrite | Cover |
|---|---|---|---|
| **Очередь** | таблица `queue_tasks` | своя БД `rewrite_job_queue` (5 таблиц) | своя БД `cover_job_queue` (3 таблицы) |
| **Пресеты** | SQL-таблица `presets` | **колонка JSON** `user_keys.rew_presets` | **файл на диске** `presets/<uid>/presets.json` |
| **Ключи** | `users.keys` + `provider_rr_cursor` | `/settings/keys` | таблица `api_keys` |
| **Пользователи** | `users(id INTEGER, token, tenant_id)` | `users(username TEXT PK, license_key)` | то же, что Rewrite |
| **Языки** | — | `/languages` | `/languages` |

Пресеты — самый показательный случай: одно понятие, три способа хранения. Именно поэтому
`pub_neighbors.py` лезет в чужие SQLite напрямую (`sessions.db → user_keys.rew_presets`,
`presets/<uid>/presets.json`) — по HTTP их взять неоткуда. 117 строк, лечится двумя
эндпоинтами `GET /api/presets`.

---

## E. Пробелы: чего нет ни в одном экране

Пишется с нуля, готового кода нет:

| Что | Данные есть? | Из ТЗ |
|---|---|---|
| **Календарь канала** | да: `pub_channels.schedule`, `tz`, `horizon_days`, `lead_days`, `pub_plan_items.slot_at` | §13 |
| | *проверено: рендера календаря нет нигде, расписание показывается строкой через `schedule_text()`, а `/channels/{cid}` — это карточка, не календарь* | |
| **Скачивание субтитров и тайм-кодов** | файлы создаются, в списке шаблонов их нет (`main.py:4677`) | §09 |
| **Телеграм-тревоги**, 6 поводов | нет | §13 |
| **Срок хранения и уборка проектов** | нет | §03/№12 |
| **Журнал платных вызовов** `provider_calls` | нет | §03/№4 |
| **Поздняя проверка «ролик стал публичным»** | нет | §11 |
| **Честная картина по ключам** (занято из лимита, ожидание, время сброса) | частично: `provider_rr_cursor` | §12 |

---

## Что дальше

Экран выпуска (0.3) строится первым, потому что через него проходит вся вертикаль:
семь стадий, три сервиса, десять таблиц. Если он собирается — остальные шесть экранов
это подмножества уже решённой задачи.
