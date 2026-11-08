#!/usr/bin/env python3
"""
Deterministic Acceptance Verification Gate (qa_verify.py)
Verifies all 5 engineering gates for AI Video Context Subtitle Workstation:
  Gate 1: Syntax & Compilation of all core modules
  Gate 2: Data Isolation (_link_or_copy copies text artifacts, no shared inode mutation)
  Gate 3: 1080p Spatial OCR & Coordinate Extraction (fast_ocr_keyframes.ocr_worker)
  Gate 4: Card Spatial Positioning, Prefix Removal ("【字卡】"), & Custom Color/Font/Size in ASS + VTT + Transcript
  Gate 5: Multi-Language Auto-LID (detect_multi_langs) & Live WebUI Endpoints (/api/style, /subtitles.srt, UI controls)
Exit code: 0 = ALL PASS, 1 = FAIL
"""
import os
import sys
import json
import shutil
import tempfile
import py_compile
import urllib.request

os.environ["CUDA_VISIBLE_DEVICES"] = ""

WORK_DIR = "/home/min20120907/Documents/ai_videos_translator"
sys.path.insert(0, WORK_DIR)


def assert_true(cond, msg):
    if not cond:
        print(f"[FAIL] {msg}")
        sys.exit(1)
    print(f"  [PASS] {msg}")


def gate_1_compile():
    print("=== Gate 1: Syntax & Module Compilation ===")
    files = [
        "webui_server.py",
        "agentic_full_translate.py",
        "fast_ocr_keyframes.py",
        "generate_context_ass.py",
    ]
    for f in files:
        path = os.path.join(WORK_DIR, f)
        py_compile.compile(path, doraise=True)
        assert_true(os.path.exists(path), f"{f} compiled cleanly")


def gate_2_data_isolation():
    print("=== Gate 2: Historical Job Data Isolation ===")
    from pathlib import Path
    import webui_server
    with tempfile.TemporaryDirectory() as tmpdir:
        src_srt = Path(tmpdir) / "src.srt"
        dst_srt = Path(tmpdir) / "dst.srt"
        src_srt.write_text("original history content", encoding="utf-8")
        webui_server._link_or_copy(src_srt, dst_srt)
        src_ino = os.stat(src_srt).st_ino
        dst_ino = os.stat(dst_srt).st_ino
        assert_true(src_ino != dst_ino, f"Text artifact copied with distinct inode ({src_ino} != {dst_ino})")
        src_srt.write_text("overwritten workspace content", encoding="utf-8")
        preserved = dst_srt.read_text(encoding="utf-8")
        assert_true(preserved == "original history content", "Historical job SRT preserved after workspace overwrite")


def gate_3_spatial_ocr():
    print("=== Gate 3: 1080p Spatial OCR & Coordinate Extraction ===")
    import fast_ocr_keyframes
    probe_img = "/home/min20120907/.gemini/antigravity-cli/brain/fda18124-3b8b-42a0-8610-c20c135586e0/scratch/probe/ikebukuro_vlog.jpg"
    if not os.path.exists(probe_img):
        # Extract from video if scratch image was cleaned
        video_path = os.path.join(WORK_DIR, "jobs/ikebukuro_vlog/video_context_1080p.mp4")
        os.makedirs(os.path.dirname(probe_img), exist_ok=True)
        os.system(f"ffmpeg -y -ss 45 -i '{video_path}' -frames:v 1 -q:v 2 '{probe_img}' >/dev/null 2>&1")
    assert_true(os.path.exists(probe_img), f"Probe 1080p frame exists: {probe_img}")
    os.environ["SRC_LANGS"] = "ko,en"
    res = fast_ocr_keyframes.ocr_worker([(probe_img, 45)])
    assert_true(len(res) == 1, f"OCR worker returned 1 frame detection (got {len(res)})")
    det = res[0]
    assert_true("text" in det and "x" in det and "y" in det, f"Detection contains text, x, y keys: {det}")
    assert_true(120 <= det["x"] <= 1800 and 100 <= det["y"] <= 1020, f"Coordinates within 1080p safe bounds: ({det['x']}, {det['y']})")
    assert_true(len(det["text"]) >= 4, f"Paragraph-grouped OCR text non-empty: {det['text']}")


def gate_4_card_positioning_and_styling():
    print("=== Gate 4: Card Positioning, Prefix Removal & Custom Styling (ASS + VTT + Transcript) ===")
    import runpy
    import webui_server

    # Backup existing workspace files
    backups = {}
    target_files = [
        "clean_orig.srt",
        "context_translated_zh.srt",
        "ocr_detections.json",
        "style_config.json",
        "context_subtitles_1080p.ass",
        "subtitles.vtt",
        "transcript.json",
    ]
    with tempfile.TemporaryDirectory() as bk_dir:
        for tf in target_files:
            p = os.path.join(WORK_DIR, tf)
            if os.path.exists(p):
                bk = os.path.join(bk_dir, tf)
                shutil.copy2(p, bk)
                backups[tf] = bk

        try:
            with open(os.path.join(WORK_DIR, "clean_orig.srt"), "w", encoding="utf-8") as f:
                f.write("1\n00:00:10,000 --> 00:00:13,000\n안녕하세요\n\n2\n00:00:44,000 --> 00:00:46,000\n【字卡】이케부쿠로 산책\n\n")
            with open(os.path.join(WORK_DIR, "context_translated_zh.srt"), "w", encoding="utf-8") as f:
                f.write("1\n00:00:10,000 --> 00:00:13,000\n大家好\n\n2\n00:00:44,000 --> 00:00:46,000\n【字卡】池袋散步\n\n")
            with open(os.path.join(WORK_DIR, "ocr_detections.json"), "w", encoding="utf-8") as f:
                json.dump([{"sec": 45, "text": "이케부쿠로 산책", "x": 640, "y": 820}], f, ensure_ascii=False)
            with open(os.path.join(WORK_DIR, "style_config.json"), "w", encoding="utf-8") as f:
                json.dump({"card_color": "#00FFFF", "card_font": "Noto Sans CJK TC", "card_size": 42}, f)

            # Test ASS generation
            os.environ["TGT_LANGS"] = "zh-TW,orig"
            runpy.run_path(os.path.join(WORK_DIR, "generate_context_ass.py"))
            ass_path = os.path.join(WORK_DIR, "context_subtitles_1080p.ass")
            with open(ass_path, "r", encoding="utf-8") as f:
                ass_content = f.read()

            assert_true("Style: CardFloat,Noto Sans CJK TC,42,&H00FFFF00" in ass_content,
                        "ASS CardFloat style uses Cyan BGR (&H00FFFF00), custom font, and size 42")
            assert_true("{\\pos(640,775)}池袋散步" in ass_content,
                        "ASS CardFloat event positioned at {\\pos(640,775)} near card (640, 820)")
            assert_true("【字卡】" not in ass_content,
                        "Zero occurrences of '【字卡】' prefix in generated ASS file")

            # Test VTT and Transcript generation
            from pathlib import Path
            webui_server.build_vtt_and_transcript(Path(WORK_DIR) / "context_translated_zh.srt")
            with open(os.path.join(WORK_DIR, "subtitles.vtt"), "r", encoding="utf-8") as f:
                vtt_content = f.read()
            with open(os.path.join(WORK_DIR, "transcript.json"), "r", encoding="utf-8") as f:
                tr_data = json.load(f)

            assert_true("color: #00FFFF;" in vtt_content and "font-family: 'Noto Sans CJK TC'" in vtt_content,
                        "VTT STYLE block injects custom card color (#00FFFF) and font family")
            assert_true("line:72% position:33% align:center" in vtt_content,
                        "VTT card cue includes spatial line:72% position:33% coordinates")
            assert_true("<c.card>池袋散步</c>" in vtt_content,
                        "VTT card cue uses <c.card> tag without '【字卡】' prefix")
            assert_true("【字卡】" not in vtt_content,
                        "Zero occurrences of '【字卡】' prefix in subtitles.vtt")
            assert_true(all("【字卡】" not in (item.get("dialogue") or "") and "【字卡】" not in (item.get("card") or "") for item in tr_data),
                        "Zero occurrences of '【字卡】' prefix in transcript.json")
            assert_true(tr_data[1].get("card") == "池袋散步" and tr_data[1].get("card_x") == 640 and tr_data[1].get("card_y") == 820,
                        "transcript.json preserves card='池袋散步' and (card_x=640, card_y=820) metadata")
        finally:
            for tf, bk in backups.items():
                shutil.copy2(bk, os.path.join(WORK_DIR, tf))


def gate_5_auto_lid_and_live_endpoints():
    print("=== Gate 5: Multi-Language Auto-Detection & Live WebUI API Verification ===")
    from pathlib import Path
    # 5a. Check live endpoints on port 8501
    with urllib.request.urlopen("http://localhost:8501/api/style", timeout=5) as resp:
        style_resp = json.loads(resp.read().decode("utf-8"))
    assert_true("card_color" in style_resp and "card_font" in style_resp and "card_size" in style_resp,
                f"GET /api/style returns valid style config: {style_resp}")

    # Test POST /api/style roundtrip
    req = urllib.request.Request(
        "http://localhost:8501/api/style",
        data=json.dumps({"card_color": "#00FFFF", "card_font": "Noto Sans CJK TC", "card_size": 38}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        post_resp = json.loads(resp.read().decode("utf-8"))
    assert_true(post_resp.get("ok") is True and post_resp["style"]["card_color"] == "#00FFFF",
                "POST /api/style updates and persists card style configuration")

    with urllib.request.urlopen("http://localhost:8501/", timeout=5) as resp:
        html = resp.read().decode("utf-8")
    for token in ['value="auto"', 'value="pt"', 'value="de"', 'id="cardColorInput"', 'id="cardFontSelect"', 'id="cardSizeInput"']:
        assert_true(token in html, f"WebUI HTML contains control '{token}'")

    # 5b. Check multi-point Auto-LID function on real video
    import webui_server
    from faster_whisper import WhisperModel
    video_path = Path(WORK_DIR) / "jobs/ikebukuro_vlog/video_context_1080p.mp4"
    if video_path.exists():
        model = WhisperModel("tiny", device="cpu", compute_type="int8", cpu_threads=8)
        detected = webui_server.detect_multi_langs(video_path, model)
        assert_true(isinstance(detected, list) and "ko" in detected,
                    f"detect_multi_langs accurately identified active languages: {detected}")


if __name__ == "__main__":
    gate_1_compile()
    gate_2_data_isolation()
    gate_3_spatial_ocr()
    gate_4_card_positioning_and_styling()
    gate_5_auto_lid_and_live_endpoints()
    print("\nALL PASS")
    sys.exit(0)
