# -*- coding: utf-8 -*-
"""Запуск на ПРОД-БОКСЕ: семена №3 и №4 на каждый из 12 WW2-каналов, opus-4-6 (24 джобы).
Помеченные ⛔ тоже генерим (по указанию юзера)."""
import json, sqlite3, sys, urllib.request, urllib.error

SRV="/root/rewrite-server"; BASE="http://127.0.0.1:8899"; ADMIN="292d249f2de0"; MODEL="claude-opus-4-6"
sys.path.insert(0, SRV)
from auth import LicenseManager
sid = LicenseManager(f"{SRV}/sessions.db")._create_session(ADMIN,"Админ","127.0.0.1")
tokens = json.loads(sqlite3.connect(f"{SRV}/sessions.db").execute(
    "SELECT oauth_tokens FROM user_keys WHERE user_id=?", (ADMIN,)).fetchone()[0])
if not tokens:
    print("FATAL: нет oauth_tokens"); sys.exit(2)
print(f"session ok | tokens={len(tokens)} | model={MODEL}")

HDR={"X-Session-Id":sid,"Content-Type":"application/json"}
def call(path, body):
    req=urllib.request.Request(BASE+path, data=json.dumps(body,ensure_ascii=False).encode(),
                               headers=HDR, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

presets = {p["name"]: p for p in json.load(open(f"{SRV}/_ww2_presets.json", encoding="utf-8"))}
seeds   = {s["name"]: s for s in json.load(open(f"{SRV}/_seed34_map.json", encoding="utf-8"))}

results=[]
for name, p in presets.items():
    s = seeds.get(name)
    if not s:
        print(f"SKIP {name}: нет seed-данных"); continue
    for n in (3, 4):
        topic = s.get(f"seed{n}")
        if not topic:
            print(f"SKIP {name} #{n}: пусто"); continue
        job, err = call("/jobs/generation", {
            "preset_id": p["preset_id"], "topic": topic, "language": p["language"],
            "model": MODEL, "oauth_tokens": tokens, "target_length_words": p["words"],
        })
        flag = " ⛔" if s.get(f"seed{n}_flag") else ""
        if err:
            print(f"FAIL {name} #{n}: {err}"); results.append({"name":name,"seed":n,"error":err}); continue
        jid=job["job_id"]
        results.append({"name":name,"seed":n,"preset_id":p["preset_id"],"job_id":jid,
                        "language":p["language"],"words":p["words"],"flagged":bool(s.get(f"seed{n}_flag"))})
        print(f"JOB OK  {name:<18} #{n}{flag:<3} job={jid}")

json.dump(results, open(f"{SRV}/_ww2_launch_result_seed34.json","w",encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"\nЗАПУЩЕНО seed3+4: {len([r for r in results if r.get('job_id')])}/24")
