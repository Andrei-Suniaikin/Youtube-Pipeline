# Текущая система

```text
                         ┌─────────────────────┐
 donor URL / source.txt ─►    Rewrite Studio   │
                         │ text + checkpoints  │
                         └──────────┬──────────┘
                                    │ script
                                    ▼
┌──────────────────┐       ┌─────────────────────┐
│ Desktop VideoStudio│     │ Web VideoStudio     │
│ Tkinter + workers ├─ ─ ─ ►│ projects + queues   │
└─────────┬────────┘       │ + publication plane │
          │                 └──────┬──────┬───────┘
          │                        │      │
          │            TTS/images/video  │ render claim
          │                        │      ▼
          │                        │  remote render node
          │                        │      │ MP4
          │                        ▼      ▼
          │                 project filesystem
          │                        │
          │                        ├──────────────► Cover Studio
          │                        │               image + text
          │                        │
          └────────────────────────┴──────────────► Upload agent
                                                   AdsPower + YouTube Studio
                                                        │
                                                        ▼
                                                  exact receipt/check
```

Пунктир Desktop → Web — только опциональный bridge для двух Edge TTS routes. Desktop projects,
остальные стадии и общий pipeline с Web не синхронизируются; при ошибке bridge Desktop молча
возвращается к локальному Edge TTS.

Три сайта имеют отдельные приложения, отдельную авторизацию и отдельные SQLite-базы, но
делят одну машину, часть диска и некоторые аккаунты нейросетей. Загрузка и тяжёлый рендер
могут выполняться на других машинах.
