# Карта DOM YouTube Studio — снята живьём 2026-08-03

Источник: санитизированный профиль AdsPower 9001, канал `UCFAKE000000000000000001` (Synthetic Channel),
интерфейс Studio на английском. Снято через `tools/ytdom.py`.
Проверяется командой `python3 tools/verify_selectors.py --profile <N> --channel <URL>`
(на 03.08 — **34/34 живо**).

Правило: **ни один селектор не попадает в код без строки из дампа.** Если Studio
изменится — сначала снять дамп заново, потом править `src/yt_studio.py::SEL`.

---

## 1. Главное открытие: Studio отдаёт состояние программно

Polymer-компоненты держат реальное состояние в JS-свойствах. Оно машинное и
**одинаковое на всех языках интерфейса** — поэтому regex по словам
(`Draft|Черновик|Entwurf|…`) не нужен вообще.

### `ytcp-video-row` → свойство `.video`

| Поле | Пример значения | Заменяет |
|---|---|---|
| `videoId` | `DEMO0000004` | парсинг `href` из строки |
| `title` | `Synthetic example title…` | `#video-title` textContent |
| `draftStatus` | `DRAFT_STATUS_NONE` / `DRAFT_STATUS_PUBLIC` | regex `Draft\|Черновик\|…` |
| `privacy` | `VIDEO_PRIVACY_PUBLIC/PRIVATE/UNLISTED` | regex `Public\|Открыт\|…` |
| `status` | `VIDEO_STATUS_PROCESSED/UPLOADED` | regex «обработка» |
| `visibility.effectiveStatus` | `…_DRAFT` / `…_USER_CONFIG` / `…_UPLOADING_OR_PROCESSING` | — |
| `timePublishedSeconds` | `1785603611`, `0` у черновика | детект Scheduled |
| `videoPrechecks.*` | `copyrightPrechecksDone: true` | regex «checking / copyright» |
| `alteredContentSettings.creatorDisclosedHasAlteredContent` | `VIDEO_HAS_ALTERED_CONTENT_YES` | — |

Также на строке: `visibilityCellRenderData.iconId` = `yt-sys-icons:privacy_public`
(тоже язык-независимо).

### `ytcp-uploads-dialog` (мастер загрузки)

| Поле | Пример | Заменяет |
|---|---|---|
| `workflowStep` | `DETAILS` → `REVIEW` | угадывание шага по `step-badge` |
| `isVideoUploaded` | `true` | regex «upload complete\|загрузка завершена\|…» |
| `isCurrentStepInvalid` | `false` | проверку `aria-disabled` на Next |
| `isPrechecksLoading` | `false` | regex «checking\|проверка» |
| `shouldShowMonetizationStep` | `false` | слепое заполнение YPP-шага |
| `errorSectionStatus` | `LOADING` → `SUCCESS` | — |
| `uploadMode` | `EXISTING_DRAFT` | — |
| `uploadDetailsByFrontendId` | прогресс заливки | regex `(\d+)\s*%` |

⚠️ Сразу после открытия мастер отдаёт `errorSectionStatus=LOADING`,
`isVideoUploaded=null`, `workflowStep=DETAILS` — **действовать в этот момент
бесполезно**. Ждать через `yt_studio.wait_wizard_ready()`.

### `ytcp-video-visibility-select`

| Поле | Значение |
|---|---|
| `availabilityRadioSelection` | `SCHEDULE` / (публикация сейчас) |
| `visibilityOptions` | `["PRIVATE","UNLISTED","PUBLIC"]` — SCHEDULE тут НЕТ |

---

## 2. Селекторы

### Список видео
```
ytcp-video-row                              строка видео (свойство .video)
```

### Открыть мастер существующего черновика
```
/video/{videoId}/edit  →  ytcp-button#action-1     («Edit draft», стабильный id)
```
Страница `/video/{id}/edit` у черновика показывает ТОЛЬКО эту кнопку —
редактировать черновик можно лишь через мастер.

### Мастер
```
ytcp-uploads-dialog                         сам мастер
[id^=step-badge-]                           шаги: step-badge-0..3
  step-badge-0 Details · 1 Video elements · 2 Initial check · 3 Visibility
ytcp-button#next-button                     Далее
ytcp-button#done-button                     финальная (Save/Publish/Schedule)
ytcp-button#close-button                    закрыть (сохранить черновиком)
```

### Шаг Details
```
#textbox[aria-label]                        заголовок (первый ВИДИМЫЙ) и описание (второй)
tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]
tp-yt-paper-radio-button[name="VIDEO_HAS_ALTERED_CONTENT_YES"]    AI-дисклоуз
ytcp-button#toggle-button                   «Показать больше» (раскрывает AI-секцию)
```

### Шаг Visibility (внутреннее имя шага = `REVIEW`, не «VISIBILITY»)
```
ytcp-video-visibility-select                компонент
tp-yt-paper-radio-group#privacy-radios      группа
tp-yt-paper-radio-button[name="PUBLIC"]     (+ PRIVATE, UNLISTED)
```
Проверка выбора — по `aria-checked="true"`.

### Отложка ⚠️ здесь ломалось чаще всего
**Радио `name="SCHEDULE"` НЕ СУЩЕСТВУЕТ.** Отложка — отдельная раскрывающаяся секция:

```
#second-container-expand-button             раскрыть секцию Schedule (aria «Click to expand»)
ytcp-datetime-picker                        появляется после раскрытия
  #datepicker-trigger                       текущая дата, открывает календарь
  #time-of-day-container input              ВРЕМЯ (текстом, напр. "2:30 PM")
  ytcp-button#timezone-select-button        часовой пояс

ytcp-date-picker tp-yt-paper-dialog#dialog        ⚠️ КАЛЕНДАРЬ — ОТДЕЛЬНЫЙ ДИАЛОГ НА УРОВНЕ <body>
ytcp-date-picker tp-yt-paper-dialog#dialog input  ДАТА текстом (напр. "Aug 4, 2026")
ytcp-scrollable-calendar#calendar                 сам календарь
  ytcp-icon-button#prev-month / #next-month
```

Дату задавать **текстом в input диалога**, а не кликом по числу: клик по числу
не листает месяцы → дата из другого месяца выбиралась неверно.

Подтверждение отложки: `ytcp-video-visibility-select.availabilityRadioSelection === "SCHEDULE"`.

---

## 2b. ⚠️ Свежая заливка ≠ черновик (снято 2026-08-03 живой заливкой)

Всё выше снималось на ЧЕРНОВИКЕ (`uploadMode = EXISTING_DRAFT`). У свежей заливки
(`uploadMode = NEW_UPLOAD`) на **монетизированном канале** мастер другой:

```
черновик:        Details | Video elements | Initial check | Visibility          (4 шага)
свежая заливка:  Details | Monetization | Content ratings | Video elements | … | Visibility
```

**Переход по `step-badge-N` при свежей заливке НЕ РАБОТАЕТ** — перепрыгнуть шаг нельзя,
пока предыдущие не пройдены (у черновика они уже `completed`). Идти только через
`ytcp-button#next-button`, проверяя `isCurrentStepInvalid` перед каждым шагом.

### Шаг MONETIZATION (только на YPP-каналах)
`shouldShowMonetizationStep = true`. Контрол — НЕ дропдаун «Select»:
```
ytcp-video-monetization                     компонент («Select»)
  └ ytcp-icon-button                        ⚠️ клик ВНУТРИ компонента раскрывает радио
                                              (aria «Edit video monetization status»;
                                               такая же кнопка есть на фоне — целиться в компонент!)
tp-yt-paper-radio-button#radio-on[name="ON"]
tp-yt-paper-radio-button#radio-off[name="OFF"]
ytcp-button#save-button                     «Done» — БЕЗ него шаг остаётся невалидным
```
Проверка: после `#save-button` → `isCurrentStepInvalid === false`, Next разблокирован.

### Шаг CONTENT_RATINGS — главный блокер
`shouldShowContentRatingsStep = true`, `selfCertificationStatus =
VIDEO_SELF_CERTIFICATION_STATUS_NOT_CERTIFIED`.
```
ytpp-self-certification-questionnaire
  └ ytpp-self-certification-question × 11    анкета: профанация, сексуальный контент,
                                             насилие, шок-контент, опасные ситуации,
                                             наркотики, …
ytpp-self-certification-predictor            предсказание ответов от YouTube
ytcp-button#submit-questionnaire-button      «Submit rating»
```
Пока анкета не отправлена — `isCurrentStepInvalid = true`, **Next заблокирован
навсегда** → мастер физически не доходит до шага Visibility → отложку/публикацию
применить не к чему → ролик остаётся ЧЕРНОВИКОМ.

Именно это, а не селекторы отложки, роняло свежую заливку на монетизированных
каналах. Старый код про оба шага не знал: `_fill_monetization_step` искал дропдаун
«Select» (не находил), а про CONTENT_RATINGS не было ничего.

⚠️ Анкета самосертификации — **декларация о содержании ролика**. Автоматически
отвечать на неё = делать заявление от имени владельца канала; неверная декларация
бьёт по монетизации. Решение об автозаполнении принимает владелец, не код.

## 2c. Финализация: почему заливка «молча» оставалась приватной

После клика по `#done-button` («Schedule»/«Publish») мешают две вещи:

1. **Поверх мастера всплывает диалог** «We're still checking your content…» с кнопкой
   `ytcp-button#primary-action-button` («Got it»). Он перехватывает клики — повторные
   нажатия done уходят в него, видео остаётся Private.
2. **`ytcp-uploads-dialog` после успеха НЕ закрывается.** Ожидание его закрытия
   истекает, и заливка считается неудачной, хотя всё прошло.

Признак успеха — **финальный `tp-yt-paper-dialog`** с текстом вида
«Video scheduled — Your video will be set to public on August 11, 2026 at 3:00 PM»
(содержит и ссылку на видео). Закрывается `ytcp-button#close-button`.

### Как выглядит ЗАПЛАНИРОВАННОЕ видео в списке
```
privacy               = VIDEO_PRIVACY_PRIVATE        ← НЕ признак ошибки!
timePublishedSeconds  = 0                            ← тоже не признак
visibility.userSetVisibility = VIDEO_USER_SET_VISIBILITY_SCHEDULED   ← ГЛАВНЫЙ признак
scheduledPublishingDetails.scheduledPublishings[0].scheduledTimeSeconds = 1786435200
visibilityCellRenderData = {iconId:"yt-sys-icons:watch_later", text:"Scheduled"}
videoDateLabel = "Scheduled"
```

## 2d. Коллаборации (соавторы)

Живут НЕ в мастере заливки, а на странице видео `/video/{id}/edit`, в расширенных
настройках. Порядок:

Полный порядок (проверен живьём 03.08, приглашение реально создано):

```
ytcp-button#toggle-button                    «Show more» (раскрыть доп. настройки)
ytcp-button#collaboration-button             «Invite a collaborator»
  └ ytcp-video-collaborators-dialog
      input#search-input                     поиск канала — ТОЛЬКО нативный ввод
      ytcp-channel-picker-list-item          «Название @handle • N subscribers» → клик
      ytcp-button#create-link-button         ⚠️ «Create link» — ОБЯЗАТЕЛЬНО
      ytcp-button#save-button                активен ТОЛЬКО после create-link
      ytcp-button#cancel-button              отмена
ytcp-button#save                             «Save» страницы (если стал активен)
```

⚠️ **Без «Create link» кнопка Save в диалоге остаётся `aria-disabled="true"`** —
клик по ней уходит в никуда, и коллаборация молча не создаётся. Коллаборация в
Studio оформляется ссылкой-приглашением (после создания появляется «Copy invite link»).

⚠️ **Искать канал ТОЛЬКО по точному handle.** Поиск по названию возвращает чужие
каналы-однофамильцы: запрос «Example Film Studio» первым отдал `Example Film Studio
@external_example • 0 subscribers` — посторонний канал. Приглашение уходит наружу
необратимо, поэтому «взять первый похожий» недопустимо; при отсутствии точного
совпадения по `@handle` функция отказывается приглашать.

### Приём приглашения (вторая сторона)

Коллаборация двусторонняя: после создания она висит в статусе **`Pending acceptance`**,
и пока соавтор не примет — остаётся приглашением. Статусы в диалоге «Manage collaborators»:

| Текст в диалоге | Значение |
|---|---|
| `Pending acceptance` | приглашение отправлено, не принято |
| `Active collaborator` | принято, коллаборация действует |

**Ссылка-приглашение** (снята с кнопки «Copy invite link», активна после Save):
```
studio.youtube.com/channel/{UC_приглашённого}/collaboration/{UC_пригласившего}
```
Строится из двух ID — читать буфер обмена не нужно (`yt_studio.collab_invite_url`).

**Как принимают** (проверено живьём 04.08.2026, статус сменился на `Active collaborator`):

1. Активным каналом сессии сделать **канал-соавтор** — строго `_switch_to_channel`
   (меню Studio), НЕ заходом по URL `/channel/{UC}`.
2. Открыть ссылку-приглашение → всплывает диалог «You're invited to collaborate»
   («… invited you to collaborate on "<название>" and get featured»).
3. Нажать `ytcp-button#confirm-button` («Accept»); рядом `#deny-button` («Decline»),
   закрытие — `ytcp-icon-button#close-button`.
4. Признак успеха: диалог закрылся, страница пишет «All changes saved»,
   у отправителя статус становится `Active collaborator`.

⚠️ **Заход по URL `/channel/{UC}` НЕ переключает активный канал сессии.** Studio
открывается, выглядит правильно, но ссылка-приглашение молча редиректит на
`/videos/upload` — на этом потерян почти час. Переключать только через меню.

⚠️ Вкладки `/videos/collaborations` НЕ существует (отдаёт «Oops, something went
wrong») — ошибочная догадка, не использовать. В левом меню Studio пункт
«Collaborations» есть, но по другому пути.

Если канал-соавтор в другом AdsPower-профиле, приглашение приходит письмом на почту
его Google-аккаунта — тогда приём выполняется в том профиле
(`find_collab_invite_in_gmail` достаёт ссылку из письма по подстроке `/collaboration/`,
язык письма роли не играет).

⚠️ После принятия YouTube может нормализовать заголовок ролика (в тесте
`VS upload test 20260804-…` → `vs upload test`) — сравнения по названию делать
регистронезависимо и не полагаться на точное совпадение.

**Признак применённой коллаборации** (машинный):
`ytcp-video-row.video.collaboration.allCollaborators[].externalChannelId` — появляется
только после реально созданного приглашения. `serializedShareEntity` присутствует
ВСЕГДА (это идентификатор владельца) и признаком НЕ является.
Визуально: блок меняется с «Invite a collaborator» на «Manage collaborators».

Для прогона без отправки: `yt_studio.set_collaborator(..., save=False)`.

## 3. Технические грабли

**Shadow DOM.** Половина Studio живёт в `shadowRoot`; обычный
`document.querySelector` их не видит. Нужен рекурсивный обход
(`yt_studio._JS_BASE::deepAll/deepFind`).

**Клики.** Polymer часто игнорирует `el.click()` — overlay `ytcp-uploads-review`
перехватывает. Рабочий приём: временно скрыть overlay и послать настоящую
последовательность `mousedown → mouseup → click` по центру элемента
(`yt_studio._JS_BASE::realClick`).

**Ввод в поля.** Присваивание `input.value` теряется — Polymer слушает события.
Ставить через нативный сеттер прототипа + `input`/`change`
(`yt_studio._JS_BASE::setNativeValue`).

**NFD/NFC.** Studio отдаёт диакритику в NFD (`é` = `e`+U+0301), Python-строки в NFC
→ прямое сравнение заголовков промахивается на всех языках с диакритикой.
Нормализовать перед сравнением (`yt_studio._norm`).

**Языковые дыры старого текстового пути** (почему от него ушли): regex
`Draft|Черновик|koncept|Entwurf|brouillon|bozza|robocza|rascunho|schiț`
не ловит испанский `Borrador`, нидерландский `Concept`, турецкий `Taslak` →
на таком канале черновик не распознаётся и ролик заливается **заново**.

## 2e. Мидроллы (ручная расстановка рекламных слотов)

Страница: `/video/{id}/monetization/ads?authuser=N` → `ytcp-button#place-manually-button`
(«Review mid-roll placement»). Снято дампом 2026-08-04, расстановка подтверждена живьём.

```
ytpp-ad-breaks-settings                       настройка ролика (ИСТОЧНИК ИСТИНЫ)
  adBreaks.manualMidrollPlacements.placementTimesMillis   ← наши слоты
  adBreaks.autoGenMidrollPlacements.placementTimesMillis  ← что расставил YouTube
  adBreaks.autoGenMidrollsStatus = …_PROCESSING | …       ← пока считается
  submitAdBreaksChanges()
ytmonet-video-monetization-editor              редактор (открывается кнопкой)
ytve-ad-breaks-editor-options-panel
  adBreaks[] = [{placementTimeMs, type:'auto'|'manual'}]
  [aria-label="Insert ad slot"]                вставка слота (создаётся на 00:00:00)
  поля времени: формат MM:SS:FF (FF — кадры при 30 fps)
ytve-ad-breaks-marker                          маркеры таймлайна (только отрисовка)
ytcp-button#save-button (Continue) · ytcp-button#save (Save страницы) · #discard-button (Back)
```

⚠️ **Запись массива `adBreaks` в модель НЕ сохраняется.** Панель перерисовывается,
поля показывают новые времена, но Studio не засчитывает изменение (кнопка сохранения
остаётся неактивной), и после перезагрузки слотов ноль. Нужны РЕАЛЬНЫЕ клики
«Insert ad slot» + нативный ввод времени. Быстрый путь оставлен только как попытка
с проверкой активности кнопки сохранения.

⚠️ **Проверять результат по `read_ad_slots()` НЕЛЬЗЯ.** Пока
`autoGenMidrollsStatus = …_PROCESSING`, редактор заблокирован
(`shouldDisableAdBreaksEditor=true`, Insert disabled, маркеров 0) — но сохранённые
ручные слоты при этом ЕСТЬ. Источник истины — `read_saved_midrolls()`, читает
`manualMidrollPlacements` из настройки.

⚠️ **Кнопка «Review mid-roll placement» появляется РАНЬШЕ, чем становится активной.**
Ждать `aria-disabled != true`, иначе редактор откроется пустым и вставка молча не сработает.

⚠️ **YouTube СДВИГАЕТ метки** к ближайшей естественной точке: план `2:30 / 5:00 / 7:30 / 10:00`
сохранился как `2:53 / 5:50 / 7:53 / 10:50`. Сравнивать с планом «в лоб» нельзя.

**Условия доступности** (из справки YouTube, answer/6175006): ролик ≥ 8 минут,
монетизация включена, функция доступна после обработки видео; проверка качества
слотов (`manualBqsStatus`) идёт до часа после обработки.

**Порядок (важен):** Monetization → On, и только потом «Review mid-roll placement» —
при выключенной рекламе кнопка неактивна.

---

## Рекламные паузы (мидроллы) — дамп 2026-08-05

Страница: `studio.youtube.com/video/{id}/monetization/ads?authuser=N`.
Открывается и на ЗАПЛАНИРОВАННОМ ролике — публикация не нужна (прежний вывод об этом
был ошибочным: «Oops, something went wrong» на скриншоте давал чужой authuser).

### Страница монетизации
```
ytpp-ad-breaks-settings                      настройки ролика (источник истины)
  .adBreaks.manualMidrollPlacements.placementTimesMillis   наши метки
  .adBreaks.autoGenMidrollPlacements.placementTimesMillis  метки YouTube
  .adBreaks.autoGenMidrollsStatus            …_PROCESSING, пока считаются авто
ytcp-button#place-manually-button            «Review mid-roll placement», aria-disabled=false
                                             даже когда ролик в отложке
```

### Внутри редактора (появляется ТОЛЬКО после клика по кнопке выше)
```
ytve-ad-breaks-editor-options-panel          панель слотов, ЛЕЖИТ В SHADOW DOM
ytve-framestamp-input                        строка одного слота
  ytve-formatted-input > div#content-container > input   поле времени
ytve-ad-breaks-marker                        маркер на таймлайне (по одному на слот)
[aria-label="Insert ad slot"]                вставить слот (создаётся на 0:00:00:00)
ytcp-button#save-button                      «Continue» из редактора
ytcp-button#save                             «Save» страницы
button «Remove Ads»                          НЕ срабатывает программно (клик проходит,
                                             Studio изменение не засчитывает)
```

### Формат времени
Поля отдают и принимают **четыре части**: `Ч:ММ:СС:кадры` (`0:05:51:13`).
Не `ММ:СС:кадры` — на часовых роликах минуты уезжают за 60.

### Три ловушки, снятые замерами
1. **Панель отрисовывается с задержкой.** Сразу после открытия редактора полей и
   маркеров НОЛЬ, они появляются через 1–2 с. Клик «Insert ad slot» в этот момент
   уходит вхолостую → ждать `_slot_inputs(d) or read_ad_slots(d)`.
2. **Поля не видны обычным CSS** — только обход shadow DOM (`deepAll`).
3. **Ввод времени принимает лишь один способ:** CDP-клик мышью по координатам поля →
   `setSelectionRange(0, len)` → `Input.insertText` → Enter.
   НЕ работают: `ActionChains.send_keys`, цифры без разделителей,
   `Input.dispatchKeyEvent` без кодов клавиш, запись в модель панели, `setNativeValue`.
   Проверять результат по маркерам (`read_ad_slots`), а не по тексту поля: после Enter
   Studio переформатирует значение.

### Удаление слота — дамп 2026-08-05
```
ytve-ad-breaks-marker                        маркер на таймлайне, свойство positionMs (мс)
```
Своей кнопки удаления у маркера НЕТ. Работает: настоящий клик мышью по маркеру (CDP по
координатам `getBoundingClientRect`) + клавиша `Delete`, затем «Continue» → «Save».

⚠️ Кнопка **«Remove Ads» в панели — ЧУЖАЯ**: её рисует расширение Nexlev
(цепочка `… < nexlev-csui < ytve-ad-breaks-editor-options-panel`). Клики по ней Studio
не видит. Не принимать её за штатный контрол — на это ушёл час.

## Удаление ролика (`yt_studio.delete_video`) — 05.08.2026

Путь снят дампами, каждый шаг куплен неудачей. Страница: список видео
`/channel/<UC>/videos/upload` (на `/video/<id>/edit` кнопка Options отдаёт 0×0
в фоновом окне Chrome — оттуда удалять нельзя).

1. **Кнопка появляется только при наведении.** `Input.dispatchMouseEvent`
   `mouseMoved` в точку `(правый край строки − 80, центр строки)` у `ytcp-video-row`.
2. **Два разных состояния строки — два разных пути:**
   - прерванная загрузка: в строке есть **прямая** кнопка `.delete-draft-button`
     («Delete video»), меню Options нет вообще;
   - обычный ролик: меню `actions-menu-button` → пункт «Delete forever» (последний).
3. **Перед каждым кликом обязателен `mouseMoved` ровно в точку кнопки.** Без наведения
   Studio молча игнорирует нажатие: «Delete video» не открывает диалог. Симптом
   обманчив — ripple-подсветка рисуется, будто клик прошёл.
4. **Диалог не ищется по тегу.** `ytcp-dialog` / `ytcp-dialog-manager` имеют нулевую
   высоту, содержимое лежит глубже. Находить по фразе `cannot be undone|permanently
   delete`, взяв самый короткий подходящий `innerText`.
5. **Согласие ставится кликом по ПОДПИСИ (`<label>` «I understand…»), а не по чекбоксу.**
   `ytcp-checkbox-lit` клик мышью игнорирует, и его свойство `checked` остаётся `false`
   даже после принятия согласия. Единственный честный признак успеха — кнопка
   подтверждения перестала быть `disabled`.
6. **Не искать элементы по всей странице.** Первый видимый `checkbox` — это выбор строки
   таблицы (x≈120), а текст «Delete video» носят и кнопки в строках. Отсюда ложные
   клики: выделение строки вместо согласия.

Проверено: два синтетически обозначенных мусорных черновика удалены этим путём.

### Дополнения к удалению (вечер 05.08.2026)

- **Строка бывает шире окна.** У части профилей кнопка `.delete-draft-button` стоит на
  `x≈1307` при `window.innerWidth = 1199` — CDP-клик по координате уходит мимо экрана,
  диалог не открывается, а по логу выглядит как «кнопка не сработала». Перед снятием
  координат обязателен `scrollIntoView({block:'center', inline:'center'})`, после — проверка
  `r.x + r.width/2 <= window.innerWidth`.

## Правка метаданных залитого ролика (`yt_studio.set_video_meta`) — 05.08.2026

Страница `/video/{id}/edit`. Поля лежат как `div#textbox` внутри
`ytcp-social-suggestions-textbox`, различаются по `aria-label`:
- заголовок — «Add a title that describes your video…»;
- описание — «Tell viewers about your video…».

Ввод — тот же единственный рабочий способ, что и для таймкодов рекламы: CDP-клик мышью
по координатам → `document.execCommand('selectAll')` → `Input.insertText`.
**Кнопка Save появляется ТОЛЬКО после фактической правки** — искать её до изменения поля
бесполезно, её нет в DOM; неактивная Save означает, что ввод не приняли.

---

## 2f. Дампы 2026-08-07 — что снято живьём после разбора провала 18 заливок

Повод: прогон 07.08 объявил провальными 18 заливок, у которых файл был долит,
а отложка выставлена и подтверждена. Разбор показал, что вердикт брался из ТЕКСТА
финального диалога и поиска по заголовку. Ниже — всё, что снято дампом взамен.

### Мастер отдаёт videoId ещё до конца загрузки ⭐ главное

```
ytcp-uploads-dialog.videoId                     = 'DEMO0000005'
ytcp-uploads-dialog.uploadDetailsByFrontendId[*].videoId   — то же значение
```
Замер: пусто в первые ~4 с после `send_keys`, заполнено к ~8 с, шаг ещё `DETAILS`,
`isVideoUploaded=false`. То есть ролик можно вести ПО ID с самого начала — не искать
его потом по заголовку (а именно поиск по заголовку и рушил вердикт: Studio отдаёт
диакритику в NFD, плюс список канала обновляется с задержкой).
В коде: `read_wizard()['videoId']` → `upload_worker._wizard_vid`.

### Файловый инпут мастера

```
input < div < ytcp-uploads-file-picker < div < tp-yt-paper-dialog < ytcp-uploads-dialog
```
Живёт в light DOM, достаётся обычным `querySelector`. Важно брать именно его:
на странице канала есть и другие `input[type=file]` (обложка, баннер).
`SEL["file_input"] = 'ytcp-uploads-dialog input[type="file"]'`

### Вопрос «для детей» отрисован ДВАЖДЫ ⚠️ ломало мастер наглухо

На шаге Details видимых радио **четыре** — две одинаковые пары
`VIDEO_MADE_FOR_KIDS_MFK` / `VIDEO_MADE_FOR_KIDS_NOT_MFK`. Ответ только на первую
оставляет `isCurrentStepInvalid=true` и `#next-button` заблокированной — мастер
НИКОГДА не доходит до Visibility. Проверено живьём: клик по второй паре снимает invalid.
`set_not_made_for_kids` теперь жмёт все неотмеченные и проверяет результат по состоянию.

### Три разных `tp-yt-paper-dialog` — различать по классу, не по тексту

| Что | tag / id | class | признак |
|---|---|---|---|
| сам мастер | `tp-yt-paper-dialog#dialog` | `style-scope ytcp-uploads-dialog` | **содержит ссылку youtu.be внутри** |
| «We're still checking your content» | `tp-yt-paper-dialog#dialog` | `style-scope ytcp-dialog` | `ytcp-button#primary-action-button` («Got it») |
| подтверждение публикации | отдельный `tp-yt-paper-dialog` | НЕ `ytcp-uploads-dialog` | ссылка `youtu.be/<id>` + текст «Video scheduled…» |

⚠️ Признак «есть ссылка youtu.be → успех» БЕЗ исключения мастера по классу даёт ложный
успех ещё до финализации — мастер тоже держит ссылку на ролик.

### Удаление ролика — полностью машинный путь (меню строки списка больше нет)

```
/video/{id}/edit
  ytcp-icon-button#overflow-menu-button        (aria-label «Options»)
  tp-yt-paper-item[test-id="VIDEO_DELETE"]     ← test-id, язык интерфейса не важен
  ytcp-checkbox-lit#confirm-checkbox
  ytcp-button#confirm-button                   aria-disabled: true → false после галки
  ytcp-button#cancel-button
```
Соседние пункты меню для ориентира: `VIDEO_PROMOTE`, `VIDEO_BRAINSTORM_IDEA`.
В строке списка `ytcp-video-row` кнопок нет вообще (67 элементов внутри, 0 кнопок) —
ни до, ни после наведения настоящей мышью. Старый путь через hover оставлен фолбэком.

### Аккаунты профиля читаются машинно

```
ytd-account-item-renderer .data → accountName / channelHandle / isSelected
  serviceEndpoint…selectActiveIdentityEndpoint → authuser=N
пункт «Switch account»: data.icon.iconType === 'SWITCH_ACCOUNTS'
```
`SEL_ACCOUNTS`, `read_accounts()`, `authuser_for_channel()`.

### ⚠️ Пустой список видео — это «не прочитал», а не «роликов нет»

Замер 07.08 на канале с 30 роликами, санитизированный профиль 9001:
```
/channel/{UC}/videos/upload              → 0 строк
/channel/{UC}/videos/upload?authuser=0   → 0 строк
/channel/{UC}/videos/upload?authuser=1   → 30 строк, наш ролик SCHEDULED
```
Без верного номера страница пустая и БЕЗ ошибки. Принять пустоту за факт = объявить
ролик потерянным (или, в дедупе, залить второй раз). `yt_studio.wait_videos()`.

### ⚠️ Диалог проверок приходит в ДВУХ видах — id значат противоположное [дамп 2026-08-08]

```
вид А:  ytcp-button#primary-action-button   = «Got it»          → просто закрыть
вид Б:  ytcp-button#primary-action-button   = «Go back»    (!)  → ОТМЕНА публикации
        ytcp-button#secondary-action-button = «Publish anyway»  → опубликовать сразу
```
Оба живут в `tp-yt-paper-dialog` с классом `ytcp-dialog` и текстом «We're still checking
your content». Отличать по НАЛИЧИЮ `#secondary-action-button`: если он есть — это вид Б,
и жать надо именно его. Прежний код жал `#primary-action-button` вслепую и на виде Б
сам отменял публикацию, после чего докладывал «ролика на канале нет».
