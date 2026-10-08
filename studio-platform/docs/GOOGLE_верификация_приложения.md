# Проверка приложения Google (OAuth verification) — от и до

Составлено 08.10.2026 для приложения **Vova Studio** (домен `vovastudio.work`).
Зачем: без проверки у приложения пожизненный потолок 100 подключённых Google-аккаунтов,
а каналов больше сотни и каждый на своём аккаунте. Плюс у каждого входа экран
«Google не проверил это приложение».

Правила Google меняются. Перед отправкой сверить шаги с официальной справкой:
<https://support.google.com/cloud/answer/13463073> (подготовка к проверке) и
<https://support.google.com/cloud/answer/13464321> (демо-видео и обоснования).

---

## 0. Что у нас уже есть, а чего нет

| Что | Состояние |
|---|---|
| Свой домен | ✅ `vovastudio.work` |
| Главная страница | ✅ <https://vovastudio.work/> |
| Политика конфиденциальности | ✅ <https://vovastudio.work/privacy> (все 9 пунктов YouTube API III.A.2) |
| Условия использования | ✅ <https://vovastudio.work/terms> (со ссылкой на условия YouTube) |
| Почта поддержки | `support@vovastudio.work` — **проверить, что письма на неё доходят** |
| Подтверждение домена в Search Console | ❓ проверить (шаг 1) |
| Логотип 120×120 | ❌ нужен |
| Демо-видео | ❌ нужно снять (шаг 6) |
| Свой адрес возврата после согласия | ⚠️ сейчас OAuth Playground — см. «Главный риск» |

Права, которые просим, — **ровно два**, оба «чувствительные» (sensitive), не
«ограниченные» (restricted). Поэтому платного аудита безопасности (CASA) **не будет**,
только ручная проверка Google: обычно от 3–5 рабочих дней до нескольких недель.

| Право | Зачем оно студии |
|---|---|
| `https://www.googleapis.com/auth/yt-analytics.readonly` | читать статистику своего канала: просмотры, время просмотра, средняя длительность и процент досмотра, подписчики (+/−), лайки, комментарии, репосты — по дням |
| `https://www.googleapis.com/auth/youtube.readonly` | Google требует его для метода `reports.query` YouTube Analytics API вместе с аналитическим правом. Запись в канал оно не даёт |

---

## ⚠️ Главный риск — сделать ДО подачи

Сейчас токен получают через **Google OAuth Playground**
(`https://developers.google.com/oauthplayground`) — это адрес возврата на чужом домене.
У проверяемого приложения все адреса возврата и источники должны быть на
**подтверждённых доменах** самого приложения. Ревьюер увидит в списке
`developers.google.com` и почти наверняка вернёт заявку.

Что нужно: кнопка «Подключить канал» в студии, которая сама открывает окно согласия
Google и принимает ответ на нашем адресе, например
`https://vovastudio.work/oauth/callback`. Тогда:
- адрес Playground из «Authorized redirect URIs» убирается;
- демо-видео снимается на нашем собственном потоке, а не на служебной странице Google;
- ребятам не нужно копировать refresh token руками.

Это доработка студии — делаю по твоему слову, примерно полдня с проверкой.

---

## 1. Подтвердить домен в Search Console

1. Открыть <https://search.google.com/search-console> **тем Google-аккаунтом, который
   является владельцем (Owner) или редактором проекта в Google Cloud**. Это важно:
   Google сверяет, что домен подтверждал участник проекта.
2. «Добавить ресурс» → тип **«Домен»** → `vovastudio.work`.
3. Google покажет TXT-запись вида `google-site-verification=...`.
4. У регистратора домена (где куплен `vovastudio.work`) → DNS → добавить запись:
   тип `TXT`, имя `@` (или пусто), значение — строка от Google.
5. Подождать 5–30 минут (иногда до суток) → «Подтвердить».

Проверка: в Search Console ресурс `vovastudio.work` без пометки «не подтверждён».

---

## 2. Google Auth Platform → Branding

<https://console.cloud.google.com/auth/branding> (выбрать наш проект сверху).

| Поле | Что вписать |
|---|---|
| App name | `Vova Studio` — **ровно так же**, как на страницах сайта |
| User support email | почта, которую читают |
| App logo | квадрат 120×120 PNG/JPG, без чужих логотипов (никакого логотипа YouTube) |
| Application home page | `https://vovastudio.work/` |
| Application privacy policy link | `https://vovastudio.work/privacy` |
| Application terms of service link | `https://vovastudio.work/terms` |
| Authorized domains | `vovastudio.work` |
| Developer contact information | почта, которую читают: Google пишет именно туда |

Адреса вписывать **символ в символ**: со `https://`, без `www`, без лишнего `/` в конце
у privacy и terms. Расхождение ссылки на сайте и ссылки здесь — частая причина отказа.

Логотип: если его загрузить, Google проверяет и его. Можно подать без логотипа — тогда
экран согласия будет без картинки, но проверка проще.

---

## 3. Audience

<https://console.cloud.google.com/auth/audience>

1. User type — **External**.
2. Publishing status — нажать **Publish app**, чтобы стало **In production**.
   В статусе Testing токены умирают через 7 дней, и подать на проверку нельзя.
3. Там же видно счётчик пользователей — запомнить число (для отчёта).

---

## 4. Data access (права)

<https://console.cloud.google.com/auth/scopes>

1. Оставить **только** два права из таблицы выше. Всё лишнее удалить: каждое
   неиспользуемое право — повод для отказа («request the minimum scopes»).
2. Для каждого права Google попросит обоснование. Текст ниже, вставлять как есть
   (по-английски — ревьюеры читают по-английски).

**`yt-analytics.readonly`:**

> Vova Studio is an internal production dashboard for the channel owner. The owner
> connects each of their own YouTube channels once, and the app reads that channel's
> daily YouTube Analytics report (views, estimated minutes watched, average view
> duration and percentage, subscribers gained/lost, likes, comments, shares) via
> `youtubeAnalytics.reports.query` with `ids=channel==UC…`. The numbers are shown on the
> channel's statistics tab so the owner can see which videos and publishing schedules
> work. The data is never shared with third parties, never used for advertising, and
> is deleted when the owner disconnects the channel. The scope is read-only; the app
> cannot change anything on the channel.

**`youtube.readonly`:**

> The YouTube Analytics API documentation for `reports.query` requires the
> `youtube.readonly` scope together with an analytics scope for channel reports. We
> request it only for that purpose: it is read-only, the app does not upload, edit or
> delete anything, and does not read private data beyond what the analytics report
> needs. A broader scope such as `youtube` is not requested because it would grant
> write access we do not need.

---

## 5. Credentials — клиент OAuth

<https://console.cloud.google.com/apis/credentials> → наш OAuth client ID.

1. Тип — Web application.
2. Authorized redirect URIs — **только адреса на `vovastudio.work`** (см. «Главный
   риск»). Адрес OAuth Playground убрать перед подачей.
3. Authorized JavaScript origins — пусто или `https://vovastudio.work`.

---

## 6. Демо-видео — что снимать, по кадрам

**Формат.** Одно видео, 2–5 минут, загрузить на YouTube с доступом **«По ссылке»**
(Unlisted). Интерфейс и экран согласия Google — **на английском**: перед записью
переключить язык аккаунта/браузера или добавить к адресу согласия `&hl=en`. Без
монтажных склеек в ключевом месте (сам поток согласия — одним куском). Озвучка не
обязательна, но короткие подписи или голос помогают.

**Что Google хочет увидеть обязательно:**
1. Что приложение делает и где у него главная страница.
2. Весь поток входа: экран согласия Google с **названием приложения** и **списком
   прав**.
3. **Адресную строку с `client_id`** на экране согласия — по ней сверяют, что видео
   про тот самый проект.
4. Как именно используются данные **каждого** запрошенного права в приложении.

**Сценарий по кадрам:**

| # | Что на экране | Что сказать/подписать |
|---|---|---|
| 1 | Открыть `https://vovastudio.work/` | «Vova Studio — dashboard for our own YouTube channels» |
| 2 | Прокрутить вниз, кликнуть Privacy Policy, затем Terms | показать, что страницы открываются |
| 3 | Войти в студию, открыть список каналов, выбрать канал | «Each owner connects their own channel» |
| 4 | Нажать «Подключить канал / Connect channel» | — |
| 5 | **Окно Google: выбор аккаунта** → выбрать аккаунт-владелец канала | — |
| 6 | **Экран согласия:** видно `Vova Studio`, оба права. **Курсором показать адресную строку** с `client_id=…apps.googleusercontent.com` | «Consent screen: app name and the two read-only scopes» |
| 7 | Если вылез экран «Google hasn't verified this app» — это нормально до проверки, пройти «Advanced → Go to Vova Studio» | — |
| 8 | Нажать «Allow / Разрешить», вернуться в студию на свой домен | показать адресную строку `vovastudio.work/...` |
| 9 | Вкладка статистики канала: график по дням, просмотры, время просмотра, досмотр, подписчики | «This is the data from yt-analytics.readonly» — назвать метрики вслух/подписью |
| 10 | Показать, что записать что-либо в канал нельзя (нет кнопок загрузки/правки, только чтение) | «youtube.readonly is required by reports.query; no write access» |
| 11 | Показать «Отключить канал» и что данные удаляются | «Owner can disconnect at any time, data is deleted» |

**Что НЕ показывать:** пароли, refresh token, секреты 2FA, чужие каналы, внутренние
адреса серверов. Перед записью закрыть лишние вкладки и уведомления.

Если на момент записи нет своей кнопки «Подключить канал» (см. «Главный риск»), видео
придётся снимать через OAuth Playground — это ровно то, за что могут вернуть заявку.

---

## 7. Отправить на проверку

<https://console.cloud.google.com/auth/verification> (Verification Center) →
**Prepare for verification** (кнопка с твоего скрина).

1. Мастер пройдёт по тем же пунктам: Branding, домены, права, обоснования.
2. На шаге демо — вставить ссылку на видео из шага 6.
3. Поле «что делает приложение» — коротко, по-английски, тем же смыслом, что на
   главной странице.
4. Отправить. Статус станет **«In progress»** / «Verification in progress».

---

## 8. После отправки

- Google пишет на **Developer contact email**. Отвечать **в той же переписке**, а не
  новой заявкой, иначе очередь начинается заново.
- Частые вопросы ревьюера: «видео не показывает client_id», «права шире, чем нужно»,
  «страница политики не на том же домене», «название в согласии не совпадает с сайтом».
  Все они закрыты шагами выше.
- Пока идёт проверка, приложение работает как раньше: с экраном «не проверено» и
  потолком 100 аккаунтов.
- Итог виден в Verification Center: **Verified** — потолок снят, экран
  «не проверено» пропадает.

---

## Чек-лист перед кнопкой «Submit»

- [ ] Домен `vovastudio.work` подтверждён в Search Console аккаунтом-участником проекта
- [ ] Branding: имя, почта поддержки, три ссылки, authorized domain, контакт разработчика
- [ ] Audience: External, **In production**
- [ ] Data access: ровно `yt-analytics.readonly` и `youtube.readonly`, обоснования вставлены
- [ ] Credentials: в redirect URIs только адреса `vovastudio.work`, без OAuth Playground
- [ ] Демо-видео: английский интерфейс, `client_id` в адресной строке, оба права показаны в деле, «По ссылке»
- [ ] Почта поддержки и контактная почта реально принимают письма
