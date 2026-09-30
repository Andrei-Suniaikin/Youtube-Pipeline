import json, sqlite3, sys
sys.path.insert(0, "/root/rewrite-server")
import queue as _q
lq = _q.Queue()
tokens = json.loads(sqlite3.connect("/root/rewrite-server/sessions.db").execute(
    "SELECT oauth_tokens FROM user_keys WHERE user_id=?", ("292d249f2de0",)).fetchone()[0])
tok = tokens[0] if tokens else ""
from gen_editor import edit_for_retention
text = open("_edit_test.txt", encoding="utf-8").read()
edited, rep = edit_for_retention(text, "Polish", "claude-opus-4-8", tok, lq)
open("_edit_test.out.txt", "w", encoding="utf-8").write(edited)
print("ДО:", len(text.split()), "слов | ПОСЛЕ:", len(edited.split()), "слов")
for r in rep: print(" ", r)
# слить лог-очередь
while not lq.empty():
    print("  LOG:", str(lq.get())[:120])
