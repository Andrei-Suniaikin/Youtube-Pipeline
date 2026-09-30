import json, re as _re
src = open("rewrite_core.py", encoding="utf-8").read()
ns = {"json": json, "re": _re}
from typing import Optional, Dict, Any, List, Tuple
ns.update(dict(Optional=Optional, Dict=Dict, Any=Any, List=List, Tuple=Tuple))
for fn in ("_normalize_literal_escapes", "_parse_truncated_chunk_json", "parse_chunk_response",
           "_gen_count_script_words", "_gen_merge_continuation"):
    m = _re.search(r"^def " + fn + r"\(.*?(?=^def |^class )", src, _re.M | _re.S)
    exec(m.group(0), ns)

bad1 = '{"script": "Prvni cast textu se „spatnou" uvozovkou ktera lame JSON."}\n{"script": "Druha cast nesmi vleteti."}'
bad2 = '{"script": "Prvni cast " lame."} {"script": "Druha!"}'
for i, bad in enumerate((bad1, bad2), 1):
    p = ns["parse_chunk_response"](bad, expect_funnel=False)
    assert p and "Druha" not in p["script"], f"тест{i}: второй объект утёк: " + repr(p and p["script"])[:100]
    assert "}" not in p["script"], f"тест{i}: скобка в теле"
    print(f"тест{i} OK:", repr(p["script"][:55]))

from gen_output_cleaner import clean_generated_text as c
t, r = c('Konec vety."]')
assert not t.endswith("]"), repr(t)
print("cleaner-хвост OK:", repr(t))
