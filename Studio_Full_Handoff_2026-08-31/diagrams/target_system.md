# Целевая система

```text
 Web UI / Windows client / Partner API
                  │
                  ▼
        ┌──────────────────────┐
        │   Platform API       │
        │ auth · plan · release│
        └──────────┬───────────┘
                   │ one transaction: state + outbox
                   ▼
        ┌──────────────────────┐       ┌──────────────────┐
        │ PostgreSQL           │       │ Object storage   │
        │ stages · attempts    │◄─────►│ texts · media    │
        │ leases · receipts    │       │ SHA/validation   │
        └──────────┬───────────┘       └──────────────────┘
                   │ claim / result / heartbeat
          ┌────────┼───────────┬───────────────┐
          ▼        ▼           ▼               ▼
       Rewrite    TTS      Images/Cover      Render agent
       workers  workers       workers        versioned edge
          │        │           │               │
          └────────┴─────┬─────┴───────────────┘
                         │ validated artifacts
                         ▼
                   Upload agent
             global AdsPower profile lease
                         │
                         ▼
           scheduled receipt → PUBLIC proof

 Shared services: provider capacity · secret store · recovery · metrics · audit
```

UI не вызывает worker/provider напрямую. Worker не меняет соседнюю stage. Все связи проходят
через release IDs, immutable manifests, scoped claims и проверенные receipts.
