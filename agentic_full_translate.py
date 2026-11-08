import os, json, urllib.request, time, re, difflib, opencc

s2tw = opencc.OpenCC('s2twp')
video_title = os.environ.get('VIDEO_TITLE', 'Gyuri Ch. 감규리 - 매끈매끈하다 매끈매끈한')
src_langs = [x.strip() for x in os.environ.get('SRC_LANGS', 'ko').split(',') if x.strip()]
tgt_langs = [x.strip() for x in os.environ.get('TGT_LANGS', 'zh-TW').split(',') if x.strip()] or ['zh-TW']
LANG_NAMES = {
    'zh-TW': '繁體中文（台灣口吻）',
    'en': 'English',
    'ja': '日本語（自然な話し言葉・ネットスラング対応）',
    'ko': '한국어',
    'pt': 'Português',
    'de': 'Deutsch',
    'orig': '原文保留',
}
llm_tgt_langs = [l for l in tgt_langs if l != 'orig'] or ['zh-TW']
tgt_desc = ' + '.join([LANG_NAMES.get(l, l) for l in llm_tgt_langs])
use_s2tw = ('zh-TW' in llm_tgt_langs)

# 1. 載入 OCR 畫面字卡與語音對白
ocr_cards = []
if os.path.exists('ocr_detections.json'):
    try:
        with open('ocr_detections.json', 'r', encoding='utf-8') as f:
            ocr_cards = json.load(f)
    except Exception:
        ocr_cards = []

raw_srt = ""
if os.path.exists('clean_orig.srt'):
    with open('clean_orig.srt', 'r', encoding='utf-8') as f:
        raw_srt = f.read().strip()

def parse_time_sec(ts_str):
    h, m, s_ms = ts_str.strip().split(':')
    s, ms = s_ms.split(',')
    return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000

# 解析 clean_orig.srt 成結構化列表
blocks = raw_srt.split('\n\n') if raw_srt else []
dialogues = []
for b in blocks:
    lines = [l.strip() for l in b.split('\n') if l.strip()]
    if len(lines) >= 3:
        m = re.match(r'(\d+:\d+:\d+,\d+)\s*-->\s*(\d+:\d+:\d+,\d+)', lines[1])
        if m:
            t_parts = lines[1].split(' --> ')
            dialogues.append({
                "id": lines[0],
                "time": lines[1],
                "text": ' '.join(lines[2:]).strip(),
                "start_sec": parse_time_sec(t_parts[0]),
                "end_sec": parse_time_sec(t_parts[1]),
            })

# 智能空間與時序對齊：將畫面字卡關聯至最接近的對白區塊（排除與底層台詞重複之燒錄字）
dialogue_cards = {d['id']: [] for d in dialogues}
matched_cards_set = set()

for c_idx, c in enumerate(ocr_cards):
    c_sec = float(c.get('sec', 0.0))
    c_text = c.get('text', '').strip()
    if not c_text:
        continue
    best_d = None
    best_dist = 999.0
    for d in dialogues:
        dist = min(abs(c_sec - d['start_sec']), abs(c_sec - d['end_sec']))
        if d['start_sec'] - 1.5 <= c_sec <= d['end_sec'] + 1.5:
            dist = 0.0
        if dist < best_dist:
            best_dist = dist
            best_d = d
    if best_d and best_dist <= 4.0:
        dlg_clean = best_d['text'].replace(' ', '')
        card_clean = c_text.replace(' ', '')
        sim = difflib.SequenceMatcher(None, dlg_clean, card_clean).ratio()
        if sim < 0.45:
            is_dup = any(difflib.SequenceMatcher(None, card_clean, existing.replace(' ', '')).ratio() > 0.65 for existing in dialogue_cards[best_d['id']])
            if not is_dup:
                dialogue_cards[best_d['id']].append(c_text)
                matched_cards_set.add(c_idx)

# 處理無對白區間的獨立字卡，關聯至最近對白確保不遺漏
for c_idx, c in enumerate(ocr_cards):
    if c_idx not in matched_cards_set:
        c_sec = float(c.get('sec', 0.0))
        c_text = c.get('text', '').strip()
        if not c_text:
            continue
        best_d = min(dialogues, key=lambda d: abs(c_sec - d['start_sec']), default=None)
        if best_d and abs(c_sec - best_d['start_sec']) <= 8.0:
            dlg_clean = best_d['text'].replace(' ', '')
            card_clean = c_text.replace(' ', '')
            if difflib.SequenceMatcher(None, dlg_clean, card_clean).ratio() < 0.45:
                is_dup = any(difflib.SequenceMatcher(None, card_clean, existing.replace(' ', '')).ratio() > 0.65 for existing in dialogue_cards[best_d['id']])
                if not is_dup:
                    dialogue_cards[best_d['id']].append(c_text)

# 記錄每個條目原本擁有的字卡原文
input_cards_by_id = {cid: list(cards) for cid, cards in dialogue_cards.items() if cards}

# 組裝包含字卡之時序完整大綱
full_script = []
for d in dialogues:
    full_script.append(f"[{d['time']}] (語音) {d['text']}")

full_script_text = "\n".join(full_script)
ocr_summary = "\n".join([f"[{c['sec']}s] 【畫面字卡】 {c['text']}" for c in ocr_cards])

total_attached_cards = sum(len(v) for v in dialogue_cards.values())
print(f"Loaded {len(dialogues)} dialogue lines and {len(ocr_cards)} OCR detections ({total_attached_cards} text cards attached to cues).")

# STAGE 1: 全局通讀與情境知識庫提取
stage1_prompt = f"""你是一位精通多國影視與 VTuber 文化的頂級字幕組組長。
以下是一部影片的「完整語音對白」與「畫面擷取到的字卡」：

【影片標題/檔名】
{video_title}

【畫面擷取到的關鍵字卡】：
{ocr_summary}

【全片語音對白節錄】：
{full_script_text[:3500]}

請通讀全篇內容，完成「全域情境分析與【{tgt_desc}】專有名詞對照表」（控制在 600–800 字內）：
1. 本片核心情境與脈絡（主題、笑點或重點是什麼？）
2. 重要術語、人名與網路梗對應至【{tgt_desc}】的標準譯法對照表（嚴禁輸出繁中/簡中譯名！必須直接給出【{tgt_desc}】在地化譯法，如日文請用片假名/日文漢字/VTuber用語）。
   特別注意：畫面擷取的字卡可能包含 OCR 辨識殘缺、斷字或雜訊（如綜藝特效字帶來的錯字），請依據人物與語音上下文將其校正還原為正確名稱或句子，並在對照表中列出標準【{tgt_desc}】譯法。
3. 針對【{tgt_desc}】的翻譯語氣指導（口吻、標點習慣）。
只輸出分析與名詞表，不要輸出翻譯。
"""

print("Stage 1: Extracting global context and glossary...")
payload1 = {
    "model": "qwen3.8-27b-fastmtp",
    "messages": [
        {"role": "user", "content": stage1_prompt},
        {"role": "assistant", "content": "</think>\n"}
    ],
    "temperature": 0.2,
    "max_tokens": 3500
}

req1 = urllib.request.Request("http://localhost:8088/v1/chat/completions",
    data=json.dumps(payload1).encode(), headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req1, timeout=120) as r:
    context_knowledge = json.loads(r.read())["choices"][0]["message"]["content"]
    context_knowledge = re.sub(r"<think>.*?</think>", "", context_knowledge, flags=re.DOTALL)
    context_knowledge = re.sub(r"^</think>\s*", "", context_knowledge).strip()
    if use_s2tw:
        context_knowledge = s2tw.convert(context_knowledge)

print("\n=== Stage 1 Output (Global Context) ===")
print(context_knowledge[:800] + "...\n")

with open("global_context.txt", "w", encoding="utf-8") as f:
    f.write(context_knowledge)

# STAGE 2: 注入全局背景，逐段依上下文自然意譯
print("Stage 2: Context-aware translation based on global knowledge...")

no_zh_warning = "" if "zh-TW" in llm_tgt_langs else f"（注意：目標語言為 {tgt_desc}，絕對禁止輸出中文！）"
multi_lang_rule = (
    f"1. 目標字幕語言為：{tgt_desc}。每個字幕區塊請依序輸出一行對應語言的翻譯（共 {len(llm_tgt_langs)} 行對白）{no_zh_warning}。"
    if len(llm_tgt_langs) > 1 else
    f"1. 請將所有字幕完整翻譯為【{tgt_desc}】{no_zh_warning}。"
)

stage2_system = f"""你是一位頂級影視與 Vtuber 多語翻譯專家。你剛剛已經通讀了全篇影片並建立了以下背景知識庫：
{context_knowledge}

【任務規則】
{multi_lang_rule}
2. 對於原片中的語音語意模糊或辨識瑕疵處，請依照全篇上下文自動校正意譯為【{tgt_desc}】。
3. 【字卡翻譯絕對鐵則（必讀）】：
   - 當字幕條目包含【字卡】時（例如「【字卡】자세 교정 이야기 중」），你【必須】依上下文修復 OCR 錯字，並將該字卡 100% 翻譯為【{tgt_desc}】！
   - 【格式嚴格要求】：字卡行開頭必須嚴格保留「【字卡】」這三個中文字前綴標記，後面緊接翻譯譯文，格式為「【字卡】<目標語言譯文>」。
   - 【絕對禁止原文】：嚴禁留下未翻譯的原文字卡（如韓文 Hangul、日文假名）！每一條【字卡】都必須是譯文！
   - 【禁止重複】：影片畫面中已存在原始字卡，因此【字卡】永遠只輸出唯一一行【{tgt_desc}】譯文！即使包含原文保留要求，【字卡】也絕對不可輸出原文或雙重行！
4. 輸入每個區塊格式為「序號 / 原文 /（可選）【字卡】」，沒有時間軸。輸出請保持相同格式（第一行序號，其後為譯文行），區塊之間以空行分隔，不得輸出時間軸，不得跳過或刪減序號。
5. 只輸出翻譯後的區塊內容，不要包含 Markdown 代碼標籤或額外解說。{no_zh_warning}

【字卡輸出範例】
輸入：
1
내가 고쳐줄게.
【字卡】자세 교정 이야기 중

輸出（若目標為繁中）：
1
我會幫妳修好。
【字卡】姿勢矯正話題進行中

輸出（若目標為日文）：
1
直してあげるよ。
【字卡】姿勢矯正の話の最中
"""

# 組裝包含字卡註記的翻譯區塊
merged_cues = []
for d in dialogues:
    card_suffix = "".join([f"\n【字卡】{c}" for c in dialogue_cards.get(d['id'], [])])
    merged_cues.append(f"{d['id']}\n{d['text']}{card_suffix}")

def translate_chunk(chunk_text):
    p = {
        "model": "qwen3.8-27b-fastmtp",
        "messages": [
            {"role": "system", "content": stage2_system},
            {"role": "user", "content": f"請將以下字幕翻譯為【{tgt_desc}】{no_zh_warning}。\n【重要指令】：凡有【字卡】之條目，必須保留【字卡】前綴並將其內容 100% 翻譯為【{tgt_desc}】，絕對不可遺漏，不可保留未翻譯的韓文/日文原文！\n\n{chunk_text}"},
            {"role": "assistant", "content": "</think>\n"}
        ],
        "temperature": 0.2, "max_tokens": 4096
    }
    req = urllib.request.Request("http://localhost:8088/v1/chat/completions",
        data=json.dumps(p).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        res = json.loads(resp.read().decode())["choices"][0]["message"]["content"]
        res = re.sub(r"<think>.*?</think>", "", res, flags=re.DOTALL)
        res = re.sub(r"^</think>\s*", "", res).strip()
        lines = [l for l in res.split("\n") if not l.strip().startswith("```")]
        return "\n".join(lines).strip()

translated_chunks = []
for i in range(0, len(merged_cues), 70):
    print(f"Translating chunk {i//70 + 1} with full context...")
    translated_chunks.append(translate_chunk("\n\n".join(merged_cues[i:i+70])))

raw_joined = "\n\n".join(translated_chunks).strip()
raw_translated = s2tw.convert(raw_joined) if use_s2tw else raw_joined

orig_time_by_id = {d['id'].strip(): d['time'].strip() for d in dialogues}
orig_text_by_id = {d['id'].strip(): d['text'].strip() for d in dialogues}
fixed_blocks = []
seen_ids = set()

for b in raw_translated.split("\n\n"):
    lines = [l.strip() for l in b.split("\n") if l.strip()]
    lines = [l for l in lines if "-->" not in l]
    if len(lines) < 2:
        continue
    cid = lines[0]
    if cid not in orig_time_by_id or cid in seen_ids:
        continue
    seen_ids.add(cid)

    dlg_trans = [l for l in lines[1:] if not l.startswith("【字卡】")]
    card_trans = [l for l in lines[1:] if l.startswith("【字卡】")]

    # 若模型遺漏字卡行，自動從 input_cards_by_id 取回待修復
    if cid in input_cards_by_id and not card_trans:
        for orig_c in input_cards_by_id[cid]:
            card_trans.append(f"【字卡】{orig_c}")

    # 移除字卡內的重複行（例如模型同時輸出譯文與原文）
    dedup_cards = []
    for c_line in card_trans:
        c_clean = c_line.replace("【字卡】", "").strip()
        if not c_clean:
            continue
        if not any(difflib.SequenceMatcher(None, c_clean, existing.replace("【字卡】", "").strip()).ratio() > 0.85 for existing in dedup_cards):
            dedup_cards.append(c_line)
    card_trans = dedup_cards

    # 雙語原文處理：'orig' 僅加於語音對白末尾，絕對不可加於字卡
    if 'orig' in tgt_langs and cid in orig_text_by_id:
        dlg_trans.append(orig_text_by_id[cid])

    fixed_blocks.append({
        "id": cid,
        "time": orig_time_by_id[cid],
        "dlg_lines": dlg_trans,
        "card_lines": card_trans,
    })

missing = len(orig_time_by_id) - len(seen_ids)
print(f"Translated cues: {len(seen_ids)}/{len(orig_time_by_id)} (missing {missing})")

# DETERMINISTIC POST-CHECK: 檢測並修補任何殘留原文或未翻譯之字卡
def needs_card_translation(card_text):
    txt = card_text.replace("【字卡】", "").strip()
    if not txt:
        return False
    # 若來源為韓文且目標不含韓文，字卡不可殘留諺文字元
    if any(s.startswith("ko") for s in src_langs) and "ko" not in llm_tgt_langs:
        if re.search(r"[\uac00-\ud7a3]", txt):
            return True
    # 若來源為日文且目標不含日文，字卡不可殘留平假名/片假名
    if any(s.startswith("ja") for s in src_langs) and "ja" not in llm_tgt_langs:
        if re.search(r"[\u3040-\u30ff]", txt):
            return True
    return False

untranslated_cards = []
for blk in fixed_blocks:
    for c_idx, c_line in enumerate(blk["card_lines"]):
        if needs_card_translation(c_line):
            untranslated_cards.append((blk["id"], c_idx, c_line))

if untranslated_cards:
    print(f"Detected {len(untranslated_cards)} untranslated card lines. Running targeted Qwen repair pass...")
    repair_lines = "\n".join([f"{cid}: {c.replace('【字卡】', '').strip()}" for cid, _, c in untranslated_cards])
    repair_prompt = f"""你是一位頂級影視在地化專家。請將以下影片畫面中的字卡逐行翻譯為【{tgt_desc}】：
注意：原字卡可能包含綜藝特效字帶來的 OCR 殘缺或錯字，請依語意校正並翻譯為自然流暢的【{tgt_desc}】。
格式要求：每行輸出必須為「序號: 【字卡】<譯文>」（「【字卡】」三個字請逐字保留作為前綴，不可替換為原文，嚴禁輸出韓文/日文原文！）：

範例：
輸入：1: 자세 교정 이야기 중
輸出：1: 【字卡】姿勢矯正話題進行中

{repair_lines}"""

    p_repair = {
        "model": "qwen3.8-27b-fastmtp",
        "messages": [
            {"role": "user", "content": repair_prompt},
            {"role": "assistant", "content": "</think>\n"}
        ],
        "temperature": 0.1, "max_tokens": 1500
    }
    try:
        req_rep = urllib.request.Request("http://localhost:8088/v1/chat/completions",
            data=json.dumps(p_repair).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req_rep, timeout=60) as resp:
            rep_res = json.loads(resp.read().decode())["choices"][0]["message"]["content"]
            rep_res = re.sub(r"<think>.*?</think>", "", rep_res, flags=re.DOTALL)
            rep_res = re.sub(r"^</think>\s*", "", rep_res).strip()
            
            # 解析序號並替換
            card_map = {}
            for line in rep_res.split("\n"):
                m = re.match(r'^(\d+)\s*[:：]\s*(【字卡】.*)', line.strip())
                if m:
                    card_map[m.group(1)] = m.group(2).strip()
                else:
                    m2 = re.match(r'^(\d+)\s*[:：]\s*(.*)', line.strip())
                    if m2:
                        cleaned = m2.group(2).strip()
                        cleaned = re.sub(r'^(【字卡】)+', '', cleaned).strip()
                        card_map[m2.group(1)] = f"【字卡】{cleaned}"

            for blk in fixed_blocks:
                cid = blk["id"]
                if cid in card_map:
                    new_c = card_map[cid]
                    if use_s2tw:
                        new_c = s2tw.convert(new_c)
                    blk["card_lines"] = [new_c]
            print(f"Successfully repaired {len(card_map)} untranslated cards with 100% target language!")
    except Exception as e:
        print(f"Warning: Card repair pass encountered error: {e}")

# 最終組裝 SRT：保證每個字卡行格式正規
final_blocks = []
for blk in fixed_blocks:
    norm_cards = []
    for c in blk["card_lines"]:
        cleaned = c.strip()
        if not cleaned.startswith("【字卡】"):
            cleaned = f"【字卡】{cleaned}"
        cleaned = re.sub(r'^(【字卡】)+', '【字卡】', cleaned)
        if use_s2tw:
            cleaned = s2tw.convert(cleaned)
        norm_cards.append(cleaned)
    lines = [blk["id"], blk["time"]] + blk["dlg_lines"] + norm_cards
    final_blocks.append("\n".join(lines))

final_full_srt = "\n\n".join(final_blocks) + "\n"
with open("context_translated_zh.srt", "w", encoding="utf-8") as f:
    f.write(final_full_srt)

print(f"Saved full context-aware translation to context_translated_zh.srt (total cues: {len(final_blocks)})!")
