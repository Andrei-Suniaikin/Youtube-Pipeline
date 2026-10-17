# Деплой

Пока здесь только тревоги: остальные сервисы разворачиваются как раньше — копированием
файлов и `systemctl restart <сервис>` (`vsgen`, `rewrite-server`, `cover-server`).

Единая студия обновляется скриптом `update.sh`. Выкатка **без простоя** (два экземпляра
`studio-platform@a`/`@b`, переключение Caddy, замок движка) — `BLUE_GREEN.md`.

## Тревоги

```bash
sudo cp deploy/studio-alerts.service deploy/studio-alerts.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now studio-alerts.timer
```

Проверка:

```bash
systemctl list-timers studio-alerts.timer     # когда следующий проход
journalctl -u studio-alerts -n 50             # что говорил последний
```

Токен и `chat_id` — в `.env` рядом с кодом, в git он не попадает. `EnvironmentFile`
указан с минусом: без файла юнит не падает, тревоги просто не отправляются, и это
видно на экране «Админ» строкой «Телеграм не настроен».

Пути в юните (`/srv/studio`, `/data/tenants`, пользователь `studio`) — предположение
по устройству production; перед первой установкой сверить с машиной.

## Чего здесь намеренно нет

Юнитов для кабинета, Rewrite и Cover. Они уже развёрнуты и работают; переписывать
их запуск заодно с добавлением тревог — это менять то, что не просили, в момент,
когда проверить нечем.
