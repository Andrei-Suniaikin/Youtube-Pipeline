# -*- coding: utf-8 -*-
"""Завести каналы манхвы в кабинет — один раз.

Манхва до сих пор шла мимо кабинета: рендер делает своя служба, а заливка запускалась
руками скриптом на Минске. Каналов в pub_channels не было вообще, поэтому кабинет не мог
ни поставить задание, ни взять бронь профиля — и манхва конкурировала за профиль 21
с ВВ2 вслепую.

Статус канала здесь — не догадка, а факт из ответа YouTube:
  active — с канала уже уходил длинный ролик, значит телефон подтверждён;
  paused — YouTube отбил заливку кодом PHONE_VERIFICATION_REQUIRED_LONG_VIDEO.
Ролики манхвы идут по 40-84 минуты, а без подтверждения телефоном YouTube не принимает
длиннее 15 минут. Пауза снимается человеком после подтверждения номера.
"""
import json
import sqlite3
import sys
import time

DB = "/root/VideoStudio/server/vsgen.db"
TENANT = "3"
TZ = {"ru": "Europe/Moscow", "it": "Europe/Rome", "es": "Europe/Madrid",
      "pt": "Europe/Lisbon", "pl": "Europe/Warsaw", "ro": "Europe/Bucharest",
      "cs": "Europe/Prague"}

# имя, UC, язык, профиль, handle, статус
CHANNELS = [
    ("Хару Манхва",            "UCVREAS1oT4aFf6BT-ynTWFg", "ru", 21, "@harumanhwa",          "active"),
    ("Кофе и Манхва",          "UC-65TlNg-KKVRerxznVXTxw", "ru", 21, "@kofeimanhwa",         "active"),
    ("Реванш Манхва",          "UCrx3aBHIaQLj9gqHVlCPbig", "ru", 21, "@revanshmanhwa",       "active"),
    ("S-Ранг Манхва",          "UCOVvbQ7WpDjkSJRauTDTqaQ", "ru", 21, "@srangmanhwa",         "active"),
    ("La Rivincita Manhwa",    "UCHTVsWoaACKlafEY48WJeHw", "it", 21, "@larivincitamanhwa",   "active"),
    ("Il Risveglio Manhwa",    "UCqFMoZJCKj6YTaL83mbu-fg", "it", 21, "@ilrisvegliomanhwa",   "active"),
    ("Cuori di Manhwa",        "UCTH4S4cOW03YVg56m5xG4Hw", "it", 21, "@cuoridimanhwa",       "active"),
    ("Amor en Viñetas",        "UCjKcIgA61QZHHGfXf_DkMbQ", "es", 21, "@amorenvinetas",       "active"),
    ("Renacer Manhwa",         "UCAR9BruIUfXwB230MR1kZPQ", "es", 21, "@renacermanhwa",       "active"),
    ("Mazmorra Cero",          "UC-Btib4_4i4HQMXERjNM2zg", "es", 21, "@mazmorracero",        "active"),
    ("Manhwa a Dois",          "UCE0xGgS6YjxQMBfsiGCr0MA", "pt", 21, "@manhwaadois",         "active"),
    ("Rank S Narrado",         "UCznnds10NhwEMe3W9ZIXnkQ", "pt", 21, "@ranksnarrado",        "active"),
    ("Drugie Życie Manhwy",    "UCafmKg_VBZA0EBBRbVZ9G5A", "pl", 21, "@drugiezyciemanhwy",   "active"),
    ("Dragoste în Manhwa",     "UCfxG2eB-GJkBW1cRsyNaJiw", "ro", 21, "@dragosteinmanhwa",    "active"),
    # ── ждут подтверждения телефоном (YouTube отбил длинный ролик) ──
    ("Brama Potworów",         "UCrKKIu7pcDH8NwqMzKuksQQ", "pl", 21, "@bramapotworow",       "paused"),
    ("Manhwa o Lásce",         "UCKvI6_TOAzo_zMojM4an4fg", "cs", 21, "@manhwaolasce",        "paused"),
    ("Návrat Nejsilnějšího",   "UCDsbUuGOsWpKyT44hZ9Vukw", "cs", 21, "@navratnejsilnejsiho", "paused"),
    ("Randka z Manhwą",        "UCmSvl9Zb4iFqdvPMD-0U2Cw", "pl", 22, "@randkazmanhwa",       "paused"),
    ("Renăscut Mai Puternic",  "UC4dHbJ9cbajBD6FnKKACs4g", "ro", 21, "@renascutmaiputernic", "paused"),
    ("Volta por Cima Manhwa",  "UCx061eEgBSogxrmWxQwtIIA", "pt", 21, "@voltaporcimamanhwa",  "paused"),
]

NOTE = "производство своё (manga_rewrite), кабинет только заливает"


def main(apply_it):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    now = time.time()
    add = skip = 0
    word = "завожу" if apply_it else "завёл бы"
    for name, uc, lang, profile, handle, status in CHANNELS:
        if c.execute("SELECT 1 FROM pub_channels WHERE uc=?", (uc,)).fetchone():
            print("  уже есть: " + name)
            skip += 1
            continue
        print("  %s: %-24s %s  профиль %s  %s" % (word, name, lang, profile, status))
        add += 1
        if not apply_it:
            continue
        c.execute(
            "INSERT INTO pub_channels(tenant_id,uc,name,handle,lang,niche,profile_id,tz,"
            "schedule,horizon_days,video_mode,status,note,created,updated,"
            "lead_days,deferred,deferred_limit) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (TENANT, uc, name, handle, lang, "манхва", profile, TZ.get(lang, "Europe/Warsaw"),
             json.dumps({"*": ["10:00"]}), 18, "slideshow", status, NOTE, now, now, 2, 1, 5))
    if apply_it:
        c.commit()
    print("\n%s: %d, уже было: %d" % ("заведено" if apply_it else "к заведению", add, skip))
    c.close()


if __name__ == "__main__":
    main("--apply" in sys.argv)
