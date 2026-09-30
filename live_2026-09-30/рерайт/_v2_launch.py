# -*- coding: utf-8 -*-
"""На ПРОД-БОКСЕ: обновить 12 пресетов из bundle_v2 + запустить 5 джоб по семени №16 (opus-4-6)."""
import json, sqlite3, sys, urllib.request, urllib.parse, urllib.error

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

bundle = json.load(open(f"{SRV}/_bundle_v2.json", encoding="utf-8"))

# Шаг 1: обновить все 12 пресетов (POST по имени — существующий id переиспользуется)
presets=[]
for b in bundle:
    pres, err = call("/presets/" + urllib.parse.quote(b["name"]), {
        "name": b["name"], "target_length_words": b["words"],
        "content": b["preset"], "language": b["language"],
    })
    if err:
        print(f"FAIL preset {b['name']}: {err}"); continue
    presets.append({"name":b["name"],"preset_id":pres["id"],"language":b["language"],"words":b["words"]})
    print(f"PRESET UPD  {b['name']:<20} id={pres['id']:<20} {len(b['preset'])}c")
json.dump(presets, open(f"{SRV}/_ww2_presets.json","w",encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"ПРЕСЕТОВ ОБНОВЛЕНО: {len(presets)}/12\n")

# Шаг 2: 5 джоб по семени №16
pmap={p["name"]:p for p in presets}
results=[]
for b in bundle:
    if not b.get("seed16"): continue
    p=pmap.get(b["name"])
    if not p:
        print(f"SKIP {b['name']}: пресет не обновился"); continue
    job, err = call("/jobs/generation", {
        "preset_id": p["preset_id"], "topic": b["seed16"], "language": b["language"],
        "model": MODEL, "oauth_tokens": tokens, "target_length_words": b["words"],
    })
    flag=" ⛔" if b.get("seed16_flag") else ""
    if err:
        print(f"FAIL job {b['name']}: {err}"); results.append({"name":b["name"],"seed":16,"error":err}); continue
    results.append({"name":b["name"],"seed":16,"preset_id":p["preset_id"],"job_id":job["job_id"],
                    "language":b["language"],"words":b["words"],"flagged":bool(b.get("seed16_flag"))})
    print(f"JOB OK  {b['name']:<20} #16{flag:<3} job={job['job_id']}")
json.dump(results, open(f"{SRV}/_ww2_launch_result_seed16.json","w",encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"\nЗАПУЩЕНО seed16: {len([r for r in results if r.get('job_id')])}/5")
