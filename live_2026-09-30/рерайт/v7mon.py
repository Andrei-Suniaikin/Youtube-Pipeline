import sqlite3, json, time
JOB=open("_v7_jobid.txt").read().strip()
for i in range(40):
    con=sqlite3.connect("job_queue.db"); con.row_factory=sqlite3.Row
    r=con.execute("SELECT data,status,log FROM jobs WHERE id=?", (JOB,)).fetchone(); con.close()
    if not r:
        print("job исчез"); break
    d=json.loads(r["data"]); st=r["status"]
    pp=d.get("progress_percent"); pd=str(d.get("progress_detail") or "")[:75]
    print("[%dm] %s %s%% %s" % (i, st, pp, pd), flush=True)
    if st in ("completed","failed","interrupted"):
        log=r["log"] or ""
        for l in log.split("\n"):
            if "v7" in l or "🧭" in l or "Conv turn" in l or "🧹" in l:
                print("  ", l[:130])
        print("RECOVERY:", log.count("length recovery"), "| err:", d.get("error"))
        fs=d.get("output_files") or []
        if isinstance(fs,str):
            try: fs=json.loads(fs)
            except: fs=[fs]
        print("FILE:", fs[0] if fs else "—")
        break
    time.sleep(60)
