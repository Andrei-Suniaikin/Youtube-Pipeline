import sqlite3, json, time
JOB=open("_final_jobid.txt").read().strip()
for i in range(40):
    con=sqlite3.connect("job_queue.db"); con.row_factory=sqlite3.Row
    r=con.execute("SELECT status,log,data FROM jobs WHERE id=?", (JOB,)).fetchone(); con.close()
    st=r["status"]
    if st in ("completed","failed","interrupted"):
        log=r["log"] or ""
        for l in log.split("\n"):
            if any(k in l for k in ("v7","🧭","✅ v7","🧹","✂️","Done:","RECOVERY")):
                print("  ", l[:135])
        print("RECOVERY:", log.count("length recovery"))
        d=json.loads(r["data"]); fs=d.get("output_files") or []
        if isinstance(fs,str):
            try: fs=json.loads(fs)
            except: fs=[fs]
        print("FINAL", st, "| FILE:", fs[0] if fs else "—", "| err:", d.get("error"))
        break
    time.sleep(50)
else: print("ещё идёт")
