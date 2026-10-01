import os, sqlite3, subprocess

DB = "/root/VideoStudio/server/vsgen.db"


def minutes(path):
    if not path or not os.path.exists(path):
        return None
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return round(float(out) / 60, 1)
    except Exception:
        return None


c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
rows = c.execute(
    "SELECT j.uc, j.video_path, j.error, j.verdict, ch.name, ch.profile_id "
    "FROM pub_upload_jobs j LEFT JOIN pub_channels ch ON ch.uc=j.uc "
    "WHERE ch.profile_id IN (22,21) AND (j.error LIKE 'PHONE_VERIFICATION%' "
    "OR j.verdict IN ('scheduled','published')) AND j.finished > strftime('%s','now')-604800 "
    "ORDER BY ch.name").fetchall()
print("%-22s %-8s %-9s %s" % ("канал", "профиль", "минут", "итог"))
for r in rows:
    m = minutes(r["video_path"])
    itog = "ОТБИТ (телефон)" if (r["error"] or "").startswith("PHONE") else "залит"
    print("%-22s %-8s %-9s %s" % ((r["name"] or "?")[:22], r["profile_id"],
                                  m if m is not None else "файла нет", itog))
c.close()
