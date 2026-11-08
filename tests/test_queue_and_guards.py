import os
import sys
import re
import json
import time
import copy
import hashlib
import tempfile
import shutil
import subprocess
import threading
from pathlib import Path

# Ensure CUDA is disabled
os.environ["CUDA_VISIBLE_DEVICES"] = ""

REAL_WORK_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REAL_WORK_DIR))

try:
    import pytest
except ImportError:
    pytest = None
from fastapi.testclient import TestClient
import webui_server

# Real constants for restoration
REAL_BASE_DIR = webui_server.BASE_DIR
REAL_JOBS_DIR = webui_server.JOBS_DIR
REAL_HISTORY_FILE = webui_server.HISTORY_FILE
REAL_STYLE_FILE = webui_server.STYLE_FILE


def get_real_workspace_snapshot():
    """Calculate sha256 of all sensitive workspace files and all files in jobs/."""
    snapshot = {}
    target_files = [
        "context_subtitles_1080p.ass",
        "subtitles.vtt",
        "transcript.json",
        "context_translated_zh.srt",
        "history.json",
        "style_config.json",
    ]
    for tf in target_files:
        p = REAL_WORK_DIR / tf
        if p.exists() and p.is_file():
            snapshot[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            snapshot[str(p)] = None

    jobs_dir = REAL_WORK_DIR / "jobs"
    if jobs_dir.exists():
        for root, dirs, files in os.walk(jobs_dir):
            for f in sorted(files):
                fp = Path(root) / f
                if fp.is_file():
                    snapshot[str(fp)] = hashlib.sha256(fp.read_bytes()).hexdigest()
    return snapshot


def verify_real_workspace_snapshot(before_snap):
    """Verify that no monitored file in REAL_WORK_DIR or jobs/ was changed."""
    after_snap = get_real_workspace_snapshot()
    diffs = []
    all_keys = set(before_snap.keys()) | set(after_snap.keys())
    for k in sorted(all_keys):
        v_before = before_snap.get(k)
        v_after = after_snap.get(k)
        if v_before != v_after:
            diffs.append((k, v_before, v_after))
    if diffs:
        diff_msgs = [f"  MUTATED: {k} (before={b[:8] if b else 'None'} -> after={a[:8] if a else 'None'})" for k, b, a in diffs]
        err_msg = "REAL WORKSPACE MUTATION DETECTED!\n" + "\n".join(diff_msgs)
        print(f"\n[CRITICAL FAILURE] {err_msg}\n")
        raise AssertionError(err_msg)


class SimpleMonkeyPatch:
    """Minimal monkeypatch implementation for standalone script execution without pytest."""
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        old_val = getattr(target, name)
        self._undo.append((target, name, old_val))
        setattr(target, name, value)

    def undo(self):
        for target, name, old_val in reversed(self._undo):
            setattr(target, name, old_val)
        self._undo.clear()


def _drain_queue():
    """Drain queue and reset BATCH_QUEUE to clean initial state."""
    with webui_server._queue_lock:
        while not webui_server._task_queue.empty():
            try:
                webui_server._task_queue.get_nowait()
                webui_server._task_queue.task_done()
            except Exception:
                pass
        webui_server.BATCH_QUEUE["is_running"] = False
        webui_server.BATCH_QUEUE["total"] = 0
        webui_server.BATCH_QUEUE["current_index"] = 0
        webui_server.BATCH_QUEUE["current_title"] = ""
        webui_server.BATCH_QUEUE["percent"] = 0
        webui_server.BATCH_QUEUE["items"] = []
        if "finished_at" in webui_server.BATCH_QUEUE:
            webui_server.BATCH_QUEUE["finished_at"] = None


class IsolatedTestEnv:
    """Creates a temporary workspace and monkeypatches webui_server path constants."""
    def __init__(self, monkeypatcher):
        self.mp = monkeypatcher
        self.tmp_dir = None
        self.before_snapshot = None
        self.orig_job_status = None
        self.orig_batch_queue = None

    def __enter__(self):
        # 1. Snapshot real workspace before test
        self.before_snapshot = get_real_workspace_snapshot()

        # 2. Backup in-memory structures
        self.orig_job_status = copy.deepcopy(webui_server.JOB_STATUS)
        self.orig_batch_queue = copy.deepcopy(webui_server.BATCH_QUEUE)

        # 3. Create isolated temp dir
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="test_isolated_webui_"))
        tmp_jobs = self.tmp_dir / "jobs"
        tmp_jobs.mkdir(parents=True, exist_ok=True)
        tmp_uploads = self.tmp_dir / "uploads"
        tmp_uploads.mkdir(parents=True, exist_ok=True)
        tmp_history = self.tmp_dir / "history.json"
        tmp_history.write_text("[]", encoding="utf-8")
        tmp_style = self.tmp_dir / "style_config.json"
        tmp_style.write_text("{}", encoding="utf-8")

        # 4. Monkeypatch all module-level paths and default active_id
        self.mp.setattr(webui_server, "BASE_DIR", self.tmp_dir)
        self.mp.setattr(webui_server, "JOBS_DIR", tmp_jobs)
        self.mp.setattr(webui_server, "HISTORY_FILE", tmp_history)
        self.mp.setattr(webui_server, "STYLE_FILE", tmp_style)

        # 5. Reset queue and ensure safe non-existent active_id
        _drain_queue()
        webui_server.JOB_STATUS["active_id"] = "test_fake_active_id"

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 1. Drain any remaining tasks
        _drain_queue()

        # 2. Restore in-memory state
        webui_server.JOB_STATUS.clear()
        webui_server.JOB_STATUS.update(copy.deepcopy(self.orig_job_status))
        webui_server.BATCH_QUEUE.clear()
        webui_server.BATCH_QUEUE.update(copy.deepcopy(self.orig_batch_queue))

        # 3. Clean up temp dir
        if self.tmp_dir and self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

        # 4. Verify snapshot of real workspace (no mutations allowed)
        verify_real_workspace_snapshot(self.before_snapshot)


def test_youtube_sequential_enqueue_and_completion(monkeypatch):
    """
    1. 連續 POST /api/youtube 3 次都 200、queue_total 1→2→3、task id 不重複、
       送出後立刻 GET /api/queue 的 is_running == True、最後全部 done、percent == 100、finished_at 非 None。
    """
    with IsolatedTestEnv(monkeypatch) as env:
        def fake_youtube_pipeline(url, src_langs=None, tgt_langs=None):
            time.sleep(0.08)

        monkeypatch.setattr(webui_server, "run_youtube_pipeline", fake_youtube_pipeline)

        client = TestClient(webui_server.app)

        # 1. First POST
        r1 = client.post("/api/youtube", json={"url": "https://www.youtube.com/watch?v=vid1"})
        assert r1.status_code == 200, f"POST 1 failed: {r1.text}"
        assert r1.json().get("queue_total") == 1, f"Expected queue_total 1, got {r1.json()}"

        # Immediately check is_running == True
        q1 = client.get("/api/queue").json()
        assert q1.get("is_running") is True, f"Expected is_running True immediately after submit, got {q1}"

        # 2. Second POST
        r2 = client.post("/api/youtube", json={"url": "https://www.youtube.com/watch?v=vid2"})
        assert r2.status_code == 200, f"POST 2 failed: {r2.text}"
        assert r2.json().get("queue_total") == 2, f"Expected queue_total 2, got {r2.json()}"

        # 3. Third POST
        r3 = client.post("/api/youtube", json={"url": "https://www.youtube.com/watch?v=vid3"})
        assert r3.status_code == 200, f"POST 3 failed: {r3.text}"
        assert r3.json().get("queue_total") == 3, f"Expected queue_total 3, got {r3.json()}"

        # Verify task IDs are not duplicate
        q_state = client.get("/api/queue").json()
        items = q_state.get("items", [])
        task_ids = [it["id"] for it in items]
        assert len(task_ids) == 3, f"Expected 3 tasks in queue, got {task_ids}"
        assert len(set(task_ids)) == 3, f"Duplicate task IDs found: {task_ids}"

        # Wait for all tasks to complete sequentially
        start_time = time.time()
        while time.time() - start_time < 5.0:
            q = client.get("/api/queue").json()
            if not q.get("is_running") and q.get("percent") == 100:
                break
            time.sleep(0.05)

        final_q = client.get("/api/queue").json()
        assert final_q.get("is_running") is False, f"Expected is_running False when all done, got {final_q}"
        assert final_q.get("percent") == 100, f"Expected percent 100, got {final_q.get('percent')}"
        assert len(final_q.get("items", [])) == 3, f"Expected 3 items in final queue: {final_q.get('items')}"
        assert all(it.get("status") == "done" for it in final_q.get("items", [])), f"Not all items done: {final_q.get('items')}"
        assert final_q.get("finished_at") is not None, f"Expected finished_at to be not None, got {final_q.get('finished_at')}"


def test_upload_temp_cleanup(monkeypatch):
    """
    2. POST /api/upload（用小 bytes 假檔）回 200、queued True；處理完後 uploads/ 中對應暫存檔已刪除。
    """
    with IsolatedTestEnv(monkeypatch) as env:
        def fake_pipeline(file_path, title, src_langs=None, tgt_langs=None):
            time.sleep(0.05)

        monkeypatch.setattr(webui_server, "run_pipeline", fake_pipeline)

        client = TestClient(webui_server.app)

        fake_content = b"fake video small bytes header"
        file_name = "test_guard_sample.mp4"
        files = {"file": (file_name, fake_content, "video/mp4")}
        data = {"src_langs": "auto", "tgt_langs": "zh-TW"}

        resp = client.post("/api/upload", files=files, data=data)
        assert resp.status_code == 200, f"Upload failed: {resp.text}"
        body = resp.json()
        assert body.get("queued") is True, f"Expected queued: True, got {body}"

        # Wait for processing to complete
        start_time = time.time()
        while time.time() - start_time < 5.0:
            q = client.get("/api/queue").json()
            if not q.get("is_running") and any(it.get("status") == "done" for it in q.get("items", [])):
                break
            time.sleep(0.05)

        final_q = client.get("/api/queue").json()
        assert final_q.get("is_running") is False
        assert any(it.get("status") == "done" for it in final_q.get("items", []))

        # Check uploads inside isolated temp dir
        tmp_uploads = env.tmp_dir / "uploads"
        remaining = list(tmp_uploads.glob(f"*{file_name}"))
        assert len(remaining) == 0, f"Expected temp file for {file_name} in {tmp_uploads} to be deleted, but found: {remaining}"


def test_guards_during_running_and_done(monkeypatch):
    """
    3. JOB_STATUS["state"]="running" 時：
       POST /api/history/{任一存在或不存在id}/activate → 對存在的 id 回 409（若 history 為空，就先用 monkeypatch 讓 webui_server.load_history 回傳一筆假記錄）；
       POST /api/style 回 200 且 deferred True。
       state="done" 時 POST /api/style deferred False。
    """
    with IsolatedTestEnv(monkeypatch) as env:
        # Isolated fake history in temp dir
        fake_job_id = "test_fake_hist_123"
        fake_job_dir = env.tmp_dir / "jobs" / fake_job_id
        fake_job_dir.mkdir(parents=True, exist_ok=True)
        fake_history = [
            {"id": fake_job_id, "title": "Isolated Historical Job", "tgt_langs": ["zh-TW"]}
        ]
        monkeypatch.setattr(webui_server, "load_history", lambda: fake_history)

        # Strictly monkeypatch external subprocess and build_vtt_and_transcript to no-op
        recorded_calls = []
        def fake_subprocess_run(cmd, *a, **kw):
            recorded_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(webui_server.subprocess, "run", fake_subprocess_run)
        monkeypatch.setattr(webui_server, "build_vtt_and_transcript", lambda *a, **kw: None)

        client = TestClient(webui_server.app)

        # 1. When JOB_STATUS["state"] == "running"
        webui_server.JOB_STATUS["state"] = "running"
        webui_server.JOB_STATUS["active_id"] = "test_current_running_job"

        # POST /api/history/{existing_id}/activate -> 409 Conflict
        res_act = client.post(f"/api/history/{fake_job_id}/activate")
        assert res_act.status_code == 409, f"Expected 409 during running, got {res_act.status_code}: {res_act.text}"

        # POST /api/style during running -> 200, deferred True
        res_style_run = client.post("/api/style", json={"card_color": "#FF0000", "card_size": 42})
        assert res_style_run.status_code == 200, f"Expected 200 for /api/style, got {res_style_run.status_code}"
        run_body = res_style_run.json()
        assert run_body.get("deferred") is True, f"Expected deferred: True during running, got {run_body}"

        # 2. When JOB_STATUS["state"] == "done"
        webui_server.JOB_STATUS["state"] = "done"
        webui_server.JOB_STATUS["active_id"] = fake_job_id
        res_style_done = client.post("/api/style", json={"card_color": "#00FF00", "card_size": 36})
        assert res_style_done.status_code == 200, f"Expected 200 for /api/style, got {res_style_done.status_code}"
        done_body = res_style_done.json()
        assert done_body.get("deferred") is False, f"Expected deferred: False when done, got {done_body}"


def test_queue_clear_preserves_running_and_deletes_pending_temp_files(monkeypatch):
    """
    4. POST /api/queue/clear：佔住 worker 一個長任務時，再塞 2 個 pending file 任務，
       clear 後 removed==2、暫存檔已刪、running 項目保留、is_running True。
    """
    with IsolatedTestEnv(monkeypatch) as env:
        started_event = threading.Event()
        block_event = threading.Event()

        def fake_youtube_pipeline_blocking(url, src_langs=None, tgt_langs=None):
            started_event.set()
            block_event.wait(timeout=10.0)

        monkeypatch.setattr(webui_server, "run_youtube_pipeline", fake_youtube_pipeline_blocking)

        client = TestClient(webui_server.app)

        try:
            # Enqueue long-running task to occupy worker
            r_occupy = client.post("/api/youtube", json={"url": "https://www.youtube.com/watch?v=long_occupy"})
            assert r_occupy.status_code == 200

            # Wait until worker begins processing
            assert started_event.wait(timeout=3.0), "Worker did not start the long task in time"

            # Now worker is running the occupy task. Enqueue 2 pending file tasks.
            f1_name = "pending_clear_1.mp4"
            f2_name = "pending_clear_2.mp4"
            r_f1 = client.post("/api/upload", files={"file": (f1_name, b"pending file 1 bytes", "video/mp4")}, data={"src_langs": "auto", "tgt_langs": "zh-TW"})
            assert r_f1.status_code == 200

            r_f2 = client.post("/api/upload", files={"file": (f2_name, b"pending file 2 bytes", "video/mp4")}, data={"src_langs": "auto", "tgt_langs": "zh-TW"})
            assert r_f2.status_code == 200

            # Verify both temp files exist in tmp_uploads
            tmp_uploads = env.tmp_dir / "uploads"
            p1_files = list(tmp_uploads.glob(f"*{f1_name}"))
            p2_files = list(tmp_uploads.glob(f"*{f2_name}"))
            assert len(p1_files) >= 1, f"Expected {f1_name} temp file to exist in {tmp_uploads}"
            assert len(p2_files) >= 1, f"Expected {f2_name} temp file to exist in {tmp_uploads}"

            # POST /api/queue/clear
            clear_resp = client.post("/api/queue/clear")
            assert clear_resp.status_code == 200, f"clear_queue failed: {clear_resp.text}"
            clear_data = clear_resp.json()

            # Check removed == 2
            assert clear_data.get("removed") == 2, f"Expected removed==2, got {clear_data}"

            # Check temp files of removed tasks are deleted
            assert not any(p.exists() for p in p1_files + p2_files), "Temp files for removed tasks were not deleted"

            # Check running item preserved and is_running True
            q = client.get("/api/queue").json()
            assert q.get("is_running") is True, f"Expected is_running True while worker is still running, got {q}"
            items = q.get("items", [])
            assert len(items) == 1, f"Expected exactly 1 running item preserved, got {len(items)} items: {items}"
            assert items[0].get("status") == "running", f"Preserved item status should be 'running', got {items[0]}"
            assert items[0].get("url") == "https://www.youtube.com/watch?v=long_occupy"

        finally:
            # Always unblock worker to clean up
            block_event.set()
            start_wait = time.time()
            while time.time() - start_wait < 3.0:
                if not webui_server.BATCH_QUEUE.get("is_running"):
                    break
                time.sleep(0.05)


def test_readme_api_endpoints_match_routes():
    """
    5. README.md 中 API 表格列出的每個路徑都要能在 webui_server.app.routes 中找到
       （把 {job_id} 等參數路徑做正規化比對），且不得出現 /api/translate、/api/batch/info、/api/history/select。
    """
    # Verify snapshot around read-only test as well
    before_snap = get_real_workspace_snapshot()
    try:
        readme_path = REAL_WORK_DIR / "README.md"
        assert readme_path.exists(), "README.md not found"
        readme_text = readme_path.read_text(encoding="utf-8")

        assert "## Web API 端點速查" in readme_text, "API endpoints section missing in README.md"
        api_section = readme_text.split("## Web API 端點速查")[1]

        table_lines = []
        for line in api_section.splitlines():
            if line.startswith("## "):
                break
            table_lines.append(line)

        table_entries = []
        for line in table_lines:
            line = line.strip()
            if not line.startswith("|"):
                continue
            # Split on unescaped pipe
            parts = [p.strip() for p in re.split(r'(?<!\\)\|', line)]
            if len(parts) >= 4:
                method = parts[1].replace("\\|", "|").replace("`", "").strip()
                path = parts[2].replace("`", "").strip()
                if method in ("方法", ":---") or method.startswith(":") or method.startswith("-"):
                    continue
                table_entries.append((method, path))

        assert len(table_entries) > 0, "No API entries extracted from README.md"

        table_paths = [p for _, p in table_entries]

        # Must NOT contain obsolete paths
        forbidden = ["/api/translate", "/api/batch/info", "/api/history/select"]
        for f in forbidden:
            assert f not in table_paths, f"Obsolete/forbidden path '{f}' found in README.md API table"

        def normalize_path(p: str) -> str:
            p = p.strip()
            p = re.sub(r'\{[^}]+\}', '{param}', p)
            p = re.sub(r'<[^>]+>', '{param}', p)
            if len(p) > 1 and p.endswith('/'):
                p = p[:-1]
            return p

        app_normalized_routes = {normalize_path(r.path) for r in webui_server.app.routes}

        missing_routes = []
        for m, p in table_entries:
            norm = normalize_path(p)
            if norm not in app_normalized_routes:
                missing_routes.append((m, p, norm))

        assert len(missing_routes) == 0, f"The following README routes were not found in webui_server.app.routes: {missing_routes}"
    finally:
        verify_real_workspace_snapshot(before_snap)


if __name__ == "__main__":
    print("Running tests in tests/test_queue_and_guards.py standalone with full isolation...")
    test_list = [
        ("test_youtube_sequential_enqueue_and_completion", test_youtube_sequential_enqueue_and_completion),
        ("test_upload_temp_cleanup", test_upload_temp_cleanup),
        ("test_guards_during_running_and_done", test_guards_during_running_and_done),
        ("test_queue_clear_preserves_running_and_deletes_pending_temp_files", test_queue_clear_preserves_running_and_deletes_pending_temp_files),
        ("test_readme_api_endpoints_match_routes", test_readme_api_endpoints_match_routes),
    ]

    passed = 0
    failed = 0

    for name, fn in test_list:
        print(f"\n--- Running {name} ---")
        mp = SimpleMonkeyPatch()

        try:
            import inspect
            sig = inspect.signature(fn)
            if "monkeypatch" in sig.parameters:
                fn(mp)
            else:
                fn()
            print(f"[PASS] {name}")
            passed += 1
        except Exception as e:
            print(f"[FAIL] {name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
        finally:
            mp.undo()

    print(f"\n==========================================")
    print(f"Test Summary: {passed} passed, {failed} failed")
    print(f"==========================================")
    sys.exit(0 if failed == 0 else 1)
