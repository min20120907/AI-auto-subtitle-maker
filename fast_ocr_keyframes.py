import glob, time, json, cv2, numpy as np, easyocr, torch, re
from concurrent.futures import ProcessPoolExecutor

def ocr_worker(batch):
    import os
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    torch.set_num_threads(2)
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
        seen_texts = {}
        for reader in readers:
            # 保持 1080p 完整清晰度，並啟用 paragraph=True 聚合相鄰姓名牌與台詞字框
            res = reader.readtext(crop, paragraph=True, canvas_size=1920)
            for item in res:
                bbox, raw_txt = item[0], item[1].strip()
                if len(raw_txt) <= 1 or set(raw_txt).issubset(set('xX. -0123456789_#\'",:;~`!?*()[]{}<>^%$@+=|/\\')):
                    continue
                # Ignore noise without any CJK or word characters
                if not re.search(r'[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff\w]', raw_txt):
                    continue
                clean_chars = re.sub(r'[\W_0-9]', '', raw_txt)
                if len(clean_chars) < 2 and not re.search(r'[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff]', raw_txt):
                    continue
                if raw_txt not in seen_texts:
                    x0, y0 = bbox[0]
                    x1, y1 = bbox[2]
                    real_x = int(((x0 + x1) / 2.0) * (1920.0 / max(1, w)))
                    real_y = int((y_offset + (y0 + y1) / 2.0) * (1080.0 / max(1, h)))
                    real_x = max(120, min(1800, real_x))
                    real_y = max(80, min(1020, real_y))
                    seen_texts[raw_txt] = (real_x, real_y)
        if seen_texts:
            # 若同一畫面有多個字卡區塊，合併文字並取平均或主字卡座標
            combined_text = ' '.join(seen_texts.keys())
            avg_x = int(np.mean([pos[0] for pos in seen_texts.values()]))
            avg_y = int(np.mean([pos[1] for pos in seen_texts.values()]))
            results.append({"sec": sec, "text": combined_text, "x": avg_x, "y": avg_y})
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
