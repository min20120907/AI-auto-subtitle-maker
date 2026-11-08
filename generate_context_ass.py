import os, re, json, difflib, opencc

s2tw = opencc.OpenCC('s2twp')
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
    return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000

def to_ass_time(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"

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

def find_card_pos(sec: float) -> tuple[int, int, bool]:
    best_x, best_y, best_dist = 960, 820, 999.0
    matched = None
    for c in ocr_cards:
        d = abs(float(c.get('sec', 0.0)) - sec)
        if d < best_dist and d <= 4.0:
            best_dist = d
            best_x = int(c.get('x', 960))
            best_y = int(c.get('y', 820))
            matched = c
    if matched and 'y_top' in matched and 'y_bot' in matched:
        y_top = int(matched['y_top'])
        y_bot = int(matched['y_bot'])
        # 避免擋住原文字卡：若上方空間足夠則置於原字卡上方，否則置於下方
        if y_top >= 130:
            clear_y = max(65, y_top - 20)
        else:
            clear_y = min(1015, y_bot + 45)
        return best_x, clear_y, True
    return best_x, best_y, False

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
            card_lines = [l.replace('【字卡】', '', 1).strip() for l in raw_lines if l.startswith('【字卡】')]
            dlg_joined = ''.join(dlg_lines)
            kept_cards = [
                c for c in card_lines
                if c and (not dlg_joined or difflib.SequenceMatcher(None, dlg_joined, c).ratio() < 0.85)
            ]
            srt_lines = dlg_lines + [f"【字卡】{c}" for c in kept_cards]
            if srt_lines:
                cx, cy, has_clear = find_card_pos(t0) if kept_cards else (960, 820, False)
                cues.append({
                    'start': t0,
                    'end': t1,
                    'dlg_lines': dlg_lines,
                    'card_lines': kept_cards,
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

    # 2. 畫面字卡事件：直接定位在原字卡座標附近，不加「【字卡】」標記 (Layer 1)
    if c['card_lines']:
        safe_cards = [l.replace('【字卡】', '').replace('{', '\\{').replace('}', '\\}').strip() for l in c['card_lines']]
        card_text = "\\N".join([l for l in safe_cards if l])
        if card_text:
            pos_x = max(120, min(1800, int(c['x'])))
            if c.get('has_clear'):
                pos_y = max(65, min(1020, int(c['y'])))
            else:
                pos_y = max(65, min(1020, int(c['y']) - 45))
            events.append(
                f"Dialogue: 1,{to_ass_time(c['start'])},{to_ass_time(c['end'])},CardFloat,,0,0,0,,{{\\pos({pos_x},{pos_y})}}{card_text}"
            )

with open('context_subtitles_1080p.ass', 'w', encoding='utf-8') as f:
    f.write(ass_header + '\n'.join(events) + '\n')

print(f"Generated context_subtitles_1080p.ass ({len(cues)} cues, card_color={card_color_hex}, card_font={card_font}) successfully!")
