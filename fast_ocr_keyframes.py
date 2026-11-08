from __future__ import annotations
import glob, time, json, cv2, numpy as np, re
from concurrent.futures import ProcessPoolExecutor

try:
    import easyocr
except ImportError:
    easyocr = None

try:
    import torch
except ImportError:
    torch = None


# 側邊直播聊天室常見純雜訊樣式（時間戳、純帳號 Handle、觀看人數、SuperChat 標頭等）
_CHAT_NOISE_PATTERNS = re.compile(
    r"^(\d{1,2}:\d{2}(\s*(AM|PM|am|pm))?|@[A-Za-z0-9_.\-]+|\[?\s*(LIVE|Chat|채팅|チャット|聊天室|留言)\s*\]?|"
    r"[₩¥$€£]\s*[\d,.]+|[\d,.]+\s*(명|人|views|watching))$",
    re.IGNORECASE,
)


def is_valid_ocr_text(raw_txt: str) -> bool:
    """檢查 OCR 文字是否為有意義之字幕/字卡/留言內容（排除單字元與純符號雜訊）。"""
    txt = (raw_txt or "").strip()
    if len(txt) <= 1 or set(txt).issubset(set("xX. -0123456789_#'\",:;~`!?*()[]{}<>^%$@+=|/\\")):
        return False
    if _CHAT_NOISE_PATTERNS.match(txt):
        return False
    if not re.search(r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff\w]", txt):
        return False
    clean_chars = re.sub(r"[\W_0-9]", "", txt)
    if len(clean_chars) < 2 and not re.search(r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff]", txt):
        return False
    return True


def _cjk_char_score(txt: str) -> float:
    """計算文字中之 CJK 與有效詞彙比例，供跨語系 Reader 重疊字框擇優。"""
    if not txt:
        return 0.0
    cjk_count = len(re.findall(r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff]", txt))
    word_count = len(re.findall(r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff\w]", txt))
    return cjk_count * 1.5 + word_count * 0.5


def is_side_chat_zone(x_min: float, x_max: float, cx: float, box_w: float) -> bool:
    """
    判斷字框是否位於 1920x1080 畫面左右兩側之 VTuber 聊天室/留言欄區域。
    左側留言區：中心點 cx < 460 且右界 < 700
    右側留言區：中心點 cx > 1460 且左界 > 1220
    """
    if box_w > 680:
        return False
    if cx < 460 and x_max < 700:
        return True
    if cx > 1460 and x_min > 1220:
        return True
    return False


def dedup_cross_reader_boxes(raw_boxes: list[dict]) -> list[dict]:
    """
    當啟用多語系 EasyOCR Reader（例如 ko + ja）時，同一畫面位置的字框會被不同 Reader 重複辨識出不同文字，
    舊版僅以字串 seen_texts 去重會導致同一句留言/字卡被拼成兩段亂碼。
    此函式依 1920x1080 空間重疊度去重，同一實體字框僅保留信心度與字元品質最高者。
    """
    if not raw_boxes:
        return []
    sorted_boxes = sorted(
        raw_boxes,
        key=lambda b: float(b.get("conf", 1.0)) * 2.0 + _cjk_char_score(b.get("text", "")),
        reverse=True,
    )
    kept = []
    for cand in sorted_boxes:
        c_xmin, c_xmax = cand["x_min"], cand["x_max"]
        c_ymin, c_ymax = cand["y_min"], cand["y_max"]
        c_w = max(1.0, c_xmax - c_xmin)
        c_h = max(1.0, c_ymax - c_ymin)
        is_dup_box = False
        for ex in kept:
            if cand["text"] == ex["text"] and abs(cand["cy"] - ex["cy"]) <= max(c_h, ex["h"]) * 0.6:
                is_dup_box = True
                break
            v_ov = max(0.0, min(c_ymax, ex["y_max"]) - max(c_ymin, ex["y_min"])) / max(1.0, min(c_h, ex["h"]))
            h_ov = max(0.0, min(c_xmax, ex["x_max"]) - max(c_xmin, ex["x_min"])) / max(1.0, min(c_w, ex["w"]))
            if v_ov >= 0.55 and h_ov >= 0.45:
                is_dup_box = True
                break
        if not is_dup_box:
            kept.append(cand)
    return kept


def merge_same_line_boxes(boxes: list[dict]) -> list[dict]:
    """
    僅將「同一水平行、相鄰近距」的字框（如同一行的『[姓名牌]』與『台詞』）合併為單行，
    嚴格禁止垂直跨行合併（避免把 VTuber 側邊聊天室上下排列的多則觀眾留言全部黏成同一行），
    且禁止跨越側邊留言區與中央主字幕區合併。
    """
    if not boxes:
        return []
    sorted_b = sorted(boxes, key=lambda b: (round(b["cy"] / 12.0), b["x_min"]))
    lines: list[dict] = []

    for b in sorted_b:
        merged = False
        b_side = is_side_chat_zone(b["x_min"], b["x_max"], b["cx"], b["w"])
        for line in lines:
            l_side = is_side_chat_zone(line["x_min"], line["x_max"], line["cx"], line["w"])
            # 不可將側邊聊天室字框與中央主字卡合併
            if b_side != l_side:
                continue
            min_h = max(1.0, min(line["h"], b["h"]))
            max_h = max(1.0, max(line["h"], b["h"]))
            # 字級高度差距過大代表不同層級 UI/字卡，不合併
            if max_h / min_h > 1.85:
                continue
            # 必須在同一水平線上（垂直中心差 <= 0.42 倍字高），絕不把上下行的留言合併
            if abs(line["cy"] - b["cy"]) > 0.42 * min_h:
                continue
            # 計算水平間距（支援由左至右排序）
            if b["x_min"] >= line["x_min"]:
                gap_x = b["x_min"] - line["x_max"]
            else:
                gap_x = line["x_min"] - b["x_max"]
            max_gap = min(110.0, 1.45 * max_h)
            if -0.35 * min_h <= gap_x <= max_gap:
                # 依 X 座標由左至右拼接同一行文字
                if b["x_min"] >= line["x_min"]:
                    if b["text"] not in line["text"]:
                        line["text"] = f"{line['text']} {b['text']}".strip()
                else:
                    if b["text"] not in line["text"]:
                        line["text"] = f"{b['text']} {line['text']}".strip()
                line["x_min"] = min(line["x_min"], b["x_min"])
                line["x_max"] = max(line["x_max"], b["x_max"])
                line["y_min"] = min(line["y_min"], b["y_min"])
                line["y_max"] = max(line["y_max"], b["y_max"])
                line["w"] = line["x_max"] - line["x_min"]
                line["h"] = line["y_max"] - line["y_min"]
                line["cx"] = (line["x_min"] + line["x_max"]) / 2.0
                line["cy"] = (line["y_min"] + line["y_max"]) / 2.0
                line["conf"] = max(line.get("conf", 1.0), b.get("conf", 1.0))
                merged = True
                break
        if not merged:
            lines.append(dict(b))

    return lines


def filter_and_separate_regions(lines: list[dict], max_side_comments: int = 3) -> list[dict]:
    """
    分離主畫面綜藝字卡與 VTuber 側邊留言，並過濾直播聊天室刷屏小字雜訊：
    1. 過濾不合法或單字元雜訊，以及側邊欄字高過小（< 24px）之聊天室洗版碎字。
    2. 識別側邊垂直堆疊之聊天室欄位（左/右側欄或同 X 軸垂直連續 >= 3 行之窄欄）：
       - 若畫面同時存在中央/主畫面大字卡，且側邊為密集小字聊天室視窗（>= 3 行且字高明顯小於主字卡），
         自動剔除背景聊天室刷屏雜訊，避免側邊聊天室與主字卡混雜。
       - 若為需要保留之側邊觀眾留言字卡，每一則留言各自維持獨立座標與獨立項目，絕不與其他留言合併成單行或取平均座標。
    """
    valid_lines = []
    for l in lines:
        txt = re.sub(r"\s+", " ", l.get("text", "")).strip()
        if not is_valid_ocr_text(txt):
            continue
        l_copy = dict(l)
        l_copy["text"] = txt
        l_copy["is_side_comment"] = is_side_chat_zone(l["x_min"], l["x_max"], l["cx"], l["w"])
        # 側邊聊天室極小字（1080p 下高度 < 24px）多為直播聊天室刷屏 ID/時間戳，直接過濾
        if l_copy["is_side_comment"] and l_copy["h"] < 24.0:
            continue
        valid_lines.append(l_copy)

    if not valid_lines:
        return []

    # 偵測垂直堆疊之窄欄（即使 X 略微偏離 460/1460，只要 >= 3 行在同一個窄欄垂直排列且字高偏小，亦視為側邊留言欄）
    for i, li in enumerate(valid_lines):
        if li["w"] <= 620 and (li["cx"] < 600 or li["cx"] > 1320):
            col_peers = [
                lj for lj in valid_lines
                if lj["w"] <= 620
                and abs(lj["cx"] - li["cx"]) <= 150.0
                and abs(lj["cy"] - li["cy"]) <= 280.0
            ]
            if len(col_peers) >= 3:
                li["is_side_comment"] = True

    main_cards = [l for l in valid_lines if not l["is_side_comment"]]
    side_comments = [l for l in valid_lines if l["is_side_comment"]]

    # 若同時有主字卡與密集側邊聊天室（>= 3 行小字），且側邊平均字高明顯較小，排除背景直播聊天室洗版
    if main_cards and len(side_comments) >= 3:
        max_main_h = max(m["h"] for m in main_cards)
        avg_side_h = float(np.mean([s["h"] for s in side_comments]))
        if avg_side_h < 36.0 or avg_side_h < max_main_h * 0.78:
            side_comments = []

    # 對保留的側邊留言按 Y 由上而下排序，並限制單一畫面最多保留前 max_side_comments 則最清晰的留言
    if len(side_comments) > max_side_comments:
        side_comments = sorted(
            side_comments,
            key=lambda s: (s["h"] * float(s.get("conf", 1.0)), -s["cy"]),
            reverse=True,
        )[:max_side_comments]
    side_comments.sort(key=lambda s: (s["cy"], s["cx"]))
    main_cards.sort(key=lambda m: (m["cy"], m["cx"]))

    # 主字卡優先排列在前，側邊留言依序獨立排列在後
    ordered = main_cards + side_comments
    items = []
    seen_norm = set()
    for reg in ordered:
        norm_key = re.sub(r"\s+", "", reg["text"])
        if norm_key in seen_norm:
            continue
        seen_norm.add(norm_key)
        real_x = max(120, min(1800, int(round(reg["cx"]))))
        real_y = max(80, min(1020, int(round(reg["cy"]))))
        norm_top = max(40, min(1040, int(round(reg["y_min"]))))
        norm_bot = max(60, min(1060, int(round(reg["y_max"]))))
        items.append({
            "text": reg["text"],
            "x": real_x,
            "y": real_y,
            "y_top": norm_top,
            "y_bot": norm_bot,
            "is_side_comment": bool(reg["is_side_comment"]),
        })
    return items


def extract_frame_ocr_items(raw_reader_outputs: list, w: int, h: int, y_offset: int) -> list[dict]:
    """
    將多個或單個 EasyOCR reader 的輸出轉為標準化 1920x1080 座標字框，
    執行跨 Reader 空間去重、同行水平合併、以及 VTuber 側邊留言分離與雜訊過濾。
    """
    scale_x = 1920.0 / max(1, w)
    scale_y = 1080.0 / max(1, h)
    raw_boxes = []

    for item in raw_reader_outputs:
        if not item or len(item) < 2:
            continue
        bbox = item[0]
        raw_txt = (item[1] or "").strip()
        conf = float(item[2]) if len(item) > 2 and item[2] is not None else 1.0
        if not raw_txt or conf < 0.15:
            continue
        # 若為純符號/無文字雜訊先行略過
        if not re.search(r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff\w]", raw_txt):
            continue
        try:
            xs = [float(pt[0]) for pt in bbox]
            ys = [float(pt[1]) for pt in bbox]
        except Exception:
            continue
        x_min = min(xs) * scale_x
        x_max = max(xs) * scale_x
        y_min = (y_offset + min(ys)) * scale_y
        y_max = (y_offset + max(ys)) * scale_y

        # 若輸入的單一區塊內已含換行，拆分為獨立子行處理
        sub_lines = [s.strip() for s in raw_txt.splitlines() if s.strip()]
        if not sub_lines:
            continue
        total_h = max(4.0, y_max - y_min)
        step_h = total_h / len(sub_lines)
        for idx, sub_t in enumerate(sub_lines):
            sy_min = y_min + idx * step_h
            sy_max = sy_min + step_h
            raw_boxes.append({
                "text": sub_t,
                "conf": conf,
                "x_min": x_min,
                "x_max": x_max,
                "y_min": sy_min,
                "y_max": sy_max,
                "w": max(1.0, x_max - x_min),
                "h": max(1.0, sy_max - sy_min),
                "cx": (x_min + x_max) / 2.0,
                "cy": (sy_min + sy_max) / 2.0,
            })

    deduped = dedup_cross_reader_boxes(raw_boxes)
    merged_lines = merge_same_line_boxes(deduped)
    return filter_and_separate_regions(merged_lines)


def ocr_worker(batch, readers=None):
    import os
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    if torch is not None:
        torch.set_num_threads(2)
    if readers is None:
        raw_langs = [x.strip() for x in os.environ.get('SRC_LANGS', 'ko').split(',') if x.strip() and x.strip() != 'auto']
        lang_map = {'ko': 'ko', 'ja': 'ja', 'zh': 'ch_tra', 'zh-TW': 'ch_tra', 'pt': 'pt', 'de': 'de'}
        non_en = [lang_map[k] for k in raw_langs if k in lang_map]
        # EasyOCR allows at most one CJK script per Reader alongside 'en'
        readers = [easyocr.Reader([lg, 'en'], gpu=False, verbose=False) for lg in (non_en or ['ko'])]

    results = []
    for f, sec in batch:
        img = cv2.imread(f)
        if img is None:
            continue
        h, w = img.shape[:2]
        if h > w:
            # Vertical / portrait Shorts (h > w): text cards often appear from 12% to 92%
            y_offset = int(h * 0.12)
            y_end = int(h * 0.92)
        else:
            # Standard horizontal 16:9 video: subtitles and cards in lower 55%
            y_offset = int(h * 0.45)
            y_end = h
        crop = img[y_offset:y_end, :]
        raw_outputs = []
        for reader in readers:
            # 以 paragraph=False 擷取原始字框，再由 merge_same_line_boxes 僅對同行水平聚合，
            # 徹底防止 VTuber 影片旁邊垂直排列的聊天室/觀眾留言被 paragraph=True 黏成一大串！
            try:
                res = reader.readtext(crop, paragraph=False, canvas_size=1920)
            except TypeError:
                res = reader.readtext(crop, paragraph=True, canvas_size=1920)
            if res:
                raw_outputs.extend(res)

        items = extract_frame_ocr_items(raw_outputs, w=w, h=h, y_offset=y_offset)
        if items:
            # 主座標採用主字卡（優先非側邊留言之主字卡），絕不將左右兩側與中央字卡的座標硬取平均
            primary = next((it for it in items if not it.get("is_side_comment")), items[0])
            # 多個獨立字卡/留言以換行符號分隔，不再以空白字元全部雜湊成同一行
            combined_text = "\n".join(it["text"] for it in items)
            results.append({
                "sec": sec,
                "text": combined_text,
                "x": int(primary["x"]),
                "y": int(primary["y"]),
                "y_top": int(primary["y_top"]),
                "y_bot": int(primary["y_bot"]),
                "items": items,
            })
    return results


def main():
    files = sorted(glob.glob('frames/f_*.jpg'))
    keyframes = []
    prev_strip = None
    for idx, f in enumerate(files):
        sec = idx * 3.0
        img = cv2.imread(f, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        h, w = img.shape
        if h > w:
            strip = img[int(h * 0.12):int(h * 0.92), :]
        else:
            strip = img[int(h * 0.40):h, :]
        edges = cv2.Canny(strip, 100, 200)
        edge_density = np.mean(edges > 0)
        if idx == 0 or edge_density > 0.015:
            if prev_strip is None or cv2.absdiff(strip, prev_strip).mean() > 5.0:
                keyframes.append((f, sec))
                prev_strip = strip

    print(f"Filtering complete: Found {len(keyframes)} keyframes with text.")
    
    # Split into 8 chunks for 8 worker processes on 32-thread CPU
    num_workers = 8
    chunks = [keyframes[i::num_workers] for i in range(num_workers)]
    
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=num_workers) as ex:
        worker_results = list(ex.map(ocr_worker, chunks))
    
    all_detections = sorted([item for sub in worker_results for item in sub], key=lambda x: x["sec"])
    print(f"OCR finished in {time.time()-t0:.2f}s! Total valid detections: {len(all_detections)}")
    
    with open('ocr_detections.json', 'w', encoding='utf-8') as out_f:
        json.dump(all_detections, out_f, ensure_ascii=False, indent=2)
    print("Saved to ocr_detections.json")


if __name__ == '__main__':
    main()
