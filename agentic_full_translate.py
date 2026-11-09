from __future__ import annotations
import os, json, urllib.request, urllib.error, time, re, difflib
from pathlib import Path

try:
    import opencc
    s2tw = opencc.OpenCC('s2twp')
except ImportError:
    class _FallbackOpenCC:
        def convert(self, text: str) -> str:
            return text
    s2tw = _FallbackOpenCC()

video_title = os.environ.get('VIDEO_TITLE', 'Gyuri Ch. 감규리 - 매끈매끈하다 매끈매끈한')
src_langs = [x.strip() for x in os.environ.get('SRC_LANGS', 'ko').split(',') if x.strip()]
tgt_langs = [x.strip() for x in os.environ.get('TGT_LANGS', 'zh-TW').split(',') if x.strip()] or ['zh-TW']
llm_model_id = os.environ.get('LLM_MODEL', 'qwen3.8-27b-fastmtp').strip() or 'qwen3.8-27b-fastmtp'


def _load_hermes_env_key(key_name: str) -> str:
    val = os.environ.get(key_name, "").strip()
    if val:
        return val
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key_name:
                    return v.strip().strip('"').strip("'")
        except Exception:
            pass
    return ""


LLM_PROVIDERS = {
    "qwen3.8-27b-fastmtp": {
        "url": "http://127.0.0.1:8088/v1/chat/completions",
        "model": "qwen3.8-27b-fastmtp",
        "no_think_prefill": True,
        "key_env": "",
    },
    "gemini-4-argon-high": {
        "url": "http://127.0.0.1:8965/v1/chat/completions",
        "model": "gemini-4-argon-high",
        "no_think_prefill": False,
        "key_env": "",
    },
    "gemini-4-argon-medium": {
        "url": "http://127.0.0.1:8965/v1/chat/completions",
        "model": "gemini-4-argon-medium",
        "no_think_prefill": False,
        "key_env": "",
    },
    "gemini-3.8-flash-high": {
        "url": "http://127.0.0.1:8965/v1/chat/completions",
        "model": "gemini-3.8-flash-high",
        "no_think_prefill": False,
        "key_env": "",
    },
    "claude-sonnet-5-5-high": {
        "url": "http://127.0.0.1:8965/v1/chat/completions",
        "model": "claude-sonnet-5-5-high",
        "no_think_prefill": False,
        "key_env": "",
    },
    "claude-opus-5-5-high": {
        "url": "http://127.0.0.1:8965/v1/chat/completions",
        "model": "claude-opus-5-5-high",
        "no_think_prefill": False,
        "key_env": "",
    },
    "deepseek/deepseek-chat": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "deepseek/deepseek-chat",
        "no_think_prefill": False,
        "key_env": "OPENROUTER_API_KEY",
    },
    "openrouter/free": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "openrouter/free",
        "no_think_prefill": False,
        "key_env": "OPENROUTER_API_KEY",
    },
    "gemma4:12b": {
        "url": "http://127.0.0.1:18443/v1/chat/completions",
        "model": "gemma4:12b",
        "no_think_prefill": False,
        "key_env": "",
    },
    "qwen3.6:35b-a3b": {
        "url": "http://127.0.0.1:18443/v1/chat/completions",
        "model": "qwen3.6:35b-a3b",
        "no_think_prefill": False,
        "key_env": "",
    },
}


def call_llm_chat(messages: list[dict], temperature: float = 0.2, max_tokens: int = 3500, timeout: int = 180) -> str:
    cfg = LLM_PROVIDERS.get(llm_model_id, LLM_PROVIDERS["qwen3.8-27b-fastmtp"])
    custom_url = os.environ.get("LLM_BASE_URL", "").strip()
    target_url = f"{custom_url.rstrip('/')}/chat/completions" if custom_url else cfg["url"]
    target_model = cfg["model"] if llm_model_id in LLM_PROVIDERS else llm_model_id

    req_msgs = list(messages)
    if cfg.get("no_think_prefill"):
        req_msgs.append({"role": "assistant", "content": "</think>\n"})

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "AI-Video-Subtitle-Station/2.0",
    }
    api_key = os.environ.get("LLM_API_KEY", "").strip() or (
        _load_hermes_env_key(cfg["key_env"]) if cfg.get("key_env") else ""
    )
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": target_model,
        "messages": req_msgs,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        req = urllib.request.Request(target_url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
            content = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception as e:
        if target_url != LLM_PROVIDERS["qwen3.8-27b-fastmtp"]["url"]:
            print(f"[LLM Warning] Primary model '{llm_model_id}' failed ({e}), falling back to local Qwen3.8-27B (:8088)...")
            fb_msgs = list(messages) + [{"role": "assistant", "content": "</think>\n"}]
            fb_payload = {
                "model": "qwen3.8-27b-fastmtp",
                "messages": fb_msgs,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            req_fb = urllib.request.Request(
                LLM_PROVIDERS["qwen3.8-27b-fastmtp"]["url"],
                data=json.dumps(fb_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req_fb, timeout=timeout) as r_fb:
                body_fb = json.loads(r_fb.read().decode("utf-8"))
                content = (body_fb.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        else:
            raise

    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    content = re.sub(r"^</think>\s*", "", content).strip()
    return content


LANG_NAMES = {
    'zh-TW': '繁體中文（台灣口吻）',
    'en': 'English',
    'ja': '日本語（自然な話し言葉・ネットスラング対応）',
    'ko': '한국어',
    'pt': 'Português',
    'de': 'Deutsch',
    'orig': '原文保留',
}


def parse_time_sec(ts_str: str) -> float:
    h, m, s_ms = ts_str.strip().split(':')
    s, ms = s_ms.split(',')
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def split_clumped_card_text(raw_text: str) -> list[str]:
    """
    將可能雜湊在一起的多則 VTuber 側邊留言或多行字卡拆分為獨立項目：
    1. 依換行符號 `\\n` 或 ` / ` 分隔符切分。
    2. 若單行內含多個連續觀眾留言前綴（如 `>>`、`💬`、`@user:`），自動拆開為獨立留言行。
    """
    if not raw_text:
        return []
    text = raw_text.replace("【字卡】", "\n").strip()
    # 拆分常見多留言串接標記
    text = re.sub(r"\s+/\s+", "\n", text)
    text = re.sub(r"(?<!^)\s+(?=(?:>>|💬|@[A-Za-z0-9_.\-]{2,}\s*[:：]))", "\n", text)
    parts = []
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    return parts


def expand_ocr_detections(ocr_cards: list[dict]) -> list[dict]:
    """
    將 `ocr_detections.json` 中的每一幀偵測展開為獨立的空間字卡/留言項目，
    確保同一幀內的多則側邊留言與主字卡各自獨立比對與去重，不會被綁成一整坨文字。
    """
    expanded = []
    for c in ocr_cards:
        c_sec = float(c.get("sec", 0.0))
        base_x = int(c.get("x", 960))
        base_y = int(c.get("y", 820))
        items = c.get("items")
        if isinstance(items, list) and items:
            for it in items:
                it_text = (it.get("text") or "").strip()
                if len(it_text) > 60:
                    continue
                for sub_t in split_clumped_card_text(it_text):
                    if len(sub_t) > 60:
                        continue
                    expanded.append({
                        "sec": c_sec,
                        "text": sub_t,
                        "x": int(it.get("x", base_x)),
                        "y": int(it.get("y", base_y)),
                        "y_top": int(it.get("y_top", base_y - 20)),
                        "y_bot": int(it.get("y_bot", base_y + 20)),
                        "is_side_comment": bool(it.get("is_side_comment", False)),
                    })
        else:
            raw_t = (c.get("text") or "").strip()
            sub_lines = split_clumped_card_text(raw_t)
            for idx, sub_t in enumerate(sub_lines):
                if len(sub_t) > 60:
                    continue
                expanded.append({
                    "sec": c_sec,
                    "text": sub_t,
                    "x": base_x,
                    "y": min(1020, base_y + idx * 48),
                    "y_top": int(c.get("y_top", base_y - 20)) + idx * 48,
                    "y_bot": int(c.get("y_bot", base_y + 20)) + idx * 48,
                    "is_side_comment": (base_x < 460 or base_x > 1460),
                })
    return expanded


def _is_card_duplicate(cand_clean: str, existing_clean: str) -> bool:
    if not cand_clean or not existing_clean:
        return False
    if cand_clean == existing_clean:
        return True
    if len(cand_clean) >= 4 and len(existing_clean) >= 4:
        if cand_clean in existing_clean or existing_clean in cand_clean:
            return True
    return difflib.SequenceMatcher(None, cand_clean, existing_clean).ratio() > 0.65


def attach_cards_to_dialogues(
    dialogues: list[dict],
    ocr_cards: list[dict],
    max_cards_per_cue: int = 3,
    rolling_dedup_window_sec: float = 9.0,
) -> dict[str, list[str]]:
    """
    智能空間與時序對齊：
    1. 將展開後的獨立字卡/側邊留言關聯至最接近的對白區塊（排除與底層語音台詞重複之燒錄字幕）。
    2. 跨關鍵幀滑動去重（rolling_dedup_window_sec）：VTuber 側邊聊天室留言常在畫面上停留數秒並隨新留言緩慢滾動，
       透過滑動時間窗逐條比對，確保同一則側邊留言不會在相鄰數句台詞重複出現或跟新留言雜湊在一起。
    3. 每個對白區塊最多附掛 max_cards_per_cue 條獨立字卡/留言（主字卡優先於側邊留言）。
    """
    dialogue_cards: dict[str, list[str]] = {d["id"]: [] for d in dialogues}
    if not dialogues or not ocr_cards:
        return dialogue_cards

    expanded_items = expand_ocr_detections(ocr_cards)
    # 主字卡優先處理，其次為側邊留言，同類別依時間排序
    expanded_items.sort(key=lambda it: (float(it.get("sec", 0.0)), 1 if it.get("is_side_comment") else 0))

    matched_indices = set()
    recent_attached: list[tuple[float, str]] = []  # (sec, clean_text)

    def _can_attach(target_d: dict, c_sec: float, c_text: str) -> bool:
        cid = target_d["id"]
        if len(dialogue_cards[cid]) >= max_cards_per_cue:
            return False
        dlg_clean = re.sub(r"\s+", "", target_d["text"])
        card_clean = re.sub(r"\s+", "", c_text)
        if not card_clean:
            return False
        # 排除與語音台詞高度重疊之底部燒錄字幕
        if dlg_clean and difflib.SequenceMatcher(None, dlg_clean, card_clean).ratio() >= 0.45:
            return False
        # 排除同句內重複字卡
        if any(_is_card_duplicate(card_clean, re.sub(r"\s+", "", ex)) for ex in dialogue_cards[cid]):
            return False
        # 排除滑動時間窗內已附掛過之滾動聊天室重複留言
        for prev_sec, prev_clean in recent_attached:
            if abs(c_sec - prev_sec) <= rolling_dedup_window_sec and _is_card_duplicate(card_clean, prev_clean):
                return False
        return True

    for idx, item in enumerate(expanded_items):
        c_sec = float(item.get("sec", 0.0))
        c_text = item.get("text", "").strip()
        if not c_text:
            continue
        best_d = None
        best_dist = 999.0
        for d in dialogues:
            if d["start_sec"] <= c_sec <= d["end_sec"]:
                mid = (d["start_sec"] + d["end_sec"]) / 2.0
                dist = -1.0 + min(0.99, abs(c_sec - mid) * 0.001)
            else:
                dist = min(abs(c_sec - d["start_sec"]), abs(c_sec - d["end_sec"]))
            if dist < best_dist:
                best_dist = dist
                best_d = d
        if best_d and best_dist <= 4.0:
            if _can_attach(best_d, c_sec, c_text):
                dialogue_cards[best_d["id"]].append(c_text)
                recent_attached.append((c_sec, re.sub(r"\s+", "", c_text)))
            matched_indices.add(idx)

    # 處理無對白區間的獨立字卡，關聯至最近對白確保不遺漏
    for idx, item in enumerate(expanded_items):
        if idx not in matched_indices:
            c_sec = float(item.get("sec", 0.0))
            c_text = item.get("text", "").strip()
            if not c_text:
                continue
            best_d = min(dialogues, key=lambda d: abs(c_sec - d["start_sec"]), default=None)
            if best_d and abs(c_sec - best_d["start_sec"]) <= 8.0:
                if _can_attach(best_d, c_sec, c_text):
                    dialogue_cards[best_d["id"]].append(c_text)
                    recent_attached.append((c_sec, re.sub(r"\s+", "", c_text)))

    return dialogue_cards


def run_translation_pipeline():
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

    dialogue_cards = attach_cards_to_dialogues(dialogues, ocr_cards)

    # 記錄每個條目原本擁有的字卡原文
    input_cards_by_id = {cid: list(cards) for cid, cards in dialogue_cards.items() if cards}

    # 組裝包含字卡之時序完整大綱
    full_script = []
    for d in dialogues:
        full_script.append(f"[{d['time']}] (語音) {d['text']}")

    full_script_text = "\n".join(full_script)
    expanded_for_summary = expand_ocr_detections(ocr_cards)
    ocr_summary = "\n".join([f"[{c['sec']}s] 【畫面字卡】 {c['text']}" for c in expanded_for_summary])

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
2. 重要術語、人名與網路梗對應至【{tgt_desc}】的標準譯法對照表（嚴禁輸出非目標語言之譯名！必須直接給出【{tgt_desc}】在地化譯法，如日文請用片假名/日文漢字/VTuber用語）。
   特別注意：畫面擷取的字卡可能包含 OCR 辨識殘缺、斷字或 VTuber 側邊聊天室觀眾留言，請依據人物與語音上下文將其校正還原為正確名稱或句子，並在對照表中列出標準【{tgt_desc}】譯法。
3. 針對【{tgt_desc}】的翻譯語氣指導（口吻、標點習慣）。
【重要語言規範】：請一律使用繁體中文撰寫情境分析與語氣指導（對照表目標欄位請直接給予精確地道的【{tgt_desc}】），絕對禁止輸出英文說明，避免對後續翻譯造成語系干擾！
只輸出分析與名詞表，不要輸出翻譯。
"""

    print(f"Stage 1: Extracting global context and glossary using [{llm_model_id}]...")
    context_knowledge = call_llm_chat(
        messages=[{"role": "user", "content": stage1_prompt}],
        temperature=0.2,
        max_tokens=3500,
        timeout=120,
    )
    if use_s2tw:
        context_knowledge = s2tw.convert(context_knowledge)

    print("\n=== Stage 1 Output (Global Context) ===")
    print(context_knowledge[:800] + "...\n")

    with open("global_context.txt", "w", encoding="utf-8") as f:
        f.write(context_knowledge)

    # STAGE 2: 注入全局背景，逐段依上下文自然意譯
    print(f"Stage 2: Context-aware translation based on global knowledge using [{llm_model_id}]...")

    disallowed_notes = []
    if "zh-TW" not in llm_tgt_langs:
        disallowed_notes.append("絕對禁止輸出中文")
    if "en" not in llm_tgt_langs:
        disallowed_notes.append("絕對禁止輸出英文 (English)")
    if "ko" not in llm_tgt_langs:
        disallowed_notes.append("絕對禁止保留韓文原文 (Hangul)")
    if "ja" not in llm_tgt_langs:
        disallowed_notes.append("絕對禁止輸出日文假名")

    disallowed_str = "、".join(disallowed_notes)
    lang_ban_rule = f"（嚴格警告：目標語言唯一為【{tgt_desc}】，{disallowed_str}！所有對白與字卡必須 100% 翻譯為【{tgt_desc}】！）"

    if "ja" in llm_tgt_langs:
        few_shot_example = """【字卡輸出範例】
輸入：
1
내가 고쳐줄게.
【字卡】자세 교정 이야기 중
【字卡】시청자: 진짜 웃기네 ㅋㅋㅋ

輸出：
1
直してあげるよ。
【字卡】姿勢矯正の話の最中
【字卡】リスナー：マジでウケるwww"""
    elif "en" in llm_tgt_langs:
        few_shot_example = """【字卡輸出範例】
輸入：
1
내가 고쳐줄게.
【字卡】자세 교정 이야기 중
【字卡】시청자: 진짜 웃기네 ㅋㅋㅋ

輸出：
1
I'll fix it for you.
【字卡】Talking about posture correction
【字卡】Viewer: That's hilarious lol"""
    else:
        few_shot_example = """【字卡輸出範例】
輸入：
1
내가 고쳐줄게.
【字卡】자세 교정 이야기 중
【字卡】시청자: 진짜 웃기네 ㅋㅋㅋ

輸出：
1
我會幫妳修好。
【字卡】姿勢矯正話題進行中
【字卡】觀眾：真的超好笑哈哈哈"""

    multi_lang_rule = (
        f"1. 目標字幕語言為：{tgt_desc}。每個字幕區塊請依序輸出一行對應語言的翻譯（共 {len(llm_tgt_langs)} 行對白）{lang_ban_rule}。"
        if len(llm_tgt_langs) > 1 else
        f"1. 請將所有字幕完整翻譯為【{tgt_desc}】{lang_ban_rule}。"
    )

    stage2_system = f"""你是一位頂級影視與 Vtuber 多語翻譯專家。你剛剛已經通讀了全篇影片並建立了以下背景知識庫：
{context_knowledge}

【任務規則】
{multi_lang_rule}
2. 對於原片中的語音語意模糊或辨識瑕疵處，請依照全篇上下文自動校正意譯為【{tgt_desc}】。
3. 【字卡與 VTuber 側邊留言翻譯絕對鐵則（必讀）】：
   - 當字幕條目包含【字卡】時（例如「【字卡】자세 교정 이야기 중」），你【必須】依上下文修復 OCR 錯字，並將該字卡 100% 翻譯為【{tgt_desc}】！
   - 【格式嚴格要求】：每一條字卡或觀眾留言開頭必須嚴格保留「【字卡】」這三個中文字前綴標記，後面緊接翻譯譯文，格式為「【字卡】<目標語言譯文>」。
   - 【多則留言獨立分行（嚴禁雜湊）】：若同一字幕條目下有多行【字卡】（例如多則 VTuber 側邊觀眾留言，或主字卡加側邊留言），每一則【字卡】必須各自獨立輸出一行「【字卡】<譯文>」，絕對不可將多則留言或多個字卡合併擠在同一行！若單一【字卡】原文內夾雜了兩則以上不同觀眾的留言，也請將它們拆分成多行獨立的「【字卡】<譯文>」。
   - 【絕對禁止原文與雙重行】：嚴禁留下未翻譯的原文字卡（如韓文 Hangul、日文假名）！同一則【字卡】只輸出【{tgt_desc}】譯文，不可同時輸出原文與譯文雙重行！
4. 輸入每個區塊格式為「序號 / 原文 /（可選，一至多行）【字卡】」，沒有時間軸。輸出請保持相同格式（第一行序號，其後為對白譯文行與獨立的【字卡】譯文行），區塊之間以空行分隔，不得輸出時間軸，不得跳過或刪減序號。
5. 只輸出翻譯後的區塊內容，不要包含 Markdown 代碼標籤或額外解說。{lang_ban_rule}

{few_shot_example}
"""

    # 組裝包含字卡註記的翻譯區塊
    merged_cues = []
    for d in dialogues:
        card_suffix = "".join([f"\n【字卡】{c}" for c in dialogue_cards.get(d['id'], [])])
        merged_cues.append(f"{d['id']}\n{d['text']}{card_suffix}")

    def translate_chunk(chunk_text):
        res = call_llm_chat(
            messages=[
                {"role": "system", "content": stage2_system},
                {"role": "user", "content": f"請將以下字幕 100% 翻譯為【{tgt_desc}】{lang_ban_rule}。\n【重要指令】：凡有【字卡】之條目，必須保留【字卡】前綴並將其內容 100% 翻譯為【{tgt_desc}】；若有多行【字卡】（如多則側邊留言），請務必逐行獨立輸出【字卡】，絕對不可合併成同一行，{disallowed_str}！\n\n{chunk_text}"},
            ],
            temperature=0.2,
            max_tokens=4096,
            timeout=180,
        )
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

    # 以行導向狀態機嚴格解析：防止模型偷塞序號導致整批時間軸錯位
    cue_lines_map: dict[str, list[str]] = {}
    current_cue_id = None

    for raw_l in raw_translated.splitlines():
        l = raw_l.strip()
        if not l or "-->" in l:
            continue
        # 匹配獨立序號行
        if l.isdigit() and l in orig_time_by_id:
            current_cue_id = l
            if current_cue_id not in cue_lines_map:
                cue_lines_map[current_cue_id] = []
        elif current_cue_id:
            cue_lines_map[current_cue_id].append(l)

    for cid in [d['id'].strip() for d in dialogues]:
        lines = cue_lines_map.get(cid, [])
        if not lines:
            # 若模型遺漏整句，回退保留原文
            fallback_text = orig_text_by_id.get(cid, "")
            fixed_blocks.append({
                "id": cid,
                "time": orig_time_by_id[cid],
                "dlg_lines": [fallback_text],
                "card_lines": [f"【字卡】{c}" for c in input_cards_by_id.get(cid, [])],
            })
            continue

        seen_ids.add(cid)
        dlg_trans = [l for l in lines if not l.startswith("【字卡】")]
        raw_card_trans = [l for l in lines if l.startswith("【字卡】")]

        # 將可能被模型用 " / " 串在同一行的字卡拆分為獨立【字卡】行
        card_trans = []
        for rc in raw_card_trans:
            for sub_c in split_clumped_card_text(rc):
                card_trans.append(f"【字卡】{sub_c}")

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

    # DETERMINISTIC POST-CHECK: 檢測並修補任何殘留原文或未翻譯之字卡（支援單一條目多行獨立字卡修補）
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
        print(f"Detected {len(untranslated_cards)} untranslated card lines. Running targeted LLM repair pass [{llm_model_id}]...")
        repair_lines = "\n".join([f"{cid}_{c_idx}: {c.replace('【字卡】', '').strip()}" for cid, c_idx, c in untranslated_cards])
        if "ja" in llm_tgt_langs:
            repair_ex_out = "1_0: 【字卡】姿勢矯正の話の最中"
        elif "en" in llm_tgt_langs:
            repair_ex_out = "1_0: 【字卡】Talking about posture correction"
        else:
            repair_ex_out = "1_0: 【字卡】姿勢矯正話題進行中"

        repair_prompt = f"""你是一位頂級影視在地化專家。請將以下影片畫面中的字卡或側邊留言逐行翻譯為【{tgt_desc}】：
注意：原字卡可能包含綜藝特效字帶來的 OCR 殘缺或錯字，請依語意校正並翻譯為自然流暢的【{tgt_desc}】。每行對應一則獨立字卡或留言，請勿將不同行合併！{lang_ban_rule}
格式要求：每行輸出必須為「編號: 【字卡】<譯文>」（「【字卡】」三個字請逐字保留作為前綴，不可替換為原文，{disallowed_str}！）：

範例：
輸入：1_0: 자세 교정 이야기 중
輸出：{repair_ex_out}

{repair_lines}"""

        try:
            rep_res = call_llm_chat(
                messages=[{"role": "user", "content": repair_prompt}],
                temperature=0.1,
                max_tokens=1500,
                timeout=90,
            )
            card_map = {}
            for line in rep_res.split("\n"):
                m = re.match(r'^(\d+(?:_\d+)?)\s*[:：]\s*(.*)', line.strip())
                if m:
                    key_id = m.group(1).strip()
                    cleaned = m.group(2).strip()
                    cleaned = re.sub(r'^(【字卡】)+', '', cleaned).strip()
                    if cleaned:
                        card_map[key_id] = f"【字卡】{cleaned}"

            for blk in fixed_blocks:
                cid = blk["id"]
                for c_idx in range(len(blk["card_lines"])):
                    comp_key = f"{cid}_{c_idx}"
                    new_c = card_map.get(comp_key) or (card_map.get(cid) if len(blk["card_lines"]) == 1 else None)
                    if new_c:
                        if use_s2tw:
                            new_c = s2tw.convert(new_c)
                        blk["card_lines"][c_idx] = new_c
            print(f"Successfully repaired {len(card_map)} untranslated cards with 100% target language!")
        except Exception as e:
            print(f"Warning: Card repair pass encountered error: {e}")

    # 最終組裝 SRT：保證每個字卡行格式正規且獨立分行
    final_blocks = []
    for blk in fixed_blocks:
        norm_cards = []
        for c in blk["card_lines"]:
            for sub_c in split_clumped_card_text(c):
                cleaned = f"【字卡】{sub_c}"
                if use_s2tw:
                    cleaned = s2tw.convert(cleaned)
                if cleaned not in norm_cards:
                    norm_cards.append(cleaned)
        lines = [blk["id"], blk["time"]] + blk["dlg_lines"] + norm_cards
        final_blocks.append("\n".join(lines))

    final_full_srt = "\n\n".join(final_blocks) + "\n"
    with open("context_translated_zh.srt", "w", encoding="utf-8") as f:
        f.write(final_full_srt)

    print(f"Saved full context-aware translation to context_translated_zh.srt (total cues: {len(final_blocks)})!")


if __name__ in ("__main__", "<run_path>"):
    run_translation_pipeline()
