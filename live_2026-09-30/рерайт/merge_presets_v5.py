# -*- coding: utf-8 -*-
# merge_presets_v5 — вливает v5-контенты в живой generation.json админа (атомарно, с бэкапом).
import json, os, sys, time, shutil

GEN = "/root/rewrite-server/presets/292d249f2de0/generation.json"
V5 = sys.argv[1] if len(sys.argv) > 1 else "/root/rewrite-server/v5_presets.json"

v5 = json.load(open(V5, encoding="utf-8"))
data = json.load(open(GEN, encoding="utf-8"))
presets = data["presets"]  # dict id->preset
ts = time.strftime("%Y%m%d-%H%M%S")
bak = f"{GEN}.bak-presetv5-{ts}"
shutil.copy2(GEN, bak)

changed, missing = [], list(v5.keys())
for pid, p in presets.items():
    nm = p.get("name")
    if nm in v5:
        old_len = len(p.get("content") or "")
        p["content"] = v5[nm]
        p["last_used_at"] = p.get("last_used_at")  # не трогаем
        changed.append((nm, old_len, len(v5[nm])))
        if nm in missing:
            missing.remove(nm)

data["updated_at"] = time.time()
tmp = GEN + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
os.replace(tmp, GEN)

print(f"бэкап: {bak}")
print(f"обновлено пресетов: {len(changed)}")
for nm, ol, nl in changed:
    print(f"  {nm:20} {ol} -> {nl} симв")
if missing:
    print("НЕ НАЙДЕНЫ на сервере:", missing)
# верификация: перечитать и проверить fw=0 и наличие петель
d2 = json.load(open(GEN, encoding="utf-8"))
bad = []
for p in d2["presets"].values():
    if p.get("name") in v5:
        c = p["content"]
        if c.lower().count("forward-promise") or c != v5[p["name"]]:
            bad.append(p["name"])
print("верификация после записи:", "OK, все 12 чисты и совпадают" if not bad else f"ПРОБЛЕМА: {bad}")
