# VideoStudio

## Две реализации одного продукта

Desktop и web используют похожие Python-воркеры, но это не один runtime.

- Desktop: `source/desktop/src/app_main.py` управляет локальной папкой и GUI.
- Web: `source/vsgen_server/main.py` управляет пользователями, проектами и durable queue.
- Server workers: `source/vsgen_server/src/` — именно версии, установленные рядом с VSGen.
- Desktop workers: `source/desktop/src/` — текущая локальная ветка, не подтверждённый Windows build.

Связь Desktop с Web сейчас очень узкая: `server_client.run_worker` используется только как
опциональный bridge для двух Edge TTS paths. Проекты и pipeline не синхронизируются, а ошибка
bridge приводит к молчаливому локальному fallback. Это нельзя принимать за общий orchestration.

При переписывании сначала нужно определить общий stage contract, а потом подключить оба UI к
одному orchestration API. Дублировать pipeline в Desktop и web заново нельзя.

## Desktop entrypoints

Главные классы и функции `app_main.py`:

- `ProjectFrame` — состояние и действия одного проекта;
- `App` — окно, список проектов и массовые команды;
- `TTSQueueManager`, `RenderQueueManager` — очереди только в памяти;
- `run_worker_inprocess()` — запуск modules внутри onefile;
- `evaluate_audio_completeness()` — эвристическая проверка аудио перед рендером.

`ProjectFrame` запускает стадии: выбрать source, TTS, images, generated videos, render и upload.
Массовые команды просто вызывают те же стадии для нескольких frames.

## Что считается проектом

Desktop создаёт папку в доступном корне `video_projects`. Выбранный source-файл исторически
может остаться снаружи, а GUI хранит путь только в памяти. Пресет не превращает проект в
самодостаточный immutable manifest.

Настройки Desktop разнесены по JSON-файлам в home пользователя: presets, API keys, render,
Googler, themes, voices, channels, upload log и server settings. Реальные файлы с секретами
в архив не включены.

В новой реализации один versioned project/release manifest должен хранить:

- snapshot или content hash исходного текста;
- выбранные preset versions;
- точные параметры каждой стадии;
- входные fingerprints;
- authoritative artifact IDs/hashes;
- stage attempts и receipts.

## Озвучка

Поддерживаются четыре маршрута:

| Worker | Провайдер | Выход |
|---|---|---|
| `edgetts_worker.py` | Edge TTS | `_FINAL_AUDIO_EDGETTS/` |
| `tts_worker.py` | Lumean/template API | legacy: `_FINAL_AUDIO_ELEVENLABS/`; web: `_FINAL_AUDIO_LUMEAN/` |
| `voicer_worker.py` | Voicer/ElevenLabs wrapper | `_FINAL_AUDIO_VOICER/` |
| `voicegencore_worker.py` | VoiceGenCore | `_FINAL_AUDIO_VGC/` |

Файлы обычно нумеруются `1.mp3`, `2.mp3` и так далее; full-text создаёт один файл. У новых
маршрутов есть manifests/receipts и fail-closed обработка неоднозначного оплачиваемого create.

В актуальном web runtime Lumean выделен в отдельную provider/engine lane. Для маршрута HeyGen
воркер до платного create читает шаблон и требует точный `service_key=heygen`; результат не
смешивается с legacy ElevenLabs-каталогом. Сохранение активной формы как preset и применение
её к проекту выполняются в одной транзакции. Эти изменения относятся к VSGen 31.08 и не
доказывают, что Desktop EXE использует тот же flow.

Особенно важно различать:

- **create**: возможно платное и необратимое действие;
- **poll**: безопасное чтение статуса;
- **download/validate**: получение и проверка результата.

Если create завершился timeout после отправки запроса, новый create нельзя делать вслепую.
Нужен provider job ID или состояние manual recovery.

Desktop readiness оценивает ожидаемую длительность из длины текста и блокирует очевидно
короткое аудио. Это полезный guard, но не строгий контракт: невозможность измерить длительность
может пропустить проблему.

## План сцен и изображения

Есть два исторических режима.

### Bing

`image_worker.py` ищет изображения по tags или по сценам. Scene mode пишет `NNN.jpg` через
временный файл и `os.replace`. Однако частичный набор может вернуть exit code 0. Оркестратор
обязан сравнивать `expected scenes` и проверенные artifacts, а не верить exit code.

### AI/Googler

`googler_worker.py` содержит planning, image generation, regeneration, video generation,
reference pipeline, retries и reconciliation.

Авторитетная пара плана:

- `googler/scenes.json`;
- `googler/scenes.meta.json`.

Meta — commit marker: version, digest, count и конфигурация должны совпасть. Дополнительные
receipts фиксируют input fingerprint, provider, output SHA и состояние каждого кадра.
`reference_contract.py` определяет import, immutable revisions, bundle manifests и упаковку
референсов по сценам.

Хорошие идеи, которые нужно перенести:

- atomic publish плана и кадров;
- точные input fingerprints;
- lock на mutation image set;
- quarantine и rollback неполных замен;
- provider-specific terminal/transient exit codes;
- fail-closed preflight вместимости reference bundle.

Текущий VSGen имеет отдельную legacy policy: auto/deferred render может стартовать при
`IMG_RENDER_MIN_RATIO = 0.70`, то есть при наличии не менее 70% ожидаемых изображений.
Недостающие сцены затем компенсируются render fallback-логикой. Это действующее поведение,
но не доказательство полной media stage. В новой модели media остаётся `partial`, а разрешение
рендера с gaps хранится как явная versioned render policy с перечнем компенсаций.

## Generated video

Короткие клипы сопоставляются сценам по числовому префиксу и лежат отдельно от картинок.
Текущий pipeline умеет resume и сохраняет готовые clips по одному.

Опасный исторический edge case: список source images мог исключаться из рендера до того, как
соответствующий clip реально создан. При partial generation пропадала и картинка, и видео.

Новый контракт должен публиковать замену только атомарно:

```text
source image ID + valid clip ID + clip receipt → timeline substitution committed
```

## Real visual parser

`parser_worker.py` и пакет `parser/` — отдельный production-like экспериментальный контур
подбора реальных архивных/stock-визуалов. Он ищет media через source connectors, строит
contact sheets/clips, ранжирует CLIP и LLM judge, сохраняет attribution и публикует выбранные
кадры в формат, совместимый с `googler/NNN.jpg`. AI image может быть fallback, если реального
материала недостаточно.

Niche policy загружается из `parser/niches/{name}.yaml`; в snapshot включён текущий
`ww2.yaml`. Основной `app_main.py` этот parser напрямую не вызывает, поэтому его нельзя
считать обязательной стадией обычного Desktop flow. До переноса нужно решить scope и написать
явный adapter/contract, иначе второй visual pipeline снова будет жить рядом с первым неявно.

## Рендер

`render_worker.py` — FFmpeg pipeline. `VideoRenderer.render_video()`:

1. выбирает narration audio;
2. измеряет и склеивает его;
3. строит Whisper/scene alignment;
4. выбирает images/videos;
5. создаёт timeline, batches, transitions и overlays;
6. соединяет video/audio/subtitles;
7. пишет MP4 и machine-readable success/error marker.

Есть guards покрытия narration, восстановление длительности отсутствующих scenes, batching
FFmpeg, CPU/GPU tuning и проверка fragments.

Главная проблема — входы часто выбираются рекурсивным scan папки. Старый MP3 или media file
может попасть в новый render. Рендер должен принимать только immutable `render_manifest`:

```json
{
  "narration": [{"artifact_id": "...", "sha256": "..."}],
  "timeline": [{"scene": 1, "artifact_id": "...", "duration_ms": 4200}],
  "settings_version": "...",
  "expected_duration_ms": 123456
}
```

Финальный файл должен создаваться как temp, проходить `ffprobe`/duration/audio checks, fsync и
только затем атомарно становиться current artifact.

## Remote render

VSGen не обязан рендерить на VPS. Render job имеет отдельный claim protocol:

- agent claim-ит работу с token/lease;
- скачивает assets и config;
- отправляет progress/heartbeat;
- загружает result или terminal error;
- VSGen проверяет claim token и записывает artifact.

Версия render worker на каждой ноде должна публиковаться как capability/version. Нельзя
полагаться на одинаковое имя файла.

`source/desktop/tools/render_puller.py` показывает клиентскую половину текущего протокола:
streaming assets, claim headers, progress, transcript перед MP4, cancellation/409 и cleanup.
Это локальная reference-копия, не подтверждённый файл активной render-ноды.

## Upload и YouTube

Ключевые modules:

- `upload_manager.py` — адресная книга, project target и запуск worker;
- `upload_worker.py` — мастер загрузки;
- `yt_studio.py` — DOM registry и чтение machine state;
- `upload_contract.py` — phases/receipts;
- `upload_coordination.py` — coordination primitives;
- `chapters.py` — chapters/metadata.

Upload обязан:

1. проверить, что AdsPower-профиль не открыт ни на одной другой машине;
2. получить cross-host lease на весь browser session;
3. открыть прямой URL точного UC и проверить фактический канал;
4. пройти мастер в требуемом порядке;
5. сохранить durable phase до первого `send_keys` файла;
6. после этой границы никогда не делать blind re-upload;
7. при timeout искать exact draft/video row и продолжать reconcile/finalize;
8. проверить exact UC, video ID, title hash, visibility и timezone-aware scheduled epoch;
9. сохранить receipt и только затем завершить release.

Текст финального диалога YouTube не является доказательством. Доказательство — состояние
`ytcp-video-row`/machine model.

Критический текущий разрыв: Desktop GUI entrypoint вызывает upload без
`receipt_context`, `profile_lease_context`, guards и `phase_callback`. Более строгие
примитивы уже есть в `upload_manager.py`/контрактных модулях, но обычная кнопка GUI их
сквозно не включает. Legacy adapter нельзя строить вокруг этой кнопки: он должен вызывать
строгий manager/worker contract и отдельным тестом доказывать durable boundary и lease.

Локальный `tools/upload_puller.py` показывает server claim/file/progress/result и disk outbox,
но его active-node версия не подтверждена. Более того, если phase `upload_started` не доехала
за пять попыток, эта копия всё равно продолжает browser upload; crash затем может оставить
сервер без знания необратимой границы. Это P0 migration test, а не безопасное поведение для
копирования. Координатор lease находится в `upload_profile_lease.py` и forced-command wrapper,
а dual-host active check — в `collect_channel_emails.py`; их сквозное включение puller-ом
также должно быть доказано.

## Publication plane VSGen

Production уже содержит доменные сущности каналов, profiles, plan items, releases, release
events и upload jobs. Это наиболее близкая к целевой части текущей системы.

Её следует использовать как источник требований и migration adapter, но не переносить
буквально в новый монолит. В частности, новый release должен иметь явные stage attempts,
artifact references и idempotency keys на уровне единой БД.

На дату снимка production считает выпуск успешно завершённым уже в состоянии `scheduled`.
Переход к фактически опубликованному `PUBLIC` практически не подключён, хотя UI для
завершённого plan item пишет «выложен на канал». Поэтому нынешний `done` доказывает только
успешную постановку в расписание. Новая система должна хранить `scheduled` и `published` как
разные проверяемые состояния.

Таблицы `pub_hosts` и `pub_profiles` существуют, но полноценного управления ими пока нет:
host/profile IDs вводятся числами, а внешних ключей нет. Это схема будущего домена, а не готовый
реестр агентов и профилей.

Ещё одно фактическое исключение: `pub_services._reslot` при близком/пропущенном дедлайне
автоматически меняет `slot_at` и plan item, и существующего release. Это полезное recovery,
но оно тихо мутирует расписание. Целевой contract уже фиксирует модель: `release_id` остаётся
стабильным, а каждый перенос создаёт отдельный immutable `schedule_revision_id`; upload attempt
привязан к конкретной revision. Просто переписать timestamp или перепривязать старый attempt
нельзя.

## Windows build

Windows EXE собирается PyInstaller onefile на отдельной машине. Build включает FFmpeg,
FFprobe и yt-dlp. Основной файл там называется иначе, чем локальный `app_main.py`; локально
нет актуального build helper/spec.

До изменения Desktop обязательно получить:

- фактический build manifest;
- SHA всех Python modules в build-папке;
- diff entrypoint/workers с `source/desktop`;
- clean-machine smoke собранного EXE.

Без этого нельзя обещать, что локальная правка попадёт в релиз.

## YouTube DOM evidence

Санитизированная карта подтверждённых selectors и machine-state полей находится в
`source/desktop/research/dom/SELECTOR_MAP.md`. `ytdom.py` и `verify_selectors.py` нужны,
чтобы перед каждым изменением browser adapter повторно снять live evidence. Старая строка из
карты не является вечным selector contract.
