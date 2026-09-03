# Известные пробелы и вопросы до оценки сроков

## Что не удалось подтвердить при подготовке

1. **Windows build.** Сборочная машина была недоступна. Не сняты exact entrypoint, PyInstaller
   spec/helper, embedded binary versions и SHA последнего EXE.
2. **Render-нода.** Отдельная машина была недоступна. Версия реально исполняемого
   `render_worker.py`, puller и runtime не подтверждена.
3. **Upload runtime.** В архиве есть current working-tree modules и server contracts, но не
   полный снимок активных профилей/cookies/машин; эти данные намеренно не должны попадать в ZIP.
   Локальные render/upload pullers приложены только как unverified protocol reference.
4. **Dependency locks.** Текущие сайты не воспроизводятся из одного pinned lockfile.
5. **Source revisions.** Production Rewrite/Cover не имеют надёжного git revision marker;
   локальные репозитории отстают или опережают отдельными незакоммиченными файлами.
6. **External contracts.** Provider APIs, модели и лимиты меняются; перед реализацией каждого
   adapter нужен свежий contract check и официальный test account.

## Подтверждённые несоответствия

- Локальный VSGen `main.py` короче production и не является источником publication runtime.
- Production и local Rewrite различаются; generation использует дополнительные dynamic modules.
- Production и local Cover различаются: queue pulse/events есть на сервере, более свежая log
  sanitization — только локально. Локальный UI и backend уже имеют несовпадающий endpoint.
- Rewrite UI обещает 30 дней хранения, cleanup default — 7 дней.
- Publication `done` сейчас означает verified schedule, а UI формулирует это как публикацию.
- `pub_hosts`/`pub_profiles` пока schema-only: полноценного agent/profile registry нет.
- Cover partner API и UI не полностью совпадают по режиму seed и batch admission.
- Межсервисный create не имеет общего idempotency key; crash-window может создать дубль.
- Прямое чтение соседних SQLite/presets связывает services неявным schema contract.
- Desktop GUI upload entrypoint обходит уже существующие durable receipt/profile-lease contexts;
  строгий upload contract есть в modules, но не доказан в основном пользовательском flow.
- Локальный upload puller продолжает загрузку после недоставленной critical phase и не доказывает
  сквозное включение coordinator lease/dual-host guard; активная edge-версия не снята.
- VSGen auto-render сейчас допускает 70% изображений; это legacy render policy, а не полная
  готовность media stage.
- Current `_reslot` меняет slot того же release; целевой contract требует новую immutable
  schedule revision, но такой сущности в legacy schema пока нет.
- Publication adapter Cover пишет `cover.jpg` неатомарно и валидирует в основном размер/наличие.

## Решения, которые должен принять владелец с разработчиком

1. Нужны ли и Desktop, и web как равноправные продукты, или Desktop становится thin client?
2. Нужен ли настоящий offline mode Windows, и какие стадии должны работать без сервера?
3. Что считается бизнес-финишем: `scheduled`, `PUBLIC` или ещё просмотры/страйки через N часов?
4. Какие языки, TTS/image/video providers и режимы являются обязательными в v1?
5. Входит ли semantic montage длинных donor-видео в первый release новой платформы?
6. Нужна ли partner API совместимость для Rewrite/Cover и какие клиенты реально существуют?
7. Какие роли нужны: owner, operator, tenant admin, creator, read-only auditor?
8. Сколько дней хранить source, промежуточные media, final, logs и audit?
9. Каков месячный cost ceiling по tenant/provider и кто подтверждает дорогой retry?
10. Какой объём/пиковая нагрузка ожидается: releases/day, concurrent renders, upload slots?
11. Где должна жить платформа и media: один VPS, managed DB/object storage или on-prem?
12. Какие лицензии/права есть на donor text/video/images и что должно проверяться автоматически?
13. Нужна ли ручная creative approval для script/cover/video перед загрузкой?
14. Как переносить существующих users/projects: всё, последние N дней или только активные?
15. Кто владеет 24/7 операционными alerts и каким является допустимый простой?

## Что запросить после архитектурной оценки

Только когда разработчик объяснил модель и предложил минимальный canary:

- read-only diff Windows build против snapshot;
- read-only inventory render/upload agents и их versions;
- sanitized config-name inventory;
- synthetic/test provider credentials с quotas;
- один изолированный tenant и test channel;
- representative anonymized failure receipts/log events;
- фактические SLO/cost/volume числа владельца.

Не нужно заранее передавать production DB, `.env`, API keys, browser profiles или пользовательские
media. Они не помогают понять архитектуру и создают лишний риск.
