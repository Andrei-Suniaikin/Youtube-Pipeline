# -*- coding: utf-8 -*-
"""apply_v5_patches — точечные правки v5 в боевые server.py / rewrite_core.py (2026-07-18).

Патчит ПО ЯКОРЯМ (уникальные фрагменты кода), не по номерам строк — переживает дрейф прода.
Каждый патч: якорь должен встретиться РОВНО один раз, иначе патч (и весь файл) не применяется.
Повторный запуск безопасен: уже применённые патчи распознаются и пропускаются.

Использование (на сервере):
    python3 apply_v5_patches.py --server-py /root/rewrite-server/server.py \\
                                --core /root/rewrite-server/rewrite_core.py [--dry-run]

Что правит (10 патчей):
  server.py : S1 план части = ретеншн-карта · S2 reminder интереса + FINALE DISCIPLINE
              + гейт имени (пустое имя → нейтральное аутро) + CTA только при имени
  core      : C1 идемпотентный merge (recovery не вклеивает пере-нарратив)
              C2 salvage-клэмп шва "}{"script" · C3 recovery: добивка только новым
              C4 recovery last-turn: не заказывать второй sign-off
              C5 воронка: system = архитектор ретеншн-карты · C6 PLAN RULES = ретеншн-карта
              C7 INTEREST RULES в chunk-промпт · C8 план части + вход в сцену ≤250 слов
              C9 one-shot финал: гейт имени · C10 one-shot CTA: гейт имени
"""

import argparse
import py_compile
import shutil
import sys
import time

MARK = "PATCH-2026-07-18-v5"

# ─────────────────────────── server.py ───────────────────────────

S1_OLD = '''                    _parts.append(
                        "PLAN FOR THIS PART (from the funnel step) — follow it EXACTLY and write EACH beat to its "
                        "~word budget («Слов») so the length lands on target:\\n" + _plan_k
                    )'''

S1_NEW = '''                    # PATCH-2026-07-18-v5: план части = ретеншн-карта (петли/NEW/ре-хуки), не только бюджет слов.
                    _parts.append(
                        "PLAN FOR THIS PART (retention map from the funnel step) — follow it EXACTLY: each beat to its "
                        "~word budget («Слов»), each beat DELIVERS its NEW element, execute loop ops (OPEN = plant and "
                        "go silent; CLOSE = full payoff at the planned position; keep-open loops stay open past this "
                        "part unless closed here). Re-hooks are structural one-liners (chapter turn / irony-forward / "
                        "trivia spike), not questions:\\n" + _plan_k
                    )'''

S2_OLD = '''                if _is_last_k:
                    # PATCH-2026-06-27-funnel (item 5): аутро с именем ведущего и канала.
                    _parts.append("CLOSE the script: a short outro / sign-off that NAMES the host and the channel.")
                else:
                    _parts.append("CONTINUE the narrative — do not repeat content from previous chunks.")
                # PATCH-2026-06-27-funnel (item 5): ровно ОДИН байт на подписку — в центральном чанке.
                if _k == num_chunks // 2:
                    _parts.append(
                        "In THIS chunk, naturally insert ONE short call-to-action (like + subscribe) mentioning "
                        "the channel name — a single in-voice sentence, never jarring. (Only here.)"
                    )'''

S2_NEW = '''                # PATCH-2026-07-18-v5: короткое напоминание правил интереса в каждом continue-turn.
                _parts.append(
                    "INTEREST RULES from chunk 1 stay in force: every paragraph adds NEW information — never "
                    "re-state; one physical detail, then cut — no explaining what to feel, no praising your own "
                    "material; no 'remember this / we will return to it'; questions to the viewer are over after "
                    "the first 150 words; sentences 12-20 words with the turn inside, short fragments only at "
                    "peaks; uncertainty gets one sentence, not a seminar."
                )
                if _is_last_k:
                    # PATCH-2026-07-18-v5: FINALE DISCIPLINE + гейт имени (пустое имя → нейтральное аутро, не выдумка).
                    if channel_name_hint or host_name_hint:
                        _outro = "ONE short outro that NAMES the host and the channel"
                    else:
                        _outro = ("ONE short neutral outro in the narrator's voice; do NOT invent, mention or imply "
                                  "any channel name, host name, brand or narrator identity")
                    _parts.append(
                        "FINALE DISCIPLINE: deliver the title payoff exactly where the map places it (inside the "
                        "70-92% window of the WHOLE video). After the payoff: only NEW aftermath material — fates "
                        "of the people, consequences, what was found later — then " + _outro + ", and STOP. "
                        "FORBIDDEN after the payoff: recap, re-explaining the meaning, a second ending, returning "
                        "to earlier scenes, restating the paradox."
                    )
                else:
                    _parts.append("CONTINUE the narrative — do not repeat content from previous chunks.")
                # PATCH-2026-07-18-v5: CTA только если имя канала задано (иначе модель выдумывает бренд).
                if channel_name_hint and _k == num_chunks // 2:
                    _parts.append(
                        "In THIS chunk, naturally insert ONE short call-to-action (like + subscribe) mentioning "
                        "the channel name — a single in-voice sentence, never jarring. (Only here.)"
                    )'''

# ─────────────────────────── rewrite_core.py ───────────────────────────

# v6 (18.07): модель не умеет считать СЛОВА (инструкция «пересчитай слова» мертва),
# но отлично считает АБЗАЦЫ. Переводим бюджет длины в арифметику абзацев.
C22_OLD = '''    lo = int(target_words * 0.95)
    hi = int(target_words * 1.15)
    return (
        f"LENGTH — measured in WORDS (слова): the 'script' MUST contain between {lo} and {hi} words; aim for ~{target_words}. "
        f"Count words the STANDARD way — tokens separated by spaces, exactly what a word counter reports as «Слов». "
        f"Do NOT count «слов буквенных» / letter-fragments and do NOT count characters: those give a BIGGER number and "
        f"would trick you into stopping too early, leaving the script too short. "
        f"SELF-CHECK BEFORE YOU ANSWER — mandatory: when you think the part is finished, re-read your 'script' and count its "
        f"WORDS («Слов»). If it is under {target_words}, you are NOT finished — keep developing the SAME material (more depth, a "
        f"concrete scene, fact, name or turn that genuinely belongs here) until you reach at least {target_words} words. "
        f"Never pad with filler, repetition, or meta-comments. Output the JSON only AFTER the word count («Слов») is in range."
    )'''

C22_NEW = '''    # PATCH-2026-07-18-v6: длина через АБЗАЦЫ — дискретный счёт модель исполняет,
    # непрерывный счёт слов — нет (эмпирика: стабильно ~60% от просимого в словах).
    paras = max(4, round(target_words / 105))
    return (
        f"LENGTH — write EXACTLY {paras} paragraphs in 'script'. Each paragraph = 90-120 words "
        f"(4-6 spoken sentences); a paragraph under 60 words is a fragment — merge or grow it. "
        f"{paras} paragraphs x ~105 words lands on the ~{target_words}-word target automatically, so do NOT "
        f"try to count words — count PARAGRAPHS, that is the only length control. "
        f"SELF-CHECK BEFORE YOU ANSWER — mandatory: count the paragraphs in your 'script' (separated by blank "
        f"lines). Fewer than {paras}? The part is UNFINISHED — add full new paragraphs of real material (a scene, "
        f"a fact, a turn that belongs here) until you reach EXACTLY {paras}. Never pad with filler or repetition. "
        f"Output the JSON only when the paragraph count is exactly {paras}."
    )'''

# v6 (18.07): модель отдаёт ~60% просьбы (свойство LLM), а floor дожима считался от
# РАЗДУТОЙ фактором просьбы (0.8×ask = 97.6% чистой цели) → дожим был неизбежен всегда.
# Фикс: floor-коэффициент настраиваемый (env GEN_RECOVERY_FLOOR, дефолт 0.8 = старое
# поведение); с фактором 1.6 ставим 0.5 → floor = 0.8 × чистой цели.
C21_OLD = '''                if target_words_per_turn and target_words_per_turn > 0:
                    _lo = int(target_words_per_turn * 0.8)
                    _hi = int(target_words_per_turn * 1.15)'''

C21_NEW = '''                if target_words_per_turn and target_words_per_turn > 0:
                    _floor_k = float(os.environ.get("GEN_RECOVERY_FLOOR", "0.8"))
                    _lo = int(target_words_per_turn * _floor_k)
                    _hi = int(target_words_per_turn * 1.15)'''

C1_OLD = '''    merged_script = (pm.get("script") or "").rstrip() + "\\n\\n" + (pc.get("script") or "").lstrip()'''

C1_NEW = '''    # PATCH-2026-07-18-v5: идемпотентный merge — recovery-дожим не должен вклеивать пере-нарратив.
    _pm_s = (pm.get("script") or "").rstrip()
    _pc_s = (pc.get("script") or "").lstrip()

    def _norm_p(t):
        return " ".join(t.lower().split())

    _seen_p = {_norm_p(p) for p in _pm_s.split("\\n\\n") if len(p.split()) >= 15}
    _kept_p = [p for p in _pc_s.split("\\n\\n") if not (len(p.split()) >= 15 and _norm_p(p) in _seen_p)]
    _pc_s = "\\n\\n".join(_kept_p).strip()
    if not _pc_s:
        return main_raw
    merged_script = _pm_s + "\\n\\n" + _pc_s'''

# Прод (Jul 8) использует regex-границу поля (M4 FIX-2026-07-02), не for-loop.
C2_OLD = '''            _others = "|".join(re.escape(o) for o in _GEN_KEYS if o != name)
            if _others:
                _bound = re.compile(r'"\\s*,\\s*"(?:' + _others + r')"\\s*:')
                m = _bound.search(s, start)
                if m is not None:
                    end = m.start()  # позиция закрывающей кавычки значения
            seg = s[start:end].rstrip()'''

C2_NEW = '''            _others = "|".join(re.escape(o) for o in _GEN_KEYS if o != name)
            if _others:
                _bound = re.compile(r'"\\s*,\\s*"(?:' + _others + r')"\\s*:')
                m = _bound.search(s, start)
                if m is not None:
                    end = m.start()  # позиция закрывающей кавычки значения
            # PATCH-2026-07-18-v5: multi-emit clamp — при склеенных объектах {..}{..}
            # regex-граница выше НЕ ловит шов "}{"script" (нет запятой) → обрезаем явно.
            if name == "script":
                _seam = s.find('"}{"', start)
                if _seam != -1 and _seam + 1 < end:
                    end = _seam + 1
            seg = s[start:end].rstrip()'''

# v5.3 (18.07, контрольный джоб 47cc8c24): шов бывает с пробелами/переносами между
# объектами ("}\\n{"script") — точный find его пропускал, 6 швов ушло в сборку.
C17_OLD = '''            # PATCH-2026-07-18-v5: multi-emit clamp — при склеенных объектах {..}{..}
            # regex-граница выше НЕ ловит шов "}{"script" (нет запятой) → обрезаем явно.
            if name == "script":
                _seam = s.find('"}{"', start)
                if _seam != -1 and _seam + 1 < end:
                    end = _seam + 1
            seg = s[start:end].rstrip()'''

C17_NEW = '''            # PATCH-2026-07-18-v5.3: multi-emit clamp — при склеенных объектах {..}{..}
            # шов бывает и с пробелами/переносами между объектами — режем регексом.
            if name == "script":
                _seam_m = re.search(r'"\\s*\\}\\s*\\{\\s*"', s[start:end])
                if _seam_m is not None:
                    end = start + _seam_m.start() + 1
            seg = s[start:end].rstrip()'''

# v5.4 (18.07, контрольный 47cc8c24): шов и через закрытие МАССИВА — "]}{" и "]{" —
# судья нашёл 6 штук "]{"script": в готовом тексте. Обобщаем клэмп на ] и }.
C18_OLD = '''            # PATCH-2026-07-18-v5.3: multi-emit clamp — при склеенных объектах {..}{..}
            # шов бывает и с пробелами/переносами между объектами — режем регексом.
            if name == "script":
                _seam_m = re.search(r'"\\s*\\}\\s*\\{\\s*"', s[start:end])
                if _seam_m is not None:
                    end = start + _seam_m.start() + 1
            seg = s[start:end].rstrip()'''

C18_NEW = '''            # PATCH-2026-07-18-v5.4: multi-emit clamp — шов между объектами в любом виде:
            # "}{", "}\\n{", "]}{", "]{" (закрытие log_update-массива) — режем регексом.
            if name == "script":
                _seam_m = re.search(r'"\\s*[\\]\\}]+\\s*,?\\s*\\{\\s*"', s[start:end])
                if _seam_m is not None:
                    end = start + _seam_m.start() + 1
            seg = s[start:end].rstrip()'''

C3_OLD = '''                            f"extra scene or development that belongs here. Do NOT repeat anything already written, do NOT "
                            f"start a new section/chunk. {_close} "'''

C3_NEW = '''                            f"extra scene or development that belongs here. Do NOT repeat anything already written, do NOT "
                            f"start a new section/chunk. EVERY added paragraph must contain at least one element not yet "
                            f"in the text — a new fact, name, detail, scene development or period texture; re-telling or "
                            f"summarizing does NOT count toward the word target — if verified facts are exhausted, extend "
                            f"the CURRENT scene with concrete sensory period detail instead. {_close} "'''

C4_OLD = '''                        if _is_last_turn:
                            _close = ("If your previous part already ended with a sign-off, treat that as PREMATURE — keep "
                                      "developing the story, and only at the VERY END write the final short sign-off that "
                                      "names the host and the channel.")'''

C4_NEW = '''                        if _is_last_turn:
                            # PATCH-2026-07-18-v5: не заказывать ВТОРОЙ именной sign-off (порождал двойной финал).
                            _close = ("If your previous part already ended with a sign-off, do NOT write a new different "
                                      "ending: continue with STORY material only, and at the VERY END repeat that same "
                                      "short sign-off once, word-for-word. No recap, no second farewell wording.")'''

C5_OLD = '''    system = (
        "You are a senior YouTube producer and long-form story architect. You design the engagement "
        "FUNNEL (preview -> title -> hook -> a length-budgeted PLAN) BEFORE any script is written. "
        "Output ONLY a single valid JSON object — first character '{', last '}'. No prose, no markdown fences."
    )'''

C5_NEW = '''    # PATCH-2026-07-18-v5: воронка проектирует ретеншн-карту, не только бюджет длины.
    system = (
        "You are a senior YouTube producer and long-form story architect. You design the engagement "
        "FUNNEL (preview -> title -> hook -> a RETENTION MAP: which loop opens/closes where, where "
        "attention is re-hooked, where the title promise pays off) BEFORE any script is written. "
        "The map doubles as a length budget. "
        "Output ONLY a single valid JSON object — first character '{', last '}'. No prose, no markdown fences."
    )'''

C6_OLD = '''        "PLAN RULES — this is a LENGTH BUDGET in WORDS («Слов», NOT «слов буквенных» or characters), obey it:\\n"
        f"- 'plan' MUST be a JSON array of EXACTLY {parts_n} string(s), one per part.\\n"
        f"- Each part string breaks into 7-11 concrete BEATS. Write each beat as: 'concrete beat description (~N words)'. "
        f"The N values within one part MUST sum to AT LEAST {per} words (allocate generously — a too-thin plan "
        f"makes the final script too short). Give each beat enough concrete material (a real scene, fact, name, turn) to "
        f"genuinely fill its word budget.\\n"
        "- The parts together form ONE complete arc: part 1 opens with the hook then a short host intro; middle parts "
        "develop with rising tension, concrete facts, names, scenes; the LAST part delivers the climax/payoff and ends "
        "with a short outro/sign-off naming the host and the channel. Put exactly ONE like+subscribe nudge in a middle part.\\n"'''

C6_NEW = '''        "PLAN RULES — each part string is a RETENTION MAP and a LENGTH BUDGET in WORDS («Слов», NOT «слов буквенных» or characters), obey both:\\n"
        f"- 'plan' MUST be a JSON array of EXACTLY {parts_n} string(s), one per part.\\n"
        "- START each part string with ONE header line: 'LOOPS: open=Lx: promise in 5-8 words | keep-open=ids | "
        "close=Lx at ~P percent of the WHOLE video'. Loops are numbered L1-L5 across the WHOLE video. L1 = the TITLE "
        "promise: it OPENS inside the hook and CLOSES (pays off) ONLY inside the 70-92 percent window of the whole "
        "video — never earlier, never later. Plan 2-3 smaller loops that pay off stepwise before it (around 25-40 "
        "percent and 45-60 percent) so the viewer gets paid while waiting. If the topic/seed already numbers its own "
        "hooks and loops (PLAN/HOOKS section), MAP THOSE into L1-L5 — do not invent competing ones.\\n"
        f"- After the header, break the part into 7-11 BEATS. Write each beat as: 'concrete beat (~N words | NEW: the "
        f"one new fact, name, document, place, object or reversal this beat adds | loop op: OPEN Lx / CLOSE Lx / "
        f"none)'. The N values within one part MUST sum to AT LEAST {per} words. A beat whose NEW field would repeat "
        f"already-planned information is FORBIDDEN — plan fewer, longer scenes rather than repetition beats.\\n"
        "- ENTRY (part 1 only): hook (1-2 sentences) -> one-sentence title-card promise (what this video will show) "
        "-> the FIRST CONCRETE SCENE begins within the first 250 words: a person, in a place, at a moment — "
        "background history comes later, in slices.\\n"
        "- RE-HOOKS: every 600-900 words schedule ONE structural re-hook and name it inside a beat: CHAPTER TURN (a "
        "one-line mini-title promise), or IRONY-FORWARD (one line of fate-knowledge at a section join, e.g. 'it was "
        "his last victory'), or TRIVIA SPIKE (one startling verifiable aside). Rhetorical questions are NOT re-hooks "
        "and must not appear in the plan.\\n"
        "- SCALE SHIFTS: alternate beat scale — after 2-3 close-up scene beats, one wide-shot beat (map, army, era), "
        "then back to close-up.\\n"
        "- FINALE: exactly ONE finale. The last part closes L1 inside the 70-92 percent window; after the payoff only "
        "NEW aftermath material (fates, consequences), then a short sign-off. NOTHING after the payoff may recap, "
        "re-explain or return to earlier scenes.\\n"'''

C7_OLD = '''    user_parts.append("\\n" + "\\n".join(position_lines))
    user_parts.append(
        "\\nCRITICAL OUTPUT RULES:\\n"'''

C7_NEW = '''    user_parts.append("\\n" + "\\n".join(position_lines))
    # PATCH-2026-07-18-v5: правила интереса (6 подтверждённых причин разрыва с конкурентами).
    user_parts.append(
        "\\nINTEREST RULES — apply to EVERY paragraph; they outrank style habits:\\n"
        "1. NEW-INFO RULE: every paragraph must add at least one element the viewer does not yet have — a fact, a "
        "name, a place, a document, a physical object, a sensory period detail, or a reversal. NEVER re-state in new "
        "words what was already told; a paradox or list of clues may be voiced ONCE, never returned to. If verified "
        "facts for a stretch run out, advance with concrete period texture (what that room, street, machine, uniform "
        "of that era looked, sounded, smelled like) — texture moves the scene forward, rephrasing does not.\\n"
        "2. SHOW, THEN CUT: carry emotion with ONE physical detail per beat (an object, a measurement, a gesture), "
        "then CUT to the next fact. After a strong scene: ZERO commentary paragraphs. FORBIDDEN narrator moves: "
        "pausing to tell the viewer what to feel ('let us pause over this', 'think how much courage that took', "
        "'imagine this...'), and praising your own material ('no words are needed', 'this says more than any "
        "number'). Maximum 2 such constructions per WHOLE video — zero is better.\\n"
        "3. LOOP DISCIPLINE: when a loop opens — plant it once and go SILENT until its payoff. 'Remember this', "
        "'we will come back to this', 'more on that later' are FORBIDDEN (max 2 addressed forward-flags per WHOLE "
        "video). To point at the future use third-person fate-prolepsis at a name's first appearance instead: "
        "'he had eleven months left to live.'\\n"
        "4. RE-HOOKS ARE STRUCTURAL: renew attention with the planned chapter turns, irony-forward lines and trivia "
        "spikes — NOT with rhetorical questions. Questions addressed to the viewer are allowed ONLY within the first "
        "150 words of the video; NEVER ask a question and answer it in the same breath.\\n"
        "5. SENTENCE RHYTHM: default sentence 12-20 words with the turn INSIDE it ('he had won everything — but the "
        "order never came'); a paragraph should not end with the valence it began with. Sentences of 5 words or "
        "fewer: at most 15 percent of all sentences, reserved for genuine peaks. Aphorism-enders: at most one per 3 "
        "paragraphs. The central metaphor: at most once per 1000 words. TTS-friendly means CLEAR clauses, not "
        "staccato fragments.\\n"
        "6. UNCERTAINTY = INTRIGUE, NOT A SEMINAR: where sources disagree or facts are missing, spend ONE sentence "
        "('why — remains a mystery; the files were burned') or a two-line rumor-loop (version one / version two), "
        "then move on. Source-criticism blocks over 50 words are forbidden; a numbers dispute may be voiced ONCE per "
        "video, in one sentence. NEVER voice the preset's or brief's internal fact-checking instructions in the "
        "narration."
    )
    user_parts.append(
        "\\nCRITICAL OUTPUT RULES:\\n"'''

C8_OLD = '''    if plan_for_chunk:
        position_lines.append(
            "PLAN FOR THIS PART (designed in the funnel step) — follow it EXACTLY and write EACH beat to its "
            "~character budget so the total length lands on target without padding or rushing:\\n" + plan_for_chunk
        )
        if chunk_idx == 0 and hook:
            position_lines.append(
                "Open the script with THIS exact hook (it was designed for this video; adapt it naturally into "
                + language + ", keep it an open loop, then a short host intro): " + hook
            )'''

C8_NEW = '''    if plan_for_chunk:
        # PATCH-2026-07-18-v5: план = ретеншн-карта; вход в первую сцену ≤250 слов.
        position_lines.append(
            "PLAN FOR THIS PART (the retention map from the funnel step) — follow it EXACTLY: write EACH beat to its "
            "~word budget («Слов»), DELIVER each beat's NEW element, execute its loop ops (OPEN = plant once, then "
            "silence; CLOSE = full payoff at the planned position; loops marked keep-open MUST still be open when "
            "this part ends), and place the scheduled re-hooks as one-line structural turns:\\n" + plan_for_chunk
        )
        if chunk_idx == 0 and hook:
            position_lines.append(
                "Open the script with THIS exact hook (it was designed for this video; adapt it naturally into "
                + language + ", keep it an open loop): " + hook + " — then ONE sentence of title-card promise, then "
                "enter the FIRST concrete scene within the first 250 words (a person, in a place, at a moment). No "
                "host self-introduction longer than one sentence, no background lecture before a human being appears."
            )'''

C9_OLD = '''                "This is the FINAL chunk: FIRST fully develop the remaining material and SELF-CHECK you are inside the "
                "character range above; only AFTER you have verified the count, close with a short outro/sign-off that "
                "NAMES the host and the channel. Do NOT rush, summarize, or wind down early."'''

C9_NEW = '''                "This is the FINAL chunk: FIRST fully develop the remaining material and SELF-CHECK you are inside the "
                "character range above; only AFTER you have verified the count, close with a short outro/sign-off that "
                "NAMES the host and the channel. If no host or channel name is given in this prompt, do NOT invent "
                "one — close with a neutral sign-off without any brand, host name or narrator identity. "
                "Do NOT rush, summarize, or wind down early."'''

C10_OLD = '''                "In THIS chunk, naturally insert ONE short call-to-action (like + subscribe) mentioning the "
                "channel name — a single in-voice sentence, never jarring. (Only here, not in other chunks.)"'''

C10_NEW = '''                "In THIS chunk, naturally insert ONE short call-to-action (like + subscribe) mentioning the "
                "channel name — a single in-voice sentence, never jarring. (Only here, not in other chunks.) "
                "If no channel name is given in this prompt, SKIP the call-to-action entirely and do NOT invent "
                "a channel or host name."'''

# ── S9 (18.07): ретеншн-редактор в пайплайне (автоматизация ручной QA), гейт GEN_EDITOR=1 ──
S9_OLD = '''        except Exception as _ce:
            final_script = raw_script
            log_queue.put(f"⚠️ cleaner failed ({type(_ce).__name__}: {str(_ce)[:80]}) — текст без чистки")

        # === Save outputs ==='''

S9_NEW = '''        except Exception as _ce:
            final_script = raw_script
            log_queue.put(f"⚠️ cleaner failed ({type(_ce).__name__}: {str(_ce)[:80]}) — текст без чистки")

        # PATCH-2026-07-18: ретеншн-редактор (DELETE-only, автоматизация ручной QA). Гейт GEN_EDITOR=1.
        if os.environ.get("GEN_EDITOR", "0") == "1":
            try:
                from gen_editor import edit_for_retention
                _edited, _erep = edit_for_retention(
                    final_script, language, model, job.oauth_token or "", log_queue,
                )
                # re-clean после удалений (склейки абзацев)
                try:
                    from gen_output_cleaner import clean_generated_text as _cg2
                    _edited, _ = _cg2(_edited)
                except Exception:
                    pass
                final_script = _edited
                for _er in (_erep or ["редактор: без правок"]):
                    log_queue.put(f"✂️ {_er}")
            except Exception as _ee:
                log_queue.put(f"⚠️ editor failed ({type(_ee).__name__}: {str(_ee)[:80]}) — текст без ретеншн-правок")

        # === Save outputs ==='''

# ── v7 (18.07, идеи юзера): воронка = ход 0 чата, части сырым текстом. Гейт GEN_V7=1. ──
S7_OLD = '''        _multi_done = False
        if num_chunks > 1 and provider == "subscription":'''

S7_NEW = '''        _multi_done = False
        # PATCH-2026-07-18-v7 (гейт GEN_V7=1): воронка нулевым ходом ТОГО ЖЕ чата,
        # части — сырым текстом между маркерами (без JSON). Fail-open на старый путь.
        if (num_chunks > 1 and provider == "subscription"
                and os.environ.get("GEN_V7", "0") == "1"):
            try:
                from gen_v7 import run_v7
                _ok7, _chunks7, _pv7, _tt7, _err7 = run_v7(
                    preset_content=preset.content, topic=topic, language=language,
                    target_words=target_words, num_chunks=num_chunks,
                    words_per_chunk=words_per_chunk or 0, model=model,
                    oauth_token=job.oauth_token or "", log_queue=log_queue,
                    host_name=host_name_hint, channel_name=channel_name_hint,
                )
                if _ok7 and _chunks7:
                    chunks_output.extend(_chunks7)
                    if _pv7:
                        preview = _pv7
                    if _tt7:
                        title = _tt7
                    _multi_done = True
                else:
                    log_queue.put(f"⚠️ v7: {str(_err7)[:90]} — падаю на обычный conversation-путь")
            except Exception as _v7e:
                log_queue.put(f"⚠️ v7 упал ({type(_v7e).__name__}: {str(_v7e)[:80]}) — обычный путь")
        if not _multi_done and num_chunks > 1 and provider == "subscription":'''

# При GEN_V7=1 отдельная воронка не нужна (она уходит в чат) — на conversation-пути.
S8_OLD = '''        if target_words:  # воронка осмысленна только когда задана цель длины'''

S8_NEW = '''        _v7_owns_funnel = (os.environ.get("GEN_V7", "0") == "1"
                           and provider == "subscription" and num_chunks > 1)
        if target_words and not _v7_owns_funnel:  # воронка осмысленна только когда задана цель длины (v7 несёт её в чате)'''

# ── v5.1 (18.07, по итогам калибровки 88f72552): корневые фиксы ──
# C11: хук обязан САМ открывать петлю L1 + вторичную петлю (в калибровке воронка
#      дала чистый хук без петли — зритель ехал полтекста без крючка).
C11_OLD = '''        "- ENTRY (part 1 only): hook (1-2 sentences) -> one-sentence title-card promise (what this video will show) "
        "-> the FIRST CONCRETE SCENE begins within the first 250 words: a person, in a place, at a moment — "
        "background history comes later, in slices.\\n"'''

C11_NEW = '''        "- ENTRY (part 1 only): hook (1-2 sentences) -> one-sentence title-card promise (what this video will show) "
        "-> the FIRST CONCRETE SCENE begins within the first 250 words: a person, in a place, at a moment — "
        "background history comes later, in slices. The hook text itself MUST open loop L1 AND plant one secondary "
        "loop as unresolved breaks (a fact cut mid-consequence), not as statements — if the hook resolves everything "
        "it announces, rewrite it.\\n"'''

# C12: recovery последнего turn'а — «финал уже написан» распознавать и для каналов
#      БЕЗ sign-off (финал-пуанта): в калибровке дожим +289 слов лёг ПОСЛЕ пуанты.
C12_OLD = '''                            _close = ("If your previous part already ended with a sign-off, do NOT write a new different "
                                      "ending: continue with STORY material only, and at the VERY END repeat that same "
                                      "short sign-off once, word-for-word. No recap, no second farewell wording.")'''

C12_NEW = '''                            _close = ("If your previous part already ended with its ENDING — a sign-off, or the final "
                                      "punchline/closing image on channels that end without sign-offs — do NOT write a "
                                      "new different ending: continue with STORY material only, and at the VERY END "
                                      "repeat that same short ending once, word-for-word. NOTHING may come after it: "
                                      "no recap, no 'one more thing', no second farewell wording.")'''

# ── v5.2 (18.07, по итогам калибровки №2 97ad2fb2): хвост recovery МЕХАНИЧЕСКИ
#    не может встать после финала — merge вставляет добор ПЕРЕД последним абзацем.
C13_OLD = '''def _gen_merge_continuation(main_raw: str, cont_raw: str) -> str:
    """PATCH-2026-06-27-length: мерджит script из cont_raw в конец script main_raw,
    сохраняя preview/title/hook/plan/log_update. При неудаче — main_raw без изменений."""'''

C13_NEW = '''def _gen_merge_continuation(main_raw: str, cont_raw: str, insert_before_last_para: bool = False) -> str:
    """PATCH-2026-06-27-length: мерджит script из cont_raw в script main_raw,
    сохраняя preview/title/hook/plan/log_update. При неудаче — main_raw без изменений.
    PATCH-2026-07-18-v5.2: insert_before_last_para=True (recovery ПОСЛЕДНЕГО turn'а) —
    добор вставляется ПЕРЕД финальным абзацем main, финал остаётся последним."""'''

C14_OLD = '''    _pc_s = "\\n\\n".join(_kept_p).strip()
    if not _pc_s:
        return main_raw
    merged_script = _pm_s + "\\n\\n" + _pc_s'''

C14_NEW = '''    _pc_s = "\\n\\n".join(_kept_p).strip()
    if not _pc_s:
        return main_raw
    if insert_before_last_para:
        _mp = _pm_s.split("\\n\\n")
        if len(_mp) >= 2:
            merged_script = "\\n\\n".join(_mp[:-1]) + "\\n\\n" + _pc_s + "\\n\\n" + _mp[-1]
        else:
            merged_script = _pm_s + "\\n\\n" + _pc_s
    else:
        merged_script = _pm_s + "\\n\\n" + _pc_s'''

# v5.5 (18.07, судьи Dosarele/Tajemství): «перед последним абзацем» мелко — финал
# это блок (кода+залп+образ), добор клинился между ними на 94-99%. Правильно:
# добор ложится в ТЕЛО — перед абзацем, пересекающим 85% слов.
# ЦЕЛЕВОЕ состояние — ПОЛНЫЙ блок (с префиксом _pc_s), варианты — обе прошлые версии.
C19_FULL = '''    _pc_s = "\\n\\n".join(_kept_p).strip()
    if not _pc_s:
        return main_raw
    if insert_before_last_para:
        # PATCH-2026-07-18-v5.5: добор — в тело, перед абзацем на ~85% слов;
        # финальный блок (последние ~15%: пэйофф-хвост, залп, кода, образ) не разрывается.
        _mp = _pm_s.split("\\n\\n")
        _tot = sum(len(p.split()) for p in _mp) or 1
        _acc, _cut = 0, max(1, len(_mp) - 1)
        for _pi, _pp in enumerate(_mp):
            _acc += len(_pp.split())
            if _acc >= _tot * 0.85:
                _cut = max(1, _pi)
                break
        merged_script = "\\n\\n".join(_mp[:_cut]) + "\\n\\n" + _pc_s + "\\n\\n" + "\\n\\n".join(_mp[_cut:])
    else:
        merged_script = _pm_s + "\\n\\n" + _pc_s'''

C15_OLD = '''                            raw = _gen_merge_continuation(raw, _cont)'''

C15_NEW = '''                            raw = _gen_merge_continuation(raw, _cont, insert_before_last_para=_is_last_turn)'''

# Финальное состояние last-turn _close (заменяет любую из прошлых версий цепочки C4→C12):
CLOSE_FINAL = '''                            _close = ("Your continuation will be INSERTED BEFORE the existing ending automatically. "
                                      "Therefore write PURE middle-story material only: new scenes, aftermath facts, "
                                      "concrete period texture. Do NOT write any ending, sign-off, closing image, "
                                      "recap, punchline or farewell — the script already has its ending and it will "
                                      "remain the final words.")'''

# v5.6 (18.07): сервис мультинишевый — INTEREST RULES не должны перебивать пресет
# (sleep/сказки/листиклы имеют свой формат). Пресет всегда главнее.
C20_OLD = '''        "\\nINTEREST RULES — apply to EVERY paragraph; they outrank style habits:\\n"'''

C20_NEW = '''        "\\nINTEREST RULES — apply to EVERY paragraph UNLESS the CHANNEL PRESET explicitly "
        "requires a different format, voice or genre — on any conflict the preset wins:\\n"'''

# ── v5.7 (18.07): анти-пере-нарратив — continue-turn получает дайджест «уже
#    рассказано» из NEW-полей карты предыдущих частей (дубль-арки актов II).
S3_OLD = '''                _plan_k = _plan_for(_k)
                if _plan_k:'''

S3_NEW = '''                _plan_k = _plan_for(_k)
                # PATCH-2026-07-18-v5.7: дайджест покрытого — модель не пересказывает прошлые части.
                if funnel_plan:
                    _covered = []
                    for _pp in funnel_plan[:_k]:
                        _covered += [c.strip() for c in re.findall(r"NEW:\\s*([^|)]+)", str(_pp))]
                    if _covered:
                        _parts.append(
                            "ALREADY COVERED in previous parts — do NOT retell, re-explain or summarize any "
                            "of these (one passing clause maximum if continuity demands): "
                            + "; ".join(_covered[:60])
                        )
                if _plan_k:'''

# ── v5.7: MUST-сверка — перед закрытием проверить, что формулы/цитаты брифа произнесены.
S5_OLD = '''                        "FORBIDDEN after the payoff: recap, re-explaining the meaning, a second ending, returning "
                        "to earlier scenes, restating the paradox."
                    )'''

S5_NEW = '''                        "FORBIDDEN after the payoff: recap, re-explaining the meaning, a second ending, returning "
                        "to earlier scenes, restating the paradox. "
                        "SELF-CHECK before closing: every mandatory dispute-formula, verbatim quote and "
                        "anchor fact from the brief must have been voiced exactly ONCE — if one is missing, "
                        "weave it into the aftermath BEFORE the outro."
                    )'''

# v5.8 (18.07, идея юзера): сценарист видит ВСЮ карту с первого хода — разговор
# её помнит, межчастевые петли перестают быть слепыми.
S6_OLD = '''            _turn_prompts = [_msgs0[-1].get("content", "")]'''

S6_NEW = '''            _turn0 = _msgs0[-1].get("content", "")
            # PATCH-2026-07-18-v5.8: полная ретеншн-карта в первый ход — чат помнит замысел целиком.
            if funnel_plan and len(funnel_plan) > 1:
                _full_map = "\\n\\n".join(
                    f"— PART {i + 1} of {len(funnel_plan)} —\\n{p}" for i, p in enumerate(funnel_plan)
                )
                _turn0 += (
                    "\\n\\nFULL RETENTION MAP of the WHOLE video (context only — RIGHT NOW you are writing "
                    "PART 1 and nothing else; later parts will be requested in later turns; loops you OPEN "
                    "now may close in later parts exactly as mapped):\\n" + _full_map
                )
            _turn_prompts = [_turn0]'''

PATCHES = {
    "server": [
        ("S1 план части = ретеншн-карта", S1_OLD, S1_NEW),
        ("S2 reminder + FINALE + гейт имени/CTA", S2_OLD, S2_NEW),
        ("S3 v5.7 анти-пере-нарратив (дайджест покрытого)", S3_OLD, S3_NEW),
        ("S5 v5.7 MUST-сверка формул перед финалом", S5_OLD, S5_NEW),
        ("S6 v5.8 полная карта в первый ход", S6_OLD, S6_NEW),
        ("S7 v7 ветка: воронка-в-чате + raw-текст (гейт)", S7_OLD, S7_NEW),
        ("S8 v7 гейт отдельной воронки", S8_OLD, S8_NEW),
        ("S9 ретеншн-редактор в пайплайне (гейт GEN_EDITOR)", S9_OLD, S9_NEW),
    ],
    "core": [
        ("C21 v6 floor дожима от чистой цели", C21_OLD, C21_NEW),
        ("C22 v6 длина абзацами, не словами", C22_OLD, C22_NEW),
        ("C1 идемпотентный merge", C1_OLD, C1_NEW),
        ("C3 recovery: только новое", C3_OLD, C3_NEW),
        # C4: цепочка версий (v5 → v5.1 → v5.2) — якорь-варианты, целевое состояние одно
        ("C4 recovery: добор без финала (цепочка)", [C4_OLD, C4_NEW, C12_NEW], CLOSE_FINAL),
        # C2: цепочка v5 → v5.3 → v5.4 над одним участком (salvage-клэмп)
        ("C2 salvage-клэмп шва (цепочка)", [C2_OLD, C2_NEW, C17_NEW], C18_NEW),
        ("C5 воронка system", C5_OLD, C5_NEW),
        ("C6 PLAN RULES = ретеншн-карта", C6_OLD, C6_NEW),
        ("C7 INTEREST RULES в chunk-промпт", C7_OLD, C7_NEW),
        ("C8 план части + вход ≤250 слов", C8_OLD, C8_NEW),
        ("C9 one-shot финал: гейт имени", C9_OLD, C9_NEW),
        ("C10 one-shot CTA: гейт имени", C10_OLD, C10_NEW),
        ("C11 v5.1 хук открывает петли", C11_OLD, C11_NEW),
        ("C20 v5.6 пресет главнее INTEREST RULES", C20_OLD, C20_NEW),
        ("C13 v5.2 merge: сигнатура insert_before_last_para", C13_OLD, C13_NEW),
        ("C14/C19 merge: добор в тело до 85% (цепочка)", [C14_OLD, C14_NEW], C19_FULL),
        ("C15 v5.2 вызов merge с флагом last-turn", C15_OLD, C15_NEW),
    ],
}


def patch_file(path: str, patches, dry: bool) -> bool:
    with open(path, encoding="utf-8") as f:
        src = f.read()
    out = src
    ok = True
    for pid, old, new in patches:
        variants = old if isinstance(old, list) else [old]
        hit = next((v for v in variants if v in out), None)
        if hit is not None:
            cnt = out.count(hit)
            if cnt != 1:
                print(f"  ✗ {pid}: якорь встречается {cnt} раз (нужен ровно 1) — НЕ применяю")
                ok = False
                continue
            out = out.replace(hit, new)
            print(f"  ✓ {pid}")
        else:
            # «уже применён» vs «якорь не найден» — по УНИКАЛЬНОМУ маркеру нового кода
            # (самая длинная строка new, которой НЕТ ни в одном варианте old).
            _old_lines = set()
            for v in variants:
                _old_lines |= {l.strip() for l in v.splitlines()}
            _marker = max(
                (l.strip() for l in new.splitlines() if l.strip() and l.strip() not in _old_lines),
                key=len, default="",
            )
            if _marker and _marker in out:
                print(f"  = {pid}: уже применён — пропуск")
            else:
                print(f"  ✗ {pid}: якорь НЕ НАЙДЕН — прод дрейфанул? Нужен ручной разбор")
                ok = False
    if not ok:
        print(f"  ⛔ {path}: есть непримененные патчи — файл НЕ записан")
        return False
    if out == src:
        print(f"  {path}: изменений нет (всё уже применено)")
        return True
    if dry:
        print(f"  [dry-run] {path}: все якоря найдены, запись пропущена")
        return True
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{path}.bak-v5-{ts}"
    shutil.copy2(path, bak)
    tmp = path + ".v5tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(out)
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as e:
        print(f"  ⛔ {path}: py_compile упал на патченом файле — откат. {e}")
        import os
        os.remove(tmp)
        return False
    import os
    os.replace(tmp, path)
    print(f"  💾 {path} записан (бэкап: {bak})")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-py", required=True)
    ap.add_argument("--core", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(f"== server.py: {a.server_py}")
    ok1 = patch_file(a.server_py, PATCHES["server"], a.dry_run)
    print(f"== rewrite_core.py: {a.core}")
    ok2 = patch_file(a.core, PATCHES["core"], a.dry_run)
    sys.exit(0 if (ok1 and ok2) else 2)


if __name__ == "__main__":
    main()
