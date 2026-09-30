import json, glob, os
base = "/root/manga_rewrite/projects_web/projects"
known = {"Хару Манхва","Кофе и Манхва","Реванш Манхва","S-Ранг Манхва","La Rivincita Manhwa",
"Il Risveglio Manhwa","Cuori di Manhwa","Amor en Viñetas","Renacer Manhwa","Mazmorra Cero",
"Manhwa a Dois","Rank S Narrado","Drugie Życie Manhwy","Dragoste în Manhwa","Brama Potworów",
"Manhwa o Lásce","Návrat Nejsilnějšího","Randka z Manhwą","Renăscut Mai Puternic","Volta por Cima Manhwa"}
for v in sorted(glob.glob(base + "/*/out/video.mp4"), key=os.path.getmtime, reverse=True):
    pid = v.split("/")[-3]
    try:
        p = json.load(open(os.path.join(base, pid, "project.json")))
    except Exception:
        p = {}
    if p.get("channel_name") in known:
        continue
    print("%s  канал=%r  code=%r  язык=%s  мин=%s  %dМБ  %s" % (
        pid[:8], p.get("channel_name"), p.get("channel"), p.get("language"),
        p.get("minutes"), os.path.getsize(v) / 1048576,
        __import__("time").strftime("%d.%m", __import__("time").localtime(os.path.getmtime(v)))))
