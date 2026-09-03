# Индекс исходников

В архив включён не только «красивый пример», а достаточный снимок исполняемой логики. Читать
все файлы подряд не нужно. Ниже — выжимка и порядок.

## Первый проход: 14 файлов

| Приоритет | Файл | Что из него понять |
|---:|---|---|
| 1 | `source/vsgen_server/pub_engine.py` | текущая state machine выпуска |
| 2 | `source/vsgen_server/pub_schema.py` | текущие channel/plan/release/upload сущности |
| 3 | `source/vsgen_server/pub_services.py` | связи VSGen ↔ Rewrite ↔ Cover ↔ render |
| 4 | `source/vsgen_server/pub_upload_api.py` | claim/result загрузки и защита от дубля |
| 5 | `source/vsgen_server/main.py` | реальная web queue, auth, projects и provider routing |
| 6 | `source/rewrite/job_queue.py` | fair durable queue и run token |
| 7 | `source/rewrite/rewrite_core.py` | chunk/checkpoint/provider/quality semantics |
| 8 | `source/rewrite/api.py` | публичный Rewrite contract |
| 9 | `source/cover/job_queue_cover.py` | Cover lifecycle/fairness/recovery |
| 10 | `source/cover/cover_core.py` | режимы, provider calls, partial/fallback |
| 11 | `source/cover/api_cover.py` | публичный Cover contract |
| 12 | `source/desktop/src/upload_contract.py` | необратимая граница upload и receipts |
| 13 | `source/desktop/src/googler_worker.py` | scene/image/video/reference contracts |
| 14 | `source/desktop/src/render_worker.py` | фактическая FFmpeg/timeline логика |

До чтения этих файлов полезнее прочитать `docs/01`–`08`: монолиты объясняют детали, но плохо
объясняют намерение.

## VSGen server

- `main.py`: production snapshot, самый полный, но не образец структуры.
- `src/tts_worker.py`: актуальный 31.08 Lumean/HeyGen preflight, full-text receipt/recovery и
  изолированный output-каталог.
- `pub_*.py`: актуальная publication plane; вместе образуют один домен.
- `card_jobs.py`, `channel_slots.py`: отдельные очереди/расписание.
- `src/*_worker.py`: production-версии Python workers рядом с сервером.
- `src/reference_contract.py`, `prompt_llm.py`: строгие входы/выходы и LLM schema.

Важно: server `render_worker.py` не доказывает версию файла на отдельной render-ноде.

## Desktop VideoStudio

- `src/app_main.py`: Tkinter UI и orchestration.
- TTS: `edgetts_worker.py`, `tts_worker.py`, `voicer_worker.py`, `voicegencore_worker.py`.
- Visuals: `image_worker.py`, `googler_worker.py`, `reference_contract.py`.
- Render: `render_worker.py`, `chapters.py`.
- Upload: `upload_manager.py`, `upload_worker.py`, `yt_studio.py`, `upload_contract.py`,
  `upload_coordination.py`.
- Browser helpers: `tools/monet_cdp.py`, `tools/studio_ab_monet.py`; `tools/ytdom.py`
  снимает live DOM evidence перед изменением YouTube selectors.
- Edge references: `tools/render_puller.py`, `tools/upload_puller.py`; exact active-node
  versions не подтверждены, поэтому это protocol evidence, а не deploy source.
- Coordination: `tools/upload_profile_lease.py`, `upload_profile_lease_forced.py`,
  `collect_channel_emails.py` и `verify_selectors.py`.
- Sanitized DOM evidence: `research/dom/SELECTOR_MAP.md`.
- Real visual parser: `parser_worker.py`, пакет `parser/` и
  `parser/niches/ww2.yaml`; это отдельный pipeline, не вызванный обычным `app_main.py`.
- `semantic_montage/`: экспериментально-производственный контур длинных донорских видео;
  изучать отдельно, если он входит в согласованный scope новой версии.

Desktop snapshot взят из dirty working tree. Он не является доказанным содержимым последнего
EXE. `build_workflow_reference.md` объясняет исторический процесс, но не заменяет build manifest.

## Rewrite Studio

- HTTP/auth: `server.py`, `api.py`, `auth.py`, `api_auth.py`.
- Execution: `job_queue.py`, `rewrite_core.py`.
- Generation/QA dynamic modules: `gen_v7.py`, `gen_output_cleaner.py`, `gen_editor.py`,
  `gen_judge.py`, `qa_findings.py`.
- Safety/utility: `log_public.py`, `subs_fetch.py`.
- UI: `static/index.html`.
- Schema changes: `migrations/`.

Файл `requirements-server.local-reference.txt` — локальная справка, не production lockfile.

## Cover Studio

- HTTP/auth: `cover_server.py`, `api_cover.py`, `auth.py`, `api_cover_auth.py`.
- Execution: `job_queue_cover.py`, `cover_core.py`, `provider_policy.py`.
- Credentials/admission: `api_cover_helpers.py`.
- UI: `static/index.html`.
- Prompt defaults: `default_prompts.json`, если он присутствует в allowlist snapshot.
- Queue tuning: `cover_gen_settings.json` — безопасные действующие числовые лимиты.

Production и local Cover разошлись. Архив не склеивает их автоматически: provenance указывает,
что взято с работающего сервиса, а `docs/14` — что нужно вручную reconciliation.

## Общий runtime helper

`source/shared/claude_limits.py` установлен вне каталогов приложений и опционально импортируется
VSGen/Rewrite. Без него server может стартовать, но subscription limiting меняется. Это пример,
почему «приложение поднялось» не равно «runtime воспроизведён».

## Контракты и тесты

- `contracts/openapi/`: live route/schema inventory.
- `contracts/schema/`: DDL-only, без данных.
- `contracts/examples/`: предлагаемая общая модель.
- `contracts/fixtures/`: полностью синтетические примеры.
- `tests/reference/`: регрессии текущих контрактов; не единый запускаемый test suite.

## Как использовать с нейросетью

Давайте модели один домен и связанные docs/tests, а не весь архив одним prompt. Просите:

1. перечислить входы, выходы, side effects и invariants;
2. привязать каждый вывод к файлу/функции;
3. отметить неизвестное, а не додумывать;
4. предложить contract tests до нового implementation;
5. отдельно разобрать restart/timeout после внешнего side effect.

Любой сгенерированный ответ проверяет разработчик. Код в архиве может содержать исторический
bug; он является свидетельством текущего поведения, а не автоматически правильной спецификацией.
