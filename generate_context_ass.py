from __future__ import annotations
import os, re, json, difflib

try:
    import opencc
    s2tw = opencc.OpenCC('s2twp')
except ImportError:
    class _FallbackOpenCC:
        def convert(self, text: str) -> str:
            return text
    s2tw = _FallbackOpenCC()

tgt_langs = [x.strip() for x in os.environ.get('TGT_LANGS', 'zh-TW').split(',') if x.strip()] or ['zh-TW']
use_s2tw = ('zh-TW' in tgt_langs)

if 'ja' in tgt_langs:
    default_font = 'Noto Sans CJK JP'
    style_title = 'Context-Aware Subtitles (ja)'
elif 'ko' in tgt_langs:
    default_font = 'Noto Sans CJK KR'
    style_title = 'Context-Aware Subtitles (ko)'
elif 'zh-TW' in tgt_langs:
    default_font = 'Noto Sans CJK TC'
    style_title = 'Context-Aware Subtitles (zh-TW)'
else:
    default_font = 'Noto Sans'
    style_title = 'Context-Aware Subtitles'

# 讀取字卡自訂樣式設定 (顏色、字型、大小)
style_cfg = {}
if os.path.exists('style_config.json'):
    try:
        style_cfg = json.load(open('style_config.json', encoding='utf-8'))
    except Exception:
        pass

card_color_hex = os.environ.get('CARD_COLOR') or style_cfg.get('card_color') or '#00FFFF'
card_font = os.environ.get('CARD_FONT') or style_cfg.get('card_font') or default_font
card_size = int(os.environ.get('CARD_SIZE') or style_cfg.get('card_size') or 38)


def hex_to_ass_bgr(hex_color: str) -> str:
    h = hex_color.strip().lstrip('#')
    if len(h) != 6:
        return "&H00FFFF00"  # 預設青色 Cyan (BGR: FF FF 00)
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b.upper()}{g.upper()}{r.upper()}"


card_ass_color = hex_to_ass_bgr(card_color_hex)


def parse_time(ts):
    h, m, s_ms = ts.strip().split(':')
    s, ms = s_ms.split(',')
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def to_ass_time(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def split_clumped_card_lines(card_lines: list[str]) -> list[str]:
    """
    將字卡行中可能殘留的多則串接留言（如以 `\\n`、` / ` 或多重 `>>`、`💬`、`@user:` 串接）拆分為獨立字卡行，
    確保每一則 VTuber 側邊留言都能獨立定位與分行排布，不會擠成同一長行。
    """
    result = []
    for raw in card_lines:
        txt = raw.replace('【字卡】', '').strip()
        if not txt:
            continue
        txt = re.sub(r"\s+/\s+", "\n", txt)
        txt = re.sub(r"(?<!^)\s+(?=(?:>>|💬|@[A-Za-z0-9_.\-]{2,}\s*[:：]))", "\n", txt)
        for sub in txt.splitlines():
            s = sub.strip()
            if s and s not in result:
                result.append(s)
    return result


orig_times = {}
if os.path.exists('clean_orig.srt'):
    for b in open('clean_orig.srt', encoding='utf-8').read().strip().split('\n\n'):
        ls = [l.strip() for l in b.split('\n') if l.strip()]
        if len(ls) >= 2 and '-->' in ls[1]:
            orig_times[ls[0]] = ls[1]

ocr_cards = []
if os.path.exists('ocr_detections.json'):
    try:
        ocr_cards = json.load(open('ocr_detections.json', encoding='utf-8'))
    except Exception:
        ocr_cards = []


def _compute_clear_pos(item: dict) -> tuple[int, int, bool]:
    bx = int(item.get('x', 960))
    by = int(item.get('y', 820))
    if 'y_top' in item and 'y_bot' in item:
        y_top = int(item['y_top'])
        y_bot = int(item['y_bot'])
        # 避免擋住原文字卡：若上方空間足夠則置於原字卡上方，否則置於下方
        if y_top >= 130:
            clear_y = max(65, y_top - 20)
        else:
            clear_y = min(1015, y_bot + 45)
        return bx, clear_y, True
    return bx, by, False


def find_card_pos(sec: float, cards_list: list | None = None) -> tuple[int, int, bool]:
    source_cards = ocr_cards if cards_list is None else cards_list
    best_x, best_y, best_dist = 960, 820, 999.0
    matched = None
    for c in source_cards:
        d = abs(float(c.get('sec', 0.0)) - sec)
        if d < best_dist and d <= 4.0:
            best_dist = d
            best_x = int(c.get('x', 960))
            best_y = int(c.get('y', 820))
            matched = c
    if matched:
        return _compute_clear_pos(matched)
    return best_x, best_y, False


def match_cue_cards_to_positions(sec: float, card_lines: list[str], cards_list: list | None = None) -> list[dict]:
    """
    為單一字幕條目下的每一條字卡/側邊留言分配獨立的 (x, y, has_clear) 座標：
    - 若 `ocr_detections.json` 內該時間點具備 `items`（多區域偵測，如同時有主字卡與多則側邊留言），
      依序將各條字卡對應至各自的空間座標，徹底杜絕多則留言與主字卡座標被平均或疊在同一點。
    - 若有多條留言對應至同一側邊區域，自動賦予垂直錯開步進，防止重疊。
    """
    source_cards = ocr_cards if cards_list is None else cards_list
    best_dist = 999.0
    matched = None
    for c in source_cards:
        d = abs(float(c.get('sec', 0.0)) - sec)
        if d < best_dist and d <= 4.0:
            best_dist = d
            matched = c

    spatial_items = []
    if matched:
        if isinstance(matched.get('items'), list) and matched['items']:
            spatial_items = list(matched['items'])
        else:
            spatial_items = [matched]

    placements = []
    for idx, text in enumerate(card_lines):
        if spatial_items:
            sp = spatial_items[idx] if idx < len(spatial_items) else spatial_items[-1]
            bx, by, has_clear = _compute_clear_pos(sp)
            is_side = bool(sp.get('is_side_comment', bx < 460 or bx > 1460))
        else:
            bx, by, has_clear = 960, 820, False
            is_side = False

        placements.append({
            'text': text,
            'x': bx,
            'y': by,
            'has_clear': has_clear,
            'is_side_comment': is_side,
        })
    return placements


def clamp_and_decollide_card_events(cues: list[dict], font_size: int = 38) -> list[dict]:
    """
    對所有字卡與側邊留言進行：
    1. 水平邊界安全保護：當側邊留言位於畫面左右邊緣（x < 400 或 x > 1520）且字數較長時，
       自動依字寬微調 pos_x，防止 Alignment=2 置中錨點導致文字超出 1920 畫布邊界。
    2. 垂直防碰撞排布：當同時間區間或同一條目內有多則側邊留言/字卡位於相近水平區域（|x1 - x2| < 420）時，
       自動以 `min_gap = max(44, font_size + 12)` 垂直錯開，徹底解決側邊多則留言全部疊在一起的問題。
    """
    active_placed: list[dict] = []  # {start, end, x, y}
    min_gap = max(44, int(font_size) + 12)
    resolved_events = []

    for c in cues:
        start_t = float(c['start'])
        end_t = float(c['end'])
        card_items = c.get('card_placements')
        if not card_items and c.get('card_lines'):
            card_items = [
                {
                    'text': l,
                    'x': c.get('x', 960),
                    'y': c.get('y', 820),
                    'has_clear': bool(c.get('has_clear', False)),
                    'is_side_comment': (int(c.get('x', 960)) < 460 or int(c.get('x', 960)) > 1460),
                }
                for l in c['card_lines']
            ]
        if not card_items:
            continue

        for item in card_items:
            clean_txt = item['text'].replace('【字卡】', '').replace('{', '\\{').replace('}', '\\}').strip()
            if not clean_txt:
                continue
            raw_x = int(item.get('x', 960))
            pos_x = max(120, min(1800, raw_x))

            # 若位於左右側邊留言區且文字較長，動態保留左右邊距避免被畫面裁切
            if (pos_x < 400 or pos_x > 1520) and len(clean_txt) > 6:
                est_half_w = min(420, int(len(clean_txt) * font_size * 0.50))
                pos_x = max(max(120, est_half_w + 20), min(min(1800, 1920 - est_half_w - 20), pos_x))

            if item.get('has_clear'):
                pos_y = max(65, min(1020, int(item['y'])))
            else:
                pos_y = max(65, min(1020, int(item['y']) - 45))

            # 檢查與所有時間重疊且水平區域相近的已放置字卡是否發生垂直碰撞
            overlapping = [
                p for p in active_placed
                if max(start_t, p['start']) < min(end_t, p['end']) and abs(pos_x - p['x']) < 420
            ]
            if overlapping:
                # 嘗試向下錯開；若下方接近畫布底端 (> 940)，則改為向上錯開
                direction = 1 if pos_y <= 860 else -1
                attempts = 0
                while any(abs(pos_y - p['y']) < min_gap for p in overlapping) and attempts < 12:
                    pos_y += direction * min_gap
                    if pos_y > 1015:
                        direction = -1
                        pos_y = max(65, int(item['y']) - min_gap)
                    elif pos_y < 65:
                        direction = 1
                        pos_y = min(1015, int(item['y']) + min_gap)
                    attempts += 1
                pos_y = max(65, min(1020, pos_y))

            active_placed.append({
                'start': start_t,
                'end': end_t,
                'x': pos_x,
                'y': pos_y,
            })
            resolved_events.append({
                'start': start_t,
                'end': end_t,
                'x': pos_x,
                'y': pos_y,
                'text': clean_txt,
            })

    return resolved_events


if os.path.exists('context_translated_zh.srt'):
    with open('context_translated_zh.srt', 'r', encoding='utf-8') as f:
        text = f.read().strip()

    blocks = text.split('\n\n') if text else []
    cues = []
    rewritten_srt = []
    for b in blocks:
        lines = [l.strip() for l in b.split('\n') if l.strip()]
        if len(lines) >= 3:
            cid = lines[0]
            if cid in orig_times:
                lines[1] = orig_times[cid]
            m = re.match(r'(\d+:\d+:\d+,\d+)\s*-->\s*(\d+:\d+:\d+,\d+)', lines[1])
            if m:
                t0 = parse_time(m.group(1))
                t1 = parse_time(m.group(2))
                raw_lines = [(s2tw.convert(l) if use_s2tw else l) for l in lines[2:]]
                dlg_lines = [l for l in raw_lines if not l.startswith('【字卡】')]
                raw_card_lines = [l.replace('【字卡】', '', 1).strip() for l in raw_lines if l.startswith('【字卡】')]
                card_lines = split_clumped_card_lines(raw_card_lines)
                dlg_joined = ''.join(dlg_lines)
                kept_cards = []
                for c in card_lines:
                    if not c:
                        continue
                    if dlg_joined and difflib.SequenceMatcher(None, dlg_joined, c).ratio() >= 0.85:
                        continue
                    if not any(difflib.SequenceMatcher(None, c, ex).ratio() >= 0.85 for ex in kept_cards):
                        kept_cards.append(c)
                srt_lines = dlg_lines + [f"【字卡】{c}" for c in kept_cards]
                if srt_lines:
                    cx, cy, has_clear = find_card_pos(t0) if kept_cards else (960, 820, False)
                    card_placements = match_cue_cards_to_positions(t0, kept_cards) if kept_cards else []
                    cues.append({
                        'start': t0,
                        'end': t1,
                        'dlg_lines': dlg_lines,
                        'card_lines': kept_cards,
                        'card_placements': card_placements,
                        'x': cx,
                        'y': cy,
                        'has_clear': has_clear,
                    })
                    rewritten_srt.append('\n'.join([cid, lines[1]] + srt_lines))

    with open('context_translated_zh.srt', 'w', encoding='utf-8') as f:
        f.write(('\n\n'.join(rewritten_srt) + '\n') if rewritten_srt else '\n')

    # Sort and strictly de-overlap timestamps so no ghost 3rd line can ever stack
    cues.sort(key=lambda x: x['start'])
    for i in range(len(cues) - 1):
        if cues[i]['end'] > cues[i+1]['start']:
            cues[i]['end'] = max(cues[i]['start'] + 0.2, cues[i+1]['start'] - 0.05)

    cues = [c for c in cues if c['end'] > c['start']]

    ass_header = f"""[Script Info]
Title: {style_title}
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: SubTop,{default_font},44,&H0000FFFF,&H000000FF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,3.5,1.5,8,40,40,50,1
Style: CardFloat,{card_font},{card_size},{card_ass_color},&H000000FF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,3.2,1.5,2,30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []
    for c in cues:
        # 1. 語音對白事件 (Layer 0)
        if c['dlg_lines']:
            formatted_dlg = []
            for idx, l in enumerate(c['dlg_lines']):
                safe_l = l.replace('{', '\\{').replace('}', '\\}')
                if idx == 0:
                    formatted_dlg.append(safe_l)
                else:
                    formatted_dlg.append(f"{{\\c&HE8E8E8&\\fs34}}{safe_l}{{\\c&H0000FFFF&\\fs44}}")
            text_ass = "\\N".join(formatted_dlg)
            events.append(f"Dialogue: 0,{to_ass_time(c['start'])},{to_ass_time(c['end'])},SubTop,,0,0,0,,{text_ass}")

    # 2. 畫面字卡與側邊留言事件：每條獨立定位並執行垂直防碰撞排布，不加「【字卡】」標記 (Layer 1)
    card_events = clamp_and_decollide_card_events(cues, font_size=card_size)
    for ce in card_events:
        events.append(
            f"Dialogue: 1,{to_ass_time(ce['start'])},{to_ass_time(ce['end'])},CardFloat,,0,0,0,,{{\\pos({ce['x']},{ce['y']})}}{ce['text']}"
        )

    with open('context_subtitles_1080p.ass', 'w', encoding='utf-8') as f:
        f.write(ass_header + '\n'.join(events) + '\n')

    print(f"Generated context_subtitles_1080p.ass ({len(cues)} cues, card_color={card_color_hex}, card_font={card_font}) successfully!")
