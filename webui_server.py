import os, sys, json, re, time, shutil, subprocess, threading
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse, PlainTextResponse
import uvicorn

BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"
HISTORY_FILE = BASE_DIR / "history.json"
STYLE_FILE = BASE_DIR / "style_config.json"
app = FastAPI(title="Video Context Subtitle WebUI")

# ponytail: single global job state; switch to task queue if multi-user concurrent uploads needed
JOB_STATUS = {"state": "idle", "step": "就緒", "title": "甘橘 (감귤) - 東京池袋日常 Vlog", "active_id": "job_ikebukuro"}

ARTIFACT_FILES = [
    "video_context_1080p.mp4",
    "context_translated_zh.srt",
    "context_subtitles_1080p.ass",
    "subtitles.vtt",
    "transcript.json",
    "global_context.txt",
    "ocr_detections.json",
]

DEFAULT_STYLE = {
    "card_color": "#00FFFF",
    "card_font": "Noto Sans CJK JP",
    "card_size": 38,
}

def load_style() -> dict:
    if STYLE_FILE.exists():
        try:
            data = json.loads(STYLE_FILE.read_text(encoding="utf-8"))
            return {**DEFAULT_STYLE, **data}
        except Exception:
            pass
    return dict(DEFAULT_STYLE)

def save_style(cfg: dict) -> dict:
    merged = {**load_style(), **cfg}
    STYLE_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged

def _link_or_copy(src: Path, dst: Path):
    if not src.exists():
        return
    dst.unlink(missing_ok=True)
    # 只有 mp4 用硬連結（管線一律「寫暫存檔再 replace」，不會原地改寫）。
    # 文字類產物（srt/vtt/json/ass/txt）會被各腳本以 open('w') 原地覆寫，
    # 若與歷史記錄共用 inode，下一支影片會把所有舊記錄一起蓋掉，所以必須複製。
    if src.suffix.lower() == ".mp4":
        try:
            os.link(src, dst)
            return
        except OSError:
            pass
    shutil.copy2(src, dst)

def load_history() -> list:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []

def save_history(items: list):
    HISTORY_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

def snapshot_job(job_id: str, title: str, src_langs: list[str], tgt_langs: list[str], soft_subs: bool = False):
    jdir = JOBS_DIR / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    for fname in ARTIFACT_FILES:
        _link_or_copy(BASE_DIR / fname, jdir / fname)
    items = [x for x in load_history() if x.get("id") != job_id]
    items.insert(0, {
        "id": job_id,
        "title": title,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "src_langs": src_langs,
        "tgt_langs": tgt_langs,
        "soft_subs": soft_subs,
    })
    save_history(items)
    JOB_STATUS["active_id"] = job_id

def ensure_seed_history():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    if not STYLE_FILE.exists():
        save_style(DEFAULT_STYLE)
    hist = load_history()
    if hist:
        JOB_STATUS["active_id"] = hist[0]["id"]
        JOB_STATUS["title"] = hist[0]["title"]
        j0 = JOBS_DIR / hist[0]["id"]
        j0.mkdir(parents=True, exist_ok=True)
        for fname in ARTIFACT_FILES:
            if not (j0 / fname).exists() and (BASE_DIR / fname).exists():
                _link_or_copy(BASE_DIR / fname, j0 / fname)
    elif (BASE_DIR / "video_context_1080p.mp4").exists():
        snapshot_job("ikebukuro_vlog", "감귤 (甘橘) - 日本東京池袋獨旅 Vlog", ["ko", "ja"], ["zh-TW"])

ensure_seed_history()

def _fmt_srt_ts(sec: float) -> str:
    h, r = divmod(max(0.0, sec), 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s - int(s))*1000):03d}"

def _parse_ts(ts: str) -> float:
    h, m, s_ms = ts.strip().split(":")
    s, ms = s_ms.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

def _find_card_xy(sec: float, ocr_cards: list) -> tuple[int, int]:
    best_x, best_y, best_dist = 960, 820, 999.0
    for c in ocr_cards:
        d = abs(float(c.get("sec", 0.0)) - sec)
        if d < best_dist and d <= 4.0:
            best_dist = d
            best_x = int(c.get("x", 960))
            best_y = int(c.get("y", 820))
    return best_x, best_y

def build_vtt_and_transcript(srt_path: Path):
    style_cfg = load_style()
    card_color = style_cfg.get("card_color", "#00FFFF")
    card_font = style_cfg.get("card_font", "Noto Sans CJK JP")
    ocr_cards = []
    ocr_path = BASE_DIR / "ocr_detections.json"
    if ocr_path.exists():
        try:
            ocr_cards = json.loads(ocr_path.read_text(encoding="utf-8"))
        except Exception:
            ocr_cards = []

    raw_text = srt_path.read_text(encoding="utf-8").strip() if srt_path.exists() else ""
    blocks = raw_text.split("\n\n") if raw_text else []
    transcript = []
    vtt_lines = [
        "WEBVTT",
        "",
        "STYLE",
        f"::cue(c.card) {{ color: {card_color}; font-family: '{card_font}', sans-serif; font-weight: 600; text-shadow: 0 0 4px #000, 1px 1px 2px #000; }}",
        "",
    ]
    for b in blocks:
        lines = [l.strip() for l in b.split("\n") if l.strip()]
        if len(lines) < 3:
            continue
        m = re.match(r"(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)", lines[1])
        if not m:
            continue
        t0, t1 = _parse_ts(m.group(1)), _parse_ts(m.group(2))
        dlg, cards = [], []
        for l in lines[2:]:
            if l.startswith("【字卡】"):
                cleaned = l.replace("【字卡】", "", 1).strip()
                if cleaned:
                    cards.append(cleaned)
            else:
                dlg.append(l)
        card_str = " / ".join(cards) if cards else None
        cx, cy = _find_card_xy(t0, ocr_cards) if card_str else (960, 820)
        dialogue_str = "\n".join(dlg) if dlg else (card_str or "")
        transcript.append({
            "id": lines[0],
            "start": round(t0, 3),
            "end": round(t1, 3),
            "time": lines[1],
            "dialogue": dialogue_str,
            "card": card_str,
            "card_x": cx,
            "card_y": cy,
        })
        vtt_ts = lines[1].replace(",", ".")
        if dlg:
            vtt_lines.extend([lines[0], vtt_ts, "\n".join(dlg), ""])
        if card_str:
            pct_x = max(10, min(90, int(round(cx / 1920.0 * 100))))
            pct_y = max(8, min(88, int(round(max(60, cy - 45) / 1080.0 * 100))))
            vtt_lines.extend([
                f"{lines[0]}-card",
                f"{vtt_ts} line:{pct_y}% position:{pct_x}% align:center",
                f"<c.card>{card_str}</c>",
                "",
            ])
    (BASE_DIR / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    (BASE_DIR / "subtitles.vtt").write_text("\n".join(vtt_lines), encoding="utf-8")

def detect_multi_langs(raw_video: Path, model) -> list[str]:
    """多點抽樣自動語種探測（支援單一影片含多語混雜，如韓+日或日+英）"""
    supported = {"ko", "ja", "en", "zh", "pt", "de"}
    dur = 120.0
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(raw_video)],
            capture_output=True, text=True, timeout=10
        )
        if res.stdout.strip():
            dur = max(10.0, float(res.stdout.strip().splitlines()[0]))
    except Exception:
        pass

    sample_points = sorted(set([
        max(0.0, round(dur * 0.15, 1)),
        max(0.0, round(dur * 0.50, 1)),
        max(0.0, round(dur * 0.80, 1)),
    ]))
    lang_scores: dict[str, float] = {}
    for pt in sample_points:
        end_pt = min(dur, pt + 20.0)
        if end_pt <= pt + 1.0:
            continue
        try:
            _, info = model.transcribe(
                str(raw_video),
                language=None,
                beam_size=1,
                vad_filter=True,
                clip_timestamps=f"{pt},{end_pt}",
            )
            all_probs = getattr(info, "all_language_probs", None) or [(info.language, info.language_probability)]
            for lg, prob in all_probs:
                if lg in supported:
                    lang_scores[lg] = lang_scores.get(lg, 0.0) + float(prob)
        except Exception:
            continue

    if not lang_scores:
        return ["ko"]
    total = max(1e-6, sum(lang_scores.values()))
    ranked = sorted([(lg, sc / len(sample_points)) for lg, sc in lang_scores.items()], key=lambda x: x[1], reverse=True)
    # 選出平均機率 >= 0.12 的所有活躍語種（最多前 3 個），確保多語混雜影片不漏判
    detected = [lg for lg, avg_p in ranked if avg_p >= 0.12][:3]
    return detected or [ranked[0][0]]

def ensure_h264(video_path: Path):
    """確保影片為 iPadOS WebKit / Brave 原生可解碼之 H.264 + AAC + yuv420p + faststart"""
    if not video_path.exists():
        return
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=10
        )
        v_codec = res.stdout.strip().lower()
        a_res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=10
        )
        a_codec = a_res.stdout.strip().lower()
        has_audio = bool(a_codec)

        needs_v = bool(v_codec and v_codec != "h264")
        needs_a = bool(has_audio and a_codec != "aac")

        if needs_v or needs_a:
            tmp_path = video_path.with_name(f"tmp_h264_{int(time.time())}_{video_path.name}")
            cmd = ["ffmpeg", "-y", "-i", str(video_path)]
            if needs_v:
                cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p"])
            else:
                cmd.extend(["-c:v", "copy"])
            if has_audio:
                if needs_a:
                    cmd.extend(["-c:a", "aac", "-b:a", "192k"])
                else:
                    cmd.extend(["-c:a", "copy"])
            cmd.extend(["-movflags", "+faststart", str(tmp_path)])
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tmp_path.replace(video_path)
    except Exception as e:
        print(f"ensure_h264 warning: {e}")

def run_youtube_pipeline(url: str, src_langs: list[str], tgt_langs: list[str]):
    try:
        yt_dlp = str(Path(sys.executable).parent / "yt-dlp")
        JOB_STATUS.update({"state": "running", "step": "0/4 解析與下載 YouTube 影片 (yt-dlp)...", "title": url})
        t_res = subprocess.run([yt_dlp, "--no-playlist", "--get-title", url], cwd=BASE_DIR, capture_output=True, text=True, timeout=60)
        title = (t_res.stdout.strip().splitlines() or [url])[0]
        JOB_STATUS["title"] = title
        raw_dest = BASE_DIR / "uploaded_input.mp4"
        subprocess.run([
            yt_dlp, "--no-playlist",
            "-f", "bestvideo[height<=1920][width<=1920][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[height<=1920][width<=1920]+bestaudio/best",
            "--merge-output-format", "mp4", "--force-overwrites", "-o", str(raw_dest), url
        ], cwd=BASE_DIR, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ensure_h264(raw_dest)
        run_pipeline(raw_dest, title, src_langs, tgt_langs)
    except Exception as e:
        JOB_STATUS.update({"state": "error", "step": f"❌ YT 下載失敗: {e}"})

def run_pipeline(raw_video: Path, title: str, src_langs: list[str] | None = None, tgt_langs: list[str] | None = None):
    src_langs = src_langs or ["auto"]
    tgt_langs = tgt_langs or ["zh-TW"]
    try:
        ensure_h264(raw_video)
        from faster_whisper import WhisperModel
        model = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=16, num_workers=2)

        if "auto" in src_langs:
            JOB_STATUS.update({"state": "running", "step": "1/4 多點抽樣自動語種探測中...", "title": title})
            src_langs = detect_multi_langs(raw_video, model)

        style_cfg = load_style()
        env = {
            **os.environ,
            "VIDEO_TITLE": title,
            "SRC_LANGS": ",".join(src_langs),
            "TGT_LANGS": ",".join(tgt_langs),
            "CARD_COLOR": style_cfg.get("card_color", "#00FFFF"),
            "CARD_FONT": style_cfg.get("card_font", "Noto Sans CJK JP"),
            "CARD_SIZE": str(style_cfg.get("card_size", 38)),
        }

        # 階段 1 & 2 並行流水線 (Item 3)：Whisper 語音轉錄與 FFmpeg 抽幀 + EasyOCR 同時執行
        JOB_STATUS.update({
            "state": "running",
            "step": f"1-2/4 並行執行：語音轉錄 + 1080p 畫面字卡 OCR ({','.join(src_langs)})...",
            "title": title,
        })

        ocr_err = []
        def _ocr_task():
            try:
                frames_dir = BASE_DIR / "frames"
                shutil.rmtree(frames_dir, ignore_errors=True)
                frames_dir.mkdir(exist_ok=True)
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(raw_video), "-vf", "fps=1/3", str(frames_dir / "f_%04d.jpg")],
                    cwd=BASE_DIR, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                subprocess.run([sys.executable, "fast_ocr_keyframes.py"], cwd=BASE_DIR, env=env, check=True)
            except Exception as ex:
                ocr_err.append(ex)

        ocr_thread = threading.Thread(target=_ocr_task, daemon=True)
        ocr_thread.start()

        whisper_lang = src_langs[0].split("-")[0] if len(src_langs) == 1 else None
        segments, _ = model.transcribe(str(raw_video), language=whisper_lang, beam_size=1, vad_filter=True)
        srt_blocks = []
        for idx, seg in enumerate(segments, 1):
            if seg.text.strip():
                srt_blocks.append(f"{idx}\n{_fmt_srt_ts(seg.start)} --> {_fmt_srt_ts(seg.end)}\n{seg.text.strip()}")
        (BASE_DIR / "clean_orig.srt").write_text("\n\n".join(srt_blocks) + "\n", encoding="utf-8")

        ocr_thread.join()
        if ocr_err:
            raise ocr_err[0]

        JOB_STATUS["step"] = f"3/4 Qwen3.8-27B 全域情境去噪與多語翻譯 ({','.join(tgt_langs)})..."
        subprocess.run([sys.executable, "agentic_full_translate.py"], cwd=BASE_DIR, env=env, check=True)
        subprocess.run([sys.executable, "generate_context_ass.py"], cwd=BASE_DIR, env=env, check=True)
        build_vtt_and_transcript(BASE_DIR / "context_translated_zh.srt")

        # 預覽不燒錄：網頁播放器直接掛 VTT 字幕軌，硬字幕影片改為「需要時再壓製」(/api/burn)
        JOB_STATUS["step"] = "4/4 準備預覽影片..."
        preview_tmp = BASE_DIR / "video_preview_tmp.mp4"
        shutil.copyfile(raw_video, preview_tmp)
        ensure_h264(preview_tmp)
        preview_tmp.replace(BASE_DIR / "video_context_1080p.mp4")
        job_id = f"job_{int(time.time())}"
        snapshot_job(job_id, title, src_langs, tgt_langs, soft_subs=True)
        JOB_STATUS.update({"state": "done", "step": "✅ 處理完成", "title": title, "active_id": job_id})
    except Exception as e:
        JOB_STATUS.update({"state": "error", "step": f"❌ 失敗: {e}"})

# Helper for HTTP 206 Partial Content (smooth video seeking)
def range_requests_response(request: Request, file_path: Path, content_type: str = "video/mp4"):
    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(file_path, media_type=content_type)

    range_match = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not range_match:
        raise HTTPException(status_code=400, detail="Invalid Range Header")

    start = int(range_match.group(1))
    end = int(range_match.group(2)) if range_match.group(2) else file_size - 1
    end = min(end, file_size - 1)
    chunk_size = end - start + 1

    def stream_file():
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = chunk_size
            while remaining > 0:
                read_bytes = min(remaining, 1024 * 1024)
                data = f.read(read_bytes)
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_size),
        "Content-Type": content_type,
    }
    if request.method == "HEAD":
        from fastapi import Response
        return Response(status_code=206, headers=headers)
    return StreamingResponse(stream_file(), status_code=206, headers=headers)

@app.api_route("/video/stream", methods=["GET", "HEAD"])
def get_video_stream(request: Request):
    for name in ["video_context_1080p.mp4", "video_telegram_1080p.mp4", "video_1080p_final.mp4"]:
        p = BASE_DIR / name
        if p.exists():
            return range_requests_response(request, p)
    raise HTTPException(status_code=404, detail="Video not found")

@app.api_route("/subtitles.vtt", methods=["GET", "HEAD"])
def get_vtt():
    p = BASE_DIR / "subtitles.vtt"
    if p.exists():
        return FileResponse(p, media_type="text/vtt", headers={"Cache-Control": "no-store"})
    raise HTTPException(status_code=404, detail="Subtitles not found")

@app.api_route("/subtitles.srt", methods=["GET", "HEAD"])
def get_srt():
    p = BASE_DIR / "context_translated_zh.srt"
    if p.exists():
        # 下載時自動移除內部【字卡】標記前綴
        cleaned = re.sub(r"^【字卡】", "", p.read_text(encoding="utf-8"), flags=re.MULTILINE)
        return PlainTextResponse(cleaned, headers={"Content-Disposition": 'attachment; filename="subtitles.srt"'})
    raise HTTPException(status_code=404, detail="Subtitles not found")

# 硬字幕影片：按需壓製（網頁預覽改用 VTT 字幕軌，不再每次上傳都燒錄）
BURN_STATUS = {"state": "idle", "job": None, "step": ""}

def run_burn(job_id: str):
    try:
        src = BASE_DIR / "video_context_1080p.mp4"
        ass = BASE_DIR / "context_subtitles_1080p.ass"
        if not src.exists() or not ass.exists():
            raise FileNotFoundError("缺少影片或 ASS 字幕檔")
        tmp = BASE_DIR / "video_burned_tmp.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-i", str(src), "-vf", "ass=context_subtitles_1080p.ass",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-c:a", "copy", str(tmp)
        ], cwd=BASE_DIR, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tmp.replace(BASE_DIR / "video_burned_download.mp4")
        BURN_STATUS.update({"state": "done", "job": job_id, "step": "✅ 硬字幕影片已就緒"})
    except Exception as e:
        BURN_STATUS.update({"state": "error", "job": job_id, "step": f"❌ 壓製失敗: {e}"})

@app.post("/api/burn")
def start_burn():
    if BURN_STATUS["state"] == "running":
        raise HTTPException(status_code=409, detail="已有硬字幕壓製進行中")
    if JOB_STATUS["state"] == "running":
        raise HTTPException(status_code=409, detail="影片處理中，請稍候")
    job_id = JOB_STATUS.get("active_id")
    BURN_STATUS.update({"state": "running", "job": job_id, "step": "壓製硬字幕中（約 4 分鐘）..."})
    threading.Thread(target=run_burn, args=(job_id,), daemon=True).start()
    return JSONResponse(BURN_STATUS)

@app.get("/api/burn/status")
def burn_status():
    return JSONResponse({**BURN_STATUS, "active_id": JOB_STATUS.get("active_id")})

@app.get("/download/burned")
def download_burned():
    p = BASE_DIR / "video_burned_download.mp4"
    if p.exists() and BURN_STATUS.get("job") == JOB_STATUS.get("active_id") and BURN_STATUS["state"] == "done":
        return FileResponse(p, media_type="video/mp4", filename="video_with_subtitles.mp4")
    raise HTTPException(status_code=404, detail="尚未壓製目前影片的硬字幕版本")

@app.get("/api/style")
def get_style():
    return JSONResponse(load_style())

@app.post("/api/style")
async def update_style(request: Request):
    body = await request.json()
    allowed = {}
    if "card_color" in body and re.match(r"^#[0-9a-fA-F]{6}$", str(body["card_color"]).strip()):
        allowed["card_color"] = str(body["card_color"]).strip().upper()
    if "card_font" in body and str(body["card_font"]).strip():
        allowed["card_font"] = str(body["card_font"]).strip()
    if "card_size" in body:
        try:
            allowed["card_size"] = max(20, min(72, int(body["card_size"])))
        except Exception:
            pass
    cfg = save_style(allowed)
    active = next((x for x in load_history() if x.get("id") == JOB_STATUS.get("active_id")), {})
    tgt_langs = active.get("tgt_langs") or ["zh-TW"]
    env = {
        **os.environ,
        "TGT_LANGS": ",".join(tgt_langs),
        "CARD_COLOR": cfg["card_color"],
        "CARD_FONT": cfg["card_font"],
        "CARD_SIZE": str(cfg["card_size"]),
    }
    if (BASE_DIR / "context_translated_zh.srt").exists():
        subprocess.run([sys.executable, "generate_context_ass.py"], cwd=BASE_DIR, env=env, check=False)
        build_vtt_and_transcript(BASE_DIR / "context_translated_zh.srt")
        job_id = JOB_STATUS.get("active_id")
        if job_id and (JOBS_DIR / job_id).exists():
            for fname in ["context_subtitles_1080p.ass", "subtitles.vtt", "transcript.json"]:
                _link_or_copy(BASE_DIR / fname, JOBS_DIR / job_id / fname)
    return JSONResponse({"ok": True, "style": cfg})

@app.get("/api/transcript")
def get_transcript():
    p = BASE_DIR / "transcript.json"
    if p.exists():
        return JSONResponse(json.loads(p.read_text(encoding="utf-8")))
    return JSONResponse([])

@app.get("/api/context")
def get_context():
    p = BASE_DIR / "global_context.txt"
    if p.exists():
        return JSONResponse({"context": p.read_text(encoding="utf-8")})
    return JSONResponse({"context": "No context analysis generated yet."})

@app.get("/api/cards")
def get_cards():
    p = BASE_DIR / "ocr_detections.json"
    if p.exists():
        return JSONResponse(json.loads(p.read_text(encoding="utf-8")))
    return JSONResponse([])

@app.get("/api/status")
def get_status():
    active = next((x for x in load_history() if x.get("id") == JOB_STATUS.get("active_id")), {})
    return JSONResponse({**JOB_STATUS, "soft_subs": bool(active.get("soft_subs"))})

@app.get("/api/history")
def get_history():
    items = load_history()
    for it in items:
        it["active"] = (it.get("id") == JOB_STATUS.get("active_id"))
    return JSONResponse(items)

@app.post("/api/history/{job_id}/activate")
def activate_history(job_id: str):
    items = load_history()
    target = next((x for x in items if x.get("id") == job_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="找不到該筆記錄")
    jdir = JOBS_DIR / job_id
    for fname in ARTIFACT_FILES:
        if (jdir / fname).exists():
            _link_or_copy(jdir / fname, BASE_DIR / fname)
    if (BASE_DIR / "video_context_1080p.mp4").exists():
        ensure_h264(BASE_DIR / "video_context_1080p.mp4")
    JOB_STATUS.update({"title": target["title"], "active_id": job_id})
    return JSONResponse({"ok": True, "active_id": job_id, "title": target["title"]})

@app.delete("/api/history/{job_id}")
def delete_history(job_id: str):
    items = [x for x in load_history() if x.get("id") != job_id]
    save_history(items)
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
    return JSONResponse({"ok": True, "items": items})

@app.post("/api/upload")
async def upload_video(
    file: UploadFile = File(...),
    src_langs: str = Form("auto"),
    tgt_langs: str = Form("zh-TW"),
):
    if JOB_STATUS["state"] == "running":
        raise HTTPException(status_code=409, detail="已有影片正在處理中，請稍候")
    s_list = [x.strip() for x in src_langs.split(",") if x.strip()] or ["auto"]
    t_list = [x.strip() for x in tgt_langs.split(",") if x.strip()] or ["zh-TW"]
    safe_name = Path(file.filename or "uploaded.mp4").name
    raw_dest = BASE_DIR / "uploaded_input.mp4"
    with open(raw_dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    ensure_h264(raw_dest)
    JOB_STATUS.update({"state": "running", "step": "已上傳，準備開始處理...", "title": safe_name})
    threading.Thread(target=run_pipeline, args=(raw_dest, safe_name, s_list, t_list), daemon=True).start()
    return JSONResponse({"ok": True, "status": JOB_STATUS})

@app.post("/api/youtube")
async def upload_youtube(request: Request):
    if JOB_STATUS["state"] == "running":
        raise HTTPException(status_code=409, detail="已有影片正在處理中，請稍候")
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(status_code=400, detail="請輸入有效的 http/https 影片連結")
    s_list = body.get("src_langs") or ["auto"]
    t_list = body.get("tgt_langs") or ["zh-TW"]
    JOB_STATUS.update({"state": "running", "step": "準備下載 YouTube 影片...", "title": url})
    threading.Thread(target=run_youtube_pipeline, args=(url, s_list, t_list), daemon=True).start()
    return JSONResponse({"ok": True, "status": JOB_STATUS})

HTML_CONTENT = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI 影片全域上下文翻譯與畫面字卡工作站</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style id="dynamicCardStyle">
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: #1f2937; }
    ::-webkit-scrollbar-thumb { background: #4b5563; border-radius: 3px; }
    .active-cue { background-color: rgba(59, 130, 246, 0.2); border-left: 4px solid #3b82f6; }
    video::cue(c.card) { color: #00FFFF; font-family: "Noto Sans CJK JP", sans-serif; }
  </style>
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen flex flex-col font-sans">
  <!-- Header -->
  <header class="border-b border-gray-800 bg-gray-900/90 backdrop-blur px-6 py-3 flex flex-col gap-2.5 sticky top-0 z-50">
    <div class="flex items-center justify-between flex-wrap gap-3">
      <div class="flex items-center space-x-3">
        <div class="w-9 h-9 rounded-lg bg-gradient-to-tr from-blue-600 to-indigo-500 flex items-center justify-center font-bold text-lg shadow-lg">🎬</div>
        <div>
          <h1 class="font-bold text-lg leading-tight flex items-center gap-2">
            AI 影片上下文翻譯工作站
            <span class="text-xs bg-blue-900 text-blue-300 px-2 py-0.5 rounded font-mono">1080p 60fps 原畫</span>
          </h1>
          <p id="videoSubtitle" class="text-xs text-gray-400">甘橘 (감귤) - 東京池袋日常 Vlog (全域上下文分析 + 畫面字卡 OCR 提取)</p>
        </div>
      </div>
      <div class="flex items-center space-x-2 text-sm flex-wrap gap-y-2">
        <span id="jobBadge" class="hidden text-xs px-2.5 py-1 rounded-full bg-amber-900/60 text-amber-300 border border-amber-700 font-mono"></span>
        <div class="flex items-center bg-gray-800 border border-gray-700 rounded-lg overflow-hidden">
          <input id="ytUrlInput" type="url" placeholder="貼上 YouTube 連結..." onkeydown="if(event.key==='Enter')submitYoutubeUrl()" class="bg-transparent text-xs px-3 py-1.5 text-gray-200 focus:outline-none w-48 sm:w-64">
          <button onclick="submitYoutubeUrl()" class="px-3 py-1.5 bg-red-600 hover:bg-red-500 text-white text-xs font-medium transition whitespace-nowrap">🔗 載入 YT</button>
        </div>
        <input type="file" id="uploadInput" accept="video/*" class="hidden" onchange="uploadNewVideo(this)">
        <button onclick="document.getElementById('uploadInput').click()" class="px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium transition flex items-center gap-1.5 shadow">
          <span>📤 上傳檔案</span>
        </button>
        <button onclick="switchTab('history')" class="px-3 py-1.5 rounded-lg bg-indigo-900/70 hover:bg-indigo-800 text-indigo-200 border border-indigo-700 transition flex items-center gap-1">
          <span>🗂️ 記錄管理</span>
        </button>
        <button onclick="burnAndDownload()" class="px-3 py-1.5 rounded-lg bg-amber-800/70 hover:bg-amber-700 text-amber-100 border border-amber-700 transition flex items-center gap-1" title="預覽使用字幕軌；需要成品時再壓製硬字幕（約 4 分鐘）"><span>🔥 硬字幕影片</span></button>
        <a href="/subtitles.srt" class="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-200 border border-gray-700 transition flex items-center gap-1">
          <span>📥 SRT</span>
        </a>
        <a href="/subtitles.vtt" class="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-200 border border-gray-700 transition flex items-center gap-1">
          <span>📥 VTT</span>
        </a>
      </div>
    </div>
    <!-- Multi-select Language & Card Style Bar -->
    <div class="flex flex-wrap items-center justify-between gap-x-6 gap-y-1.5 text-xs border-t border-gray-800/80 pt-2 text-gray-300">
      <div class="flex items-center gap-2 flex-wrap">
        <span class="text-gray-400 font-medium">🎙️ 影片語言（可複選）：</span>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-blue-950/70 px-2 py-0.5 rounded border border-blue-600 text-blue-200"><input type="checkbox" name="srcLang" value="auto" checked class="accent-blue-500"> 🌐 自動多語偵測 (Auto)</label>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="srcLang" value="ko" class="accent-blue-500"> 韓文 (ko)</label>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="srcLang" value="ja" class="accent-blue-500"> 日文 (ja)</label>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="srcLang" value="en" class="accent-blue-500"> 英文 (en)</label>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="srcLang" value="zh" class="accent-blue-500"> 中文 (zh)</label>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="srcLang" value="pt" class="accent-blue-500"> 葡文 (pt)</label>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="srcLang" value="de" class="accent-blue-500"> 德文 (de)</label>
      </div>
      <div class="flex items-center gap-2 flex-wrap">
        <span class="text-gray-400 font-medium">💬 字幕語言（可複選）：</span>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="tgtLang" value="zh-TW" checked class="accent-emerald-500"> 繁體中文</label>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="tgtLang" value="en" class="accent-emerald-500"> English</label>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="tgtLang" value="ja" class="accent-emerald-500"> 日本語</label>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="tgtLang" value="ko" class="accent-emerald-500"> 한국어</label>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="tgtLang" value="pt" class="accent-emerald-500"> Português</label>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="tgtLang" value="de" class="accent-emerald-500"> Deutsch</label>
        <label class="inline-flex items-center gap-1 cursor-pointer bg-gray-800/80 px-2 py-0.5 rounded border border-gray-700"><input type="checkbox" name="tgtLang" value="orig" class="accent-emerald-500"> 附帶原文</label>
      </div>
      <!-- Card Style Customization Control -->
      <div class="flex items-center gap-2 flex-wrap bg-gray-800/60 px-2.5 py-1 rounded-lg border border-gray-700/80">
        <span class="text-cyan-300 font-medium">🎨 字卡外觀：</span>
        <label class="inline-flex items-center gap-1">
          <span class="text-gray-400">顏色</span>
          <input type="color" id="cardColorInput" value="#00FFFF" onchange="applyCardStyle()" class="w-5 h-5 rounded cursor-pointer bg-transparent border-0">
        </label>
        <label class="inline-flex items-center gap-1">
          <span class="text-gray-400">字型</span>
          <select id="cardFontSelect" onchange="applyCardStyle()" class="bg-gray-900 border border-gray-700 rounded px-1.5 py-0.5 text-xs text-gray-200">
            <option value="Noto Sans CJK JP">Noto Sans CJK JP (日文)</option>
            <option value="Noto Sans CJK TC">Noto Sans CJK TC (繁中)</option>
            <option value="Noto Sans CJK KR">Noto Sans CJK KR (韓文)</option>
            <option value="Noto Sans">Noto Sans (歐美拉丁)</option>
          </select>
        </label>
        <label class="inline-flex items-center gap-1">
          <span class="text-gray-400">大小</span>
          <input type="number" id="cardSizeInput" value="38" min="20" max="72" onchange="applyCardStyle()" class="w-12 bg-gray-900 border border-gray-700 rounded px-1 py-0.5 text-xs text-center text-gray-200">
        </label>
      </div>
    </div>
  </header>

  <!-- Main Grid -->
  <main class="flex-1 grid grid-cols-1 lg:grid-cols-12 gap-6 p-6 max-w-[1800px] mx-auto w-full">
    <!-- Left: Video Player -->
    <div class="lg:col-span-8 flex flex-col space-y-4">
      <div id="videoContainer" class="bg-black rounded-2xl overflow-hidden shadow-2xl border border-gray-800 relative group aspect-video flex items-center justify-center">
        <video id="videoPlayer" class="w-full h-full object-contain" controls preload="metadata" playsinline webkit-playsinline>
          <source id="videoSource" src="/video/stream" type="video/mp4">
        </video>
        <!-- iPad Brave / WebKit DOM Subtitle Overlay -->
        <div id="customSubOverlay" class="absolute pointer-events-none inset-0 overflow-hidden" style="display: block;">
          <!-- Spoken dialogue (bottom center) -->
          <div id="dialogueBox" class="absolute bottom-6 left-1/2 -translate-x-1/2 text-center px-4 py-1.5 rounded-lg max-w-[90%] transition-opacity duration-150" style="display: none; background: rgba(0,0,0,0.65); text-shadow: 0 1px 3px rgba(0,0,0,0.9);">
            <div id="dialogueText" class="text-white font-medium text-base sm:text-lg leading-snug whitespace-pre-line"></div>
          </div>
          <!-- Floating card (positioned at card_x, card_y) -->
          <div id="cardBox" class="absolute -translate-x-1/2 -translate-y-1/2 px-3 py-1 rounded bg-black/60 shadow-lg border border-gray-800/60 font-semibold transition-all duration-150" style="display: none;"></div>
        </div>
      </div>

      <!-- Video Control Tabs -->
      <div class="bg-gray-900 rounded-xl p-4 border border-gray-800">
        <div class="flex border-b border-gray-800 pb-3 gap-6 text-sm font-medium flex-wrap">
          <button id="tabBtnContext" onclick="switchTab('context')" class="text-blue-400 border-b-2 border-blue-500 pb-1">🧠 全域情境與人設分析</button>
          <button id="tabBtnHistory" onclick="switchTab('history')" class="text-gray-400 hover:text-gray-200 pb-1">🗂️ 上傳記錄管理</button>
          <button id="tabBtnCards" onclick="switchTab('cards')" class="text-gray-400 hover:text-gray-200 pb-1">🔍 畫面擷取字卡</button>
          <button id="tabBtnInfo" onclick="switchTab('info')" class="text-gray-400 hover:text-gray-200 pb-1">ℹ️ 處理架構說明</button>
        </div>

        <!-- Tab 1: Global Context -->
        <div id="tabContentContext" class="mt-4 text-sm text-gray-200 leading-relaxed whitespace-pre-wrap break-words font-sans">
          載入全域情境分析中...
        </div>

        <!-- Tab History: Upload History Management -->
        <div id="tabContentHistory" class="mt-4 hidden space-y-2">
          <div id="historyList" class="space-y-2 text-xs"></div>
        </div>

        <!-- Tab 2: OCR Cards -->
        <div id="tabContentCards" class="mt-4 hidden max-h-96 overflow-y-auto">
          <div id="cardsList" class="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs"></div>
        </div>

        <!-- Tab 3: Info -->
        <div id="tabContentInfo" class="mt-4 hidden text-xs text-gray-400 space-y-2">
          <p>• <strong>兩階段通讀與字卡去噪</strong>：Stage 1 由 Qwen3.8-27B 通讀全片語音轉錄與 1080p 畫面字卡，自動校正特效字殘缺。Stage 2 依全域知識庫逐句意譯。</p>
          <p>• <strong>字卡空間就近定位（無冗餘前綴）</strong>：根據 OCR 偵測座標自動將譯文定位在原字卡附近（預設青色 `#00FFFF`，支援自訂顏色、字型與字級），不加「【字卡】」標記。</p>
          <p>• <strong>並行流水線與秒開預覽</strong>：Whisper 與 OCR 平行執行，預覽直接掛載 VTT 軟字幕軌，需要硬字幕成品時再按需壓製。</p>
        </div>
      </div>
    </div>

    <!-- Right: Synchronized Transcript Sidebar -->
    <div class="lg:col-span-4 bg-gray-900 border border-gray-800 rounded-2xl flex flex-col h-[calc(100vh-140px)] sticky top-24 overflow-hidden">
      <div class="p-4 border-b border-gray-800 flex items-center justify-between">
        <div>
          <h2 class="font-bold text-sm text-gray-200">即時同步字幕與字卡</h2>
          <p class="text-xs text-gray-400">點擊任意句子即可跳轉播放</p>
        </div>
        <div class="flex items-center gap-2">
          <input id="searchInput" type="text" placeholder="搜尋內容..." class="bg-gray-800 border border-gray-700 text-xs px-2.5 py-1 rounded-md text-gray-200 focus:outline-none focus:border-blue-500 w-32">
        </div>
      </div>

      <!-- Transcript Item List -->
      <div id="transcriptList" class="flex-1 overflow-y-auto p-3 space-y-2 text-sm divide-y divide-gray-800/40">
        <div class="text-center py-10 text-gray-500 text-xs">載入字幕中...</div>
      </div>
    </div>
  </main>

  <script>
    const video = document.getElementById('videoPlayer');
    const transcriptList = document.getElementById('transcriptList');
    let cues = [];
    let currentStyle = { card_color: '#00FFFF', card_font: 'Noto Sans CJK JP', card_size: 38 };

    async function loadStyle() {
      const res = await fetch('/api/style');
      if (res.ok) {
        currentStyle = await res.json();
        document.getElementById('cardColorInput').value = currentStyle.card_color || '#00FFFF';
        document.getElementById('cardFontSelect').value = currentStyle.card_font || 'Noto Sans CJK JP';
        document.getElementById('cardSizeInput').value = currentStyle.card_size || 38;
        injectCueStyle();
      }
    }

    function injectCueStyle() {
      const styleEl = document.getElementById('dynamicCardStyle');
      styleEl.innerHTML = `
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: #1f2937; }
        ::-webkit-scrollbar-thumb { background: #4b5563; border-radius: 3px; }
        .active-cue { background-color: rgba(59, 130, 246, 0.2); border-left: 4px solid #3b82f6; }
        video::cue(c.card) { color: ${currentStyle.card_color}; font-family: "${currentStyle.card_font}", sans-serif; }
      `;
    }

    async function applyCardStyle() {
      const card_color = document.getElementById('cardColorInput').value;
      const card_font = document.getElementById('cardFontSelect').value;
      const card_size = parseInt(document.getElementById('cardSizeInput').value || '38', 10);
      const res = await fetch('/api/style', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ card_color, card_font, card_size })
      });
      if (res.ok) {
        const data = await res.json();
        currentStyle = data.style;
        injectCueStyle();
        renderTranscript(cues);
        refreshTrack();
        const cardBox = document.getElementById('cardBox');
        if (cardBox && cardBox.style.display !== 'none') {
          cardBox.style.color = currentStyle.card_color || '#00FFFF';
          cardBox.style.fontFamily = `"${currentStyle.card_font || 'Noto Sans CJK JP'}", sans-serif`;
          const baseSize = currentStyle.card_size || 38;
          const overlay = document.getElementById('customSubOverlay');
          const scaledSize = Math.max(14, Math.min(48, Math.round(baseSize * ((overlay ? overlay.clientHeight : 1080) / 1080.0))));
          cardBox.style.fontSize = `${scaledSize}px`;
        }
      }
    }

    function updateOverlayGeometry() {
      const v = document.getElementById('videoPlayer');
      const overlay = document.getElementById('customSubOverlay');
      if (!v || !overlay) return;
      const cW = v.clientWidth;
      const cH = v.clientHeight;
      const vW = v.videoWidth || 1920;
      const vH = v.videoHeight || 1080;
      if (!cW || !cH) return;
      const videoAspect = vW / vH;
      const containerAspect = cW / cH;
      let rW, rH, rX, rY;
      if (containerAspect > videoAspect) {
        rH = cH;
        rW = cH * videoAspect;
        rX = (cW - rW) / 2;
        rY = 0;
      } else {
        rW = cW;
        rH = cW / videoAspect;
        rX = 0;
        rY = (cH - rH) / 2;
      }
      overlay.style.position = 'absolute';
      overlay.style.left = `${Math.round(rX)}px`;
      overlay.style.top = `${Math.round(rY)}px`;
      overlay.style.width = `${Math.round(rW)}px`;
      overlay.style.height = `${Math.round(rH)}px`;
    }

    async function loadTranscript() {
      const res = await fetch('/api/transcript');
      cues = await res.json();
      renderTranscript(cues);
    }

    function renderTranscript(items) {
      transcriptList.innerHTML = items.map((item, idx) => {
        const hasCard = !!item.card;
        const posLabel = hasCard && item.card_x ? `(${item.card_x}, ${item.card_y})` : '';
        return `
          <div id="cue-${idx}" onclick="seekTo(${item.start})" class="cue-item p-2.5 rounded-lg cursor-pointer hover:bg-gray-800 transition text-xs flex flex-col gap-1">
            <div class="flex items-center justify-between text-gray-400 font-mono text-[11px]">
              <span>#${item.id} · ${formatTime(item.start)}</span>
              ${hasCard ? `<span class="bg-cyan-950 text-cyan-300 text-[10px] px-1.5 py-0.2 rounded font-mono border border-cyan-800/60">定位 ${posLabel}</span>` : ''}
            </div>
            <div class="text-gray-100 font-medium text-sm leading-snug whitespace-pre-line">${item.dialogue}</div>
            ${hasCard ? `<div style="color:${currentStyle.card_color}; font-family:'${currentStyle.card_font}',sans-serif;" class="font-medium text-xs bg-gray-950/70 p-1.5 rounded border border-gray-800 mt-1">${item.card}</div>` : ''}
          </div>
        `;
      }).join('');
    }

    function formatTime(sec) {
      const m = Math.floor(sec / 60);
      const s = Math.floor(sec % 60);
      return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }

    async function refreshTrack() {
      const st = await (await fetch('/api/status')).json();
      video.querySelectorAll('track').forEach(t => t.remove());
      if (!st.soft_subs) return;
      const t = document.createElement('track');
      t.kind = 'subtitles'; t.label = '字幕'; t.default = true;
      t.src = '/subtitles.vtt?t=' + Date.now();
      video.appendChild(t);
      t.addEventListener('load', () => {
        if (video.textTracks && video.textTracks[0]) video.textTracks[0].mode = 'hidden';
      });
      if (video.textTracks && video.textTracks[0]) video.textTracks[0].mode = 'hidden';
    }

    async function burnAndDownload() {
      const badge = document.getElementById('jobBadge');
      badge.classList.remove('hidden');
      const r = await fetch('/api/burn', { method: 'POST' });
      if (!r.ok && r.status !== 409) { badge.innerText = '❌ ' + ((await r.json()).detail || '壓製失敗'); return; }
      const poll = async () => {
        const b = await (await fetch('/api/burn/status')).json();
        badge.innerText = b.state === 'running' ? '🔥 ' + b.step : b.step;
        if (b.state === 'running') return setTimeout(poll, 3000);
        if (b.state === 'done') window.location.href = '/download/burned';
      };
      poll();
    }

    function seekTo(sec) {
      video.currentTime = sec;
      video.play();
    }

    video.addEventListener('timeupdate', () => {
      const cur = video.currentTime;
      let activeIdx = -1;
      let activeCue = null;
      for (let i = 0; i < cues.length; i++) {
        if (cur >= cues[i].start && cur <= cues[i].end) {
          activeIdx = i;
          activeCue = cues[i];
          break;
        }
      }

      document.querySelectorAll('.cue-item').forEach(el => el.classList.remove('active-cue'));
      if (activeIdx !== -1) {
        const el = document.getElementById(`cue-${activeIdx}`);
        if (el) {
          el.classList.add('active-cue');
          el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
      }

      // DOM Subtitle Overlay
      const overlay = document.getElementById('customSubOverlay');
      const dlgBox = document.getElementById('dialogueBox');
      const dlgText = document.getElementById('dialogueText');
      const cardBox = document.getElementById('cardBox');

      if (!activeCue) {
        if (dlgBox) dlgBox.style.display = 'none';
        if (cardBox) cardBox.style.display = 'none';
      } else {
        updateOverlayGeometry();

        const hasCard = !!(activeCue.card && activeCue.card.trim());
        const hasDlg = !!(activeCue.dialogue && activeCue.dialogue.trim() && (!hasCard || activeCue.dialogue.trim() !== activeCue.card.trim()));

        // 1. Spoken Dialogue at bottom center
        if (hasDlg && dlgBox && dlgText) {
          dlgText.innerText = activeCue.dialogue;
          dlgBox.style.display = 'block';
        } else if (dlgBox) {
          dlgBox.style.display = 'none';
        }

        // 2. Floating Card
        if (hasCard && cardBox) {
          cardBox.innerText = activeCue.card;
          const pctX = activeCue.card_x ? Math.max(10, Math.min(90, (activeCue.card_x / 1920.0) * 100)) : 50;
          const pctY = activeCue.card_y ? Math.max(8, Math.min(88, (Math.max(60, activeCue.card_y - 45) / 1080.0) * 100)) : 75;
          cardBox.style.left = `${pctX}%`;
          cardBox.style.top = `${pctY}%`;
          cardBox.style.color = currentStyle.card_color || '#00FFFF';
          cardBox.style.fontFamily = `"${currentStyle.card_font || 'Noto Sans CJK JP'}", sans-serif`;
          const baseSize = currentStyle.card_size || 38;
          const scaledSize = Math.max(14, Math.min(48, Math.round(baseSize * ((overlay ? overlay.clientHeight : 1080) / 1080.0))));
          cardBox.style.fontSize = `${scaledSize}px`;
          cardBox.style.display = 'block';
        } else if (cardBox) {
          cardBox.style.display = 'none';
        }
      }
    });

    video.addEventListener('loadedmetadata', updateOverlayGeometry);
    video.addEventListener('play', updateOverlayGeometry);
    window.addEventListener('resize', updateOverlayGeometry);

    document.getElementById('searchInput').addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase();
      const filtered = cues.filter(c => 
        c.dialogue.toLowerCase().includes(q) || (c.card && c.card.toLowerCase().includes(q))
      );
      renderTranscript(filtered);
    });

    function switchTab(name) {
      document.getElementById('tabContentContext').classList.toggle('hidden', name !== 'context');
      document.getElementById('tabContentHistory').classList.toggle('hidden', name !== 'history');
      document.getElementById('tabContentCards').classList.toggle('hidden', name !== 'cards');
      document.getElementById('tabContentInfo').classList.toggle('hidden', name !== 'info');

      const tabs = ['Context', 'History', 'Cards', 'Info'];
      tabs.forEach(t => {
        const btn = document.getElementById(`tabBtn${t}`);
        const isActive = t.toLowerCase() === name;
        btn.className = isActive 
          ? "text-blue-400 border-b-2 border-blue-500 pb-1"
          : "text-gray-400 hover:text-gray-200 pb-1";
      });
      if (name === 'history') loadHistory();
    }

    function getSelectedLangs(name, fallback) {
      const vals = Array.from(document.querySelectorAll(`input[name="${name}"]:checked`)).map(el => el.value);
      return vals.length ? vals : fallback;
    }

    async function loadHistory() {
      const res = await fetch('/api/history');
      const items = await res.json();
      const el = document.getElementById('historyList');
      if (!items.length) {
        el.innerHTML = '<div class="text-gray-500 py-4 text-center">尚無上傳記錄</div>';
        return;
      }
      el.innerHTML = items.map(it => `
        <div class="flex items-center justify-between p-3 rounded-lg border ${it.active ? 'bg-blue-950/40 border-blue-600' : 'bg-gray-800/70 border-gray-700'} gap-3">
          <div class="min-w-0 flex-1">
            <div class="font-medium text-sm text-gray-100 truncate flex items-center gap-2">
              ${it.active ? '<span class="text-[10px] bg-blue-600 text-white px-1.5 py-0.5 rounded">播放中</span>' : ''}
              <span>${it.title}</span>
            </div>
            <div class="text-[11px] text-gray-400 mt-1 flex flex-wrap gap-2 font-mono">
              <span>🕒 ${it.created_at || ''}</span>
              <span>🎙️ 來源: ${(it.src_langs || ['ko']).join(', ')}</span>
              <span>💬 字幕: ${(it.tgt_langs || ['zh-TW']).join(', ')}</span>
            </div>
          </div>
          <div class="flex items-center gap-2 shrink-0">
            ${!it.active ? `<button onclick="activateJob('${it.id}')" class="px-2.5 py-1 rounded bg-blue-600 hover:bg-blue-500 text-white text-xs">▶️ 切換播放</button>` : ''}
            <button onclick="deleteJob('${it.id}')" class="px-2.5 py-1 rounded bg-red-900/60 hover:bg-red-700 text-red-200 border border-red-800 text-xs">🗑️ 刪除</button>
          </div>
        </div>
      `).join('');
    }

    async function activateJob(jobId) {
      const res = await fetch(`/api/history/${jobId}/activate`, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        document.getElementById('videoSubtitle').innerText = `${data.title} (全域上下文分析 + 畫面字卡 OCR 提取)`;
        video.src = '/video/stream?t=' + Date.now();
        video.load();
        refreshTrack();
        loadTranscript();
        loadMeta();
        loadHistory();
      }
    }

    async function deleteJob(jobId) {
      const res = await fetch(`/api/history/${jobId}`, { method: 'DELETE' });
      if (res.ok) loadHistory();
    }

    async function loadMeta() {
      const cRes = await fetch('/api/context');
      const cData = await cRes.json();
      document.getElementById('tabContentContext').innerText = cData.context;

      const cardRes = await fetch('/api/cards');
      const cards = await cardRes.json();
      document.getElementById('tabBtnCards').innerText = `🔍 畫面擷取字卡 (${cards.length}處)`;
      const listEl = document.getElementById('cardsList');
      listEl.innerHTML = cards.map(c => `
        <div onclick="seekTo(${c.sec})" class="p-2 bg-gray-800/80 rounded border border-gray-700/60 cursor-pointer hover:border-blue-500 transition">
          <div class="flex justify-between text-[10px] text-blue-400 font-mono mb-0.5">
            <span>${formatTime(c.sec)} (${c.sec}s)</span>
            ${c.x ? `<span>pos(${c.x}, ${c.y})</span>` : ''}
          </div>
          <div class="text-gray-200 font-medium">${c.text}</div>
        </div>
      `).join('');
      loadHistory();
    }

    async function submitYoutubeUrl() {
      const input = document.getElementById('ytUrlInput');
      const url = input.value.trim();
      if (!url) return;
      const src_langs = getSelectedLangs('srcLang', ['auto']);
      const tgt_langs = getSelectedLangs('tgtLang', ['zh-TW']);
      const badge = document.getElementById('jobBadge');
      badge.classList.remove('hidden');
      badge.innerText = '⏳ 送出 YouTube 連結...';
      const res = await fetch('/api/youtube', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, src_langs, tgt_langs })
      });
      if (!res.ok) {
        const err = await res.json();
        badge.innerText = `❌ ${err.detail || '送出失敗'}`;
        return;
      }
      input.value = '';
      pollStatus();
    }

    async function uploadNewVideo(input) {
      const file = input.files[0];
      if (!file) return;
      const src_langs = getSelectedLangs('srcLang', ['auto']);
      const tgt_langs = getSelectedLangs('tgtLang', ['zh-TW']);
      const badge = document.getElementById('jobBadge');
      badge.classList.remove('hidden');
      badge.innerText = `⏫ 上傳中 (${(file.size/1048576).toFixed(1)} MB)...`;
      const fd = new FormData();
      fd.append('file', file);
      fd.append('src_langs', src_langs.join(','));
      fd.append('tgt_langs', tgt_langs.join(','));
      const res = await fetch('/api/upload', { method: 'POST', body: fd });
      if (!res.ok) {
        const err = await res.json();
        badge.innerText = `❌ ${err.detail || '上傳失敗'}`;
        return;
      }
      pollStatus();
    }

    async function pollStatus() {
      const badge = document.getElementById('jobBadge');
      const res = await fetch('/api/status');
      const st = await res.json();
      if (st.title) document.getElementById('videoSubtitle').innerText = `${st.title} (全域上下文分析 + 畫面字卡 OCR 提取)`;
      if (st.state === 'running') {
        badge.classList.remove('hidden');
        badge.innerText = `⏳ ${st.step}`;
        setTimeout(pollStatus, 2500);
      } else if (st.state === 'done') {
        badge.classList.remove('hidden');
        badge.innerText = st.step;
        video.src = '/video/stream?t=' + Date.now();
        video.load();
        refreshTrack();
        loadTranscript();
        loadMeta();
      } else if (st.state === 'error') {
        badge.classList.remove('hidden');
        badge.innerText = st.step;
      }
    }

    loadStyle().then(() => {
      loadTranscript();
      loadMeta();
      refreshTrack();
      pollStatus();
    });
  </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_CONTENT

if __name__ == "__main__":
    uvicorn.run("webui_server:app", host="0.0.0.0", port=8501, reload=True)
