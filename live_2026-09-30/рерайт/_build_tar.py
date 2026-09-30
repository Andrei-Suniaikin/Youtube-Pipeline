# -*- coding: utf-8 -*-
"""Собирает tar.gz готовых WW2-текстов, разложенных по seedN/ (completed) и partial/."""
import json, os, tarfile
SRV="/root/rewrite-server"; outdir=os.path.join(SRV,"outputs/292d249f2de0")
dl=json.load(open(os.path.join(SRV,"_download_manifest.json"),encoding="utf-8"))
tarpath=os.path.join(SRV,"_ww2_gotovye.tar.gz")
n=0
with tarfile.open(tarpath,"w:gz") as tf:
    for label,seed,name,st,f in dl:
        src=os.path.join(outdir,f)
        if not os.path.isfile(src): continue
        sub = f"partial/seed{seed}" if st=="partial" else f"seed{seed}"
        arc=os.path.join("WW2_готовые",sub,f)
        tf.add(src,arcname=arc)
        n+=1
print("файлов в архиве:",n,"| размер:",os.path.getsize(tarpath)//1024,"KB")
print("path:",tarpath)
