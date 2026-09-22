# Передача Андрею: точечные изменения заливщика

Состояние по итогам пяти циклов 14.09.2026. **Production `/home/vova/VideoStudio/src/` в Минске этим экспериментом не перезаписан.** Испытания запускаются из отдельного `s5_candidate/`; две одновременные PRIVATE загрузки успешно прошли независимый readback (2/2, весь прогон675.533с): [результат](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/s5_parallel/result.json). Ступень четырёх профилей провалила сквозную проверку: 19 остановился до handoff на доступе к каналу, 20 — на повторном PRIVATE Save после закрытия мастера (последующее отдельное чтение подтвердило PRIVATE и обложку), 26/28 — после полного transfer на потерянном мастере/старом refresh fallback. Из четырёх допущенных одновременно передавали файлы только три. Полная FAST-ветка не допускается к массовому включению. `VIDEO_UPLOAD_FAST_UI=1` нельзя объявлять стабильным для остальных профилей или включать всей сетке по этим данным.

Цель переноса — сохранить три небольших изменения в backend заливки объединённого софта. Отдельный новый кабинет, переписывание интерфейса и смена Selenium на другой браузерный стек не требуются.

## Точные проверяемые исходники

Замороженный комплект: [s5_candidate_sha.json](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/s5_candidate_sha.json). Три изменённых основных модуля:

| Модуль | SHA256 |
|---|---|
| [adspower_rate_limit.py](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/s5_candidate/adspower_rate_limit.py) | `b4374ee950487119a45b38790235e2dc732af20f8e1320a57348036eb5ea9ec4` |
| [yt_studio.py](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/s5_candidate/yt_studio.py) | `052b59e85dd2c1209bd70b28fe1903e481e50d6d32a70056c1b82a1d8eff0984` |
| [upload_worker.py](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/s5_candidate/upload_worker.py) | `b56f450efe1d182cd62208352a90fe5123c0d4477a95043f2f6022a9cf6d22d4` |

Остальные зависимости и их hashes перечислены в том же manifest. Переносить изменения относительно актуального backend Андрея; не заменять целиком его модули без сверки контрактов.

## 1. S3: засчитывать простой в AdsPower limiter

В `control_slot()` сохранены mutex на весь control HTTP request и интервал **1.05с**. `_completion_ns()` читает дополнительный атомарный `.completed.json`, связанный с origin, boot_id и started_ns основной записи. Прошедший после завершения запроса простой засчитывается; новый полный sleep после каждого захвата mutex больше не нужен. Основной JSON остаётся schema-v1, совместимой со старыми callers. Старый writer, crash или смена boot_id оставляют консервативное ожидание; невалидные данные останавливают отправку. Автоматического retry HTTP нет.

Перенос: сохранить общий вызов `adspower_rate_limit.get()` вокруг существующих start/active/local-active/stop. Все процессы одного control-origin используют тот же абсолютный каталог состояния. Не держать этот mutex на CDP-чтении или передаче MP4. Это локальная межпроцессная синхронизация, не замена проверки профиля на обеих машинах.

Доказано: median control-call после простоя **1.054204→0.115333с**, затем **20/20** успешных readonly вызовов четырьмя процессами; минимальный completion gap1.052685с. Это ускорение control API, не MP4. [Live замер](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/s3_live.json), [параллельный probe](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/s3_concurrency.json).

## 2. S4: точный PRIVATE Save и быстрые переходы Next

В `_private_save_rejection()` editor route `/video/{ID}/edit` допускает отсутствие `app.channel` только при совпадении **w.video.videoId/channelId + app.currentEntity VIDEO/ID**. Присутствующий app.channel тоже должен совпадать; для channel route его проверка обязательна. Условия PRIVATE, загруженного MP4, единственного видимого мастера, активной Save, отсутствия чужих overlays и scheduler остаются.

`click_private_save()` непосредственно перед единственным кликом повторно читает snapshot в том же JS task. Recursive typed compare сравнивает все keys/values/types и порядок массивов, игнорируя только порядок полей объектов: Selenium сортирует ключи при передаче результата, из-за чего прежний `JSON.stringify` отвергал одинаковое состояние. При расхождении — остановка, без fallback на Publish/Publish anyway.

В `advance_to_review()` FAST-ветка закрепляет известный video ID на весь вызов, проходит шаги по порядку и нажимает Next один раз после валидного шага и готовой кнопки. Затем ждёт изменения `workflowStep` с poll0.25с, максимум30с в пределах общего timeout. Изменившийся/неизвестный ID или отсутствие перехода останавливают вызов. Прежние metadata helpers сохранены.

Перенос: эти predicates и atomic Save оставить внутри существующего browser adapter; backend вызывает `finalize(..., private_video_id=ID, private_channel_uc=UC)` для strict PRIVATE. Не заменять состояние мастера текстом уведомления или подписью кнопки.

Доказано на одном existing-draft canary профиля45: **3 Next за1.05617с**, ровно один Save, independent saved readback подтвердил ID/UC/PRIVATE/CUSTOM_STILL; повторной передачи файла не было. Сопоставимого baseline для процента ускорения нет. PRIVATE fixture не доказывает публичную доступность материала. [Описание и patch](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/s4/README.md), [live summary](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/s4/s4_live_summary.json).

## 3. S1/S2/S5: readiness вместо лишних переходов и resume-dismiss

В `upload_worker.py` FAST-ветка переиспользует уже готовый точный канал: current URL, app.channel, currentEntity CHANNEL/UC и Create должны совпасть. Для account menu сначала ожидается готовность самой Studio; если root уже открыл нужный UC, меню не открывается. После `?d=ud` ожидается готовность upload dialog вместо фиксированного sleep; это не заменяет fresh baseline перед отправкой файла.

Прямой переход получает ранний штатный fallback только при доказанном **main navigation403**: новый timeOrigin, exact URL/nav.name, supported responseStatus403, responseEnd, отсутствие app/wizard. Ошибка XHR/resource, старый snapshot или неизвестный статус не сокращают прежний timeout25с.

S5 `_wait_visible_resume_disposition()` читает только существующие видимые `TP-YT-PAPER-DIALOG`, учитывая hidden/style у предков и shadow hosts. Нулевую высоту host не считает скрытием: живой uploads host имеет height0 при видимом внутреннем диалоге. Скрытый текст не вызывает глобальный Cancel, Escape и reload. Видимый resume → BUSY; неизвестная/загружающаяся Studio получает до10с с poll0.25с, затем BUSY. Этот deadline ограничивает повторный опрос; он не прерывает зависший отдельный Selenium вызов. Ранний foreign-interrupted guard и последующие exact dedup/fresh file/ID guards сохранены.

Перенос: взять только эти helpers и места вызова; не добавлять screenshot/LLM-цикл перед каждым кликом. Пока держать FAST как opt-in выбранной очереди/профилей. [S5 review diff](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/s5_resources_snapshot_20260914T135932Z/s5_worker_review.diff), [resume tests](/Users/vova/VideoStudio/tests/test_visible_resume_guard.py), [navigation tests](/Users/vova/VideoStudio/tests/test_upload_speed_waits.py).

## Флаги и безопасный контракт

| Параметр | При отсутствии | Что делать при переносе |
|---|---|---|
| `ADSPOWER_RATE_LIMIT_DIR` | Pacing выключен | Передать существующий общий абсолютный путь всем control-callers; не создавать отдельную очередь на каждый worker. |
| `VIDEO_UPLOAD_FAST_UI` | FAST выключен; legacy waits | Оставить `0` по умолчанию. `1` включает навигацию/menu/dialog и S4 Next; S5 resume требует также strict. |
| `VIDEO_UPLOAD_STRICT` | Strict выключен | Контрольные загрузки идут с `1`; сохранять его вместе с durable handoff, identity и cover proofs. Не выключать ради скорости. PRIVATE guard включается для strict + PRIVATE независимо от FAST. |
| `VIDEO_UPLOAD_VERIFIED_EMPTY_FAST` | Ускоренное подтверждение пустого канала выключено | Уже существующий отдельный режим; требует strict + expected UC + двух одинаковых точных SUCCESS/EXACT snapshots через≥2с. Не трактовать обычный `[]` как разрешение залить повторно. |

Manager должен передать env в worker subprocess и сохранить существующие cross-host profile lease, `upload_started` с durable ACK до file input, связанный fresh frontend/video ID, проверку приёма обложки и final readback. После file handoff отсутствие успеха не разрешает новую заливку: восстановление идёт по известному ID. Эти контракты не нужно переносить в пользовательский интерфейс; UI может показывать текущую фазу, канал, video ID и требуемое действие.

## Приёмка в объединённом софте

Сначала перенести S3 и S4 correctness с их тестами; FAST оставить выключенным. Полную FAST-ветку оставить выключенной до исправления потери wizard и восстановления по exact video ID. Ступень четырёх профилей не прошла. Пара45/55 завершилась2/2, но этого недостаточно для допуска всей сетки или обещания ёмкости100 аккаунтов. Не копировать legacy fallback с refresh после известного ID в новый объединённый софт.

Обязательные исходные тесты: `test_adspower_rate_limit.py`, `test_upload_auth_slot.py`, `test_private_editor_intent.py`, `test_private_stress.py`, `test_upload_speed_waits.py`, `test_visible_resume_guard.py`, `test_fresh_upload_identity.py`, `test_upload_strict.py`. Приёмка live проверяет exact канал/ID, отсутствие второго handoff, accepted custom cover, сохранённую целевую visibility и освобождение профиля/lease. Исходный профиль, прокси и состояние сессии входят в условия замера.

S1/S2 уже имели неуспешные прогоны и renderer stalls. Во время части baseline S5 параллельно работал ffmpeg и CPU доходил96.3%; позднее CPU был свободен. Во время FOUR снова начался рендер: в14:15UTC CPU99.8%,12ffmpeg; cgroup подтвердил `vsrender-puller.service`. Поэтому разницу whole-upload времени нельзя целиком приписывать коду. [Оценки циклов](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/evaluations.md), [ресурсы](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/RESOURCE_REPORT.md), [методика end-to-end расчёта](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/B/README.md), [descriptive сравнение двух профилей](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/B/s5_comparison_two_v1.json).

Официальные источники, зафиксированные в [api_notes.md](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/api_notes.md): [AdsPower start API](https://localapi-doc-en.adspower.com/docs/FFMFMf), [active/local-active](https://localapi-doc-en.adspower.com/docs/YjFggL), [API frequency](https://localapi-doc-en.adspower.com/docs/Rdw7Iu), [Selenium waits](https://www.selenium.dev/documentation/webdriver/waits/), [CDP connection limits](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp), [actionability](https://playwright.dev/python/docs/actionability). AdsPower API управляет профилем и выдаёт ChromeDriver/CDP endpoints; DOM-чтения и клики дальше выполняются через браузерное подключение.

## Оставшиеся блокеры после пятого цикла

- Перед резервированием передачи проверять действующий доступ выбранного аккаунта к exact UC: профиль19 получил permission denied.
- После известного video ID потеря wizard/SELECT_FILES должна завершать попытку с recovery по тому же ID; текущий fallback ошибочно ждёт проверки YouTube до30мин.
- На20 первый atomic PRIVATE Save по frozen codepath выполнен, мастер закрылся, затем worker ошибочно дошёл до повторного клика. После acknowledged click нужна проверка exact сохранённого ID, а не повторный Save; URL/ownership guard ослаблять нельзя. Последующий независимый readback подтвердил PRIVATE и сохранённую обложку; первый Save действительно сохранился. Исходный автоматический статус failed не переписывался.
- В тестовом harness игнорирование SIGINT/SIGTERM наследуется worker; требуется штатная отмена с сохранением phase/ID и закрытием под тем же lease. Это обнаруженный недостаток, а не пройденный cancellation test.
- Конкурирующий рендер из VideoAgent использовал12FFmpeg и почти100%CPU. Совместный бюджет нагрузки и отдельные стадии transfer/finalize нужны до масштабного запуска.

Полный отчёт: [RESULT.md](/Users/vova/VideoStudio/research/ww2_upload_speed_20260914/RESULT.md). Порог8.5 всего пайплайна не достигнут; S3 имеет отдельный допуск, который нельзя переносить на общий результат.

Дополнение14:46:53UTC: исходный four runner завершился completed0/expected4; известные ID сохранены. Отдельный ROreadback20 подтвердил PRIVATE+customcover без Save/новойпередачи. Точный результат и независимые оценки: RESULT.md и final_critic.md; общая5.5,порог8.5недостигнут.
