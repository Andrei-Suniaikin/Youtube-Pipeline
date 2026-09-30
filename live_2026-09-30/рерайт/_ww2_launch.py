# -*- coding: utf-8 -*-
"""Запускается НА ПРОД-БОКСЕ. Аккаунт Morbie (292d249f2de0).
  mode=presets : создать/обновить 12 ген-пресетов
  mode=jobs    : запустить по 1 ген-джобе на семя №1 (для пресетов из _ww2_presets.json)
  mode=all     : оба шага
Секреты (токены/сессия) не печатает."""
import json, sqlite3, sys, urllib.request, urllib.parse, urllib.error

SRV   = "/root/rewrite-server"
BASE  = "http://127.0.0.1:8899"
ADMIN = "292d249f2de0"
MODEL = "claude-opus-4-6"
MODE  = sys.argv[1] if len(sys.argv) > 1 else "all"
sys.path.insert(0, SRV)

from auth import LicenseManager
lm  = LicenseManager(f"{SRV}/sessions.db")
sid = lm._create_session(ADMIN, "Админ", "127.0.0.1")
conn = sqlite3.connect(f"{SRV}/sessions.db")
row  = conn.execute("SELECT oauth_tokens FROM user_keys WHERE user_id=?", (ADMIN,)).fetchone()
tokens = json.loads(row[0]) if row and row[0] else []
if not tokens:
    print("FATAL: у админа нет oauth_tokens"); sys.exit(2)
print(f"session minted: yes | tokens: {len(tokens)} | mode: {MODE}")

HDR = {"X-Session-Id": sid, "Content-Type": "application/json"}
def call(path, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req  = urllib.request.Request(BASE + path, data=data, headers=HDR, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8')[:300]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

bundle = json.load(open(f"{SRV}/_ww2_bundle.json", encoding="utf-8"))

if MODE in ("presets", "all"):
    presets = []
    for b in bundle:
        name = b["name"]
        pres, err = call("/presets/" + urllib.parse.quote(name), {
            "name": name, "target_length_words": b["words"],
            "content": b["preset"], "language": b["language"],
        })
        if err:
            print(f"FAIL preset {name}: {err}"); continue
        presets.append({"name": name, "preset_id": pres["id"], "language": b["language"],
                        "words": b["words"], "seed1": b["seed1"]})
        print(f"PRESET OK  {name:<22} id={pres['id']}  lang={pres.get('language')}  words={pres.get('target_length_words')}")
    json.dump(presets, open(f"{SRV}/_ww2_presets.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nПРЕСЕТОВ СОЗДАНО: {len(presets)}/12")

if MODE in ("jobs", "all"):
    presets = json.load(open(f"{SRV}/_ww2_presets.json", encoding="utf-8"))
    results = []
    for p in presets:
        job, err = call("/jobs/generation", {
            "preset_id": p["preset_id"], "topic": p["seed1"], "language": p["language"],
            "model": MODEL, "oauth_tokens": tokens, "target_length_words": p["words"],
        })
        if err:
            print(f"FAIL job {p['name']}: {err}"); results.append({**{k:p[k] for k in ('name','preset_id')}, "error": err}); continue
        jid = job["job_id"]
        results.append({"name": p["name"], "preset_id": p["preset_id"], "job_id": jid,
                        "language": p["language"], "words": p["words"]})
        print(f"JOB OK  {p['name']:<22} job={jid}")
    json.dump(results, open(f"{SRV}/_ww2_launch_result.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nДЖОБ ЗАПУЩЕНО: {len([r for r in results if r.get('job_id')])}/{len(presets)}")
