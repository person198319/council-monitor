"""
Web 服務入口
FastAPI + WebSocket 即時推送逐字稿，多人共用同一畫面。

啟動方式：
  cd council-monitor
  python web/app.py
  # 瀏覽器開啟 http://localhost:8000
"""
import asyncio
import queue
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel

# 加入專案根目錄到 path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.audio_stream import robust_stream, file_stream_chunks, get_stream_url
from pipeline.transcriber import Transcriber
from pipeline.session_manager import SessionManager

STATIC_DIR = ROOT / "static"


# ──────────────────────────────────────────────────────────
# 應用程式狀態
# ──────────────────────────────────────────────────────────

class AppState:
    def __init__(self):
        self.segments: list[dict] = []
        self.sessions: list[dict] = []
        self.clients: set[WebSocket] = set()
        self.running = False
        self.status = "stopped"     # stopped / running / analyzing
        self.stop_event = threading.Event()
        self.event_queue: queue.Queue = queue.Queue()
        self._group_counter = 0
        self._last_role: str | None = None

    def reset(self) -> None:
        self.segments.clear()
        self.sessions.clear()
        self._group_counter = 0
        self._last_role = None

    def add_segment(self, seg: dict) -> dict:
        """加入 segment_id 與 group_id，group 以角色連續性分群"""
        role = seg.get("role", "不明")
        if role != self._last_role:
            self._group_counter += 1
            self._last_role = role
        enriched = {
            **seg,
            "segment_id": len(self.segments),
            "group_id": self._group_counter,
        }
        self.segments.append(enriched)
        return enriched

    def update_speaker(self, segment_id: int, name: str, apply_to_group: bool) -> list[int]:
        """更新說話者名稱，回傳所有被更新的 segment_id 列表"""
        if segment_id >= len(self.segments):
            return []
        seg = self.segments[segment_id]
        updated: list[int] = []
        if apply_to_group:
            group_id = seg["group_id"]
            for s in self.segments:
                if s["group_id"] == group_id:
                    s["speaker_name"] = name
                    updated.append(s["segment_id"])
        else:
            seg["speaker_name"] = name
            updated.append(segment_id)
        return updated


state = AppState()


# ──────────────────────────────────────────────────────────
# 背景 Pipeline（在獨立 thread 跑，透過 event_queue 回報）
# ──────────────────────────────────────────────────────────

def _put(event: dict) -> None:
    state.event_queue.put(event)


def _check_is_live(youtube_url: str, cfg: dict) -> bool:
    """判斷是直播還是錄影，決定是否重連"""
    import yt_dlp
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "extractor_args": {"youtube": {"player_client": ["web"]}},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            return bool(info.get("is_live") or info.get("was_live"))
    except Exception:
        return False


def _run_pipeline(youtube_url: str, cfg: dict) -> None:
    """在獨立 thread 中執行 ASR + Session 管理，結果透過 event_queue 回傳"""
    wcfg = cfg["whisper"]
    acfg = cfg["audio"]
    ycfg = cfg.get("youtube", {})

    is_live = _check_is_live(youtube_url, cfg)
    logger.info(f"{'直播' if is_live else '錄影'}模式：{youtube_url}")

    transcriber = Transcriber(
        model_size=wcfg["model"],
        device=wcfg["device"],
        compute_type=wcfg["compute_type"],
        language=wcfg["language"],
        num_workers=wcfg["num_workers"],
        initial_prompt=wcfg["initial_prompt"],
    )
    session_mgr = SessionManager(cfg)

    # 選擇串流來源
    if is_live:
        stream_src = robust_stream(
            youtube_url=youtube_url,
            sample_rate=acfg["sample_rate"],
            chunk_sec=acfg["chunk_sec"],
            overlap_sec=acfg["overlap_sec"],
            reconnect_delay=ycfg.get("reconnect_delay", 5),
        )
    else:
        # 錄影：只跑一次，不重連
        stream_url = get_stream_url(youtube_url)
        from pipeline.audio_stream import stream_audio_chunks
        stream_src = stream_audio_chunks(
            stream_url,
            sample_rate=acfg["sample_rate"],
            chunk_sec=acfg["chunk_sec"],
            overlap_sec=acfg["overlap_sec"],
        )

    _put({"type": "status", "data": {"status": "running"}})

    try:
        for audio_chunk, chunk_offset in stream_src:
            if state.stop_event.is_set():
                break

            transcriber.feed(audio_chunk, chunk_offset)

            for result in transcriber.get_results():
                seg = state.add_segment(result)
                _put({"type": "segment", "data": seg})
                session_mgr.on_text(result)

            completed = session_mgr.check_timeout()
            if completed:
                idx = len(state.sessions)
                state.sessions.append(completed)
                _put({"type": "session_end", "data": {"session_id": idx, **completed}})

    except Exception as e:
        logger.error(f"Pipeline 錯誤：{e}")
        _put({"type": "error", "data": {"message": str(e)}})
    finally:
        transcriber.stop()
        state.running = False
        _put({"type": "status", "data": {"status": "stopped"}})


# ──────────────────────────────────────────────────────────
# FastAPI
# ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_broadcast_loop())
    yield

app = FastAPI(title="議會質詢監控", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ──────────────────────────────────────────────────────────
# WebSocket
# ──────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    state.clients.add(websocket)
    try:
        # 新連線：送出當前完整狀態供重新整理後恢復
        await websocket.send_json({
            "type": "init",
            "data": {
                "running": state.running,
                "status": state.status,
                "segments": state.segments,
                "sessions": [
                    {"session_id": i, **s}
                    for i, s in enumerate(state.sessions)
                ],
            },
        })
        while True:
            await websocket.receive_text()   # 保持連線
    except WebSocketDisconnect:
        pass
    finally:
        state.clients.discard(websocket)


async def _broadcast(event: dict) -> None:
    dead: set[WebSocket] = set()
    for ws in list(state.clients):
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    state.clients -= dead


async def _broadcast_loop() -> None:
    """每 50ms 清空 event_queue，廣播給所有 WebSocket 客戶端"""
    while True:
        await asyncio.sleep(0.05)
        while not state.event_queue.empty():
            try:
                event = state.event_queue.get_nowait()
                # 同步更新 status
                if event["type"] == "status":
                    state.status = event["data"]["status"]
                await _broadcast(event)
            except queue.Empty:
                break


# ──────────────────────────────────────────────────────────
# REST API
# ──────────────────────────────────────────────────────────

def _load_cfg() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


class StartReq(BaseModel):
    url: str

class SpeakerReq(BaseModel):
    segment_id: int
    name: str
    apply_to_group: bool = True

class SummaryReq(BaseModel):
    session_id: int


@app.post("/api/monitor/start")
async def api_start(req: StartReq):
    if state.running:
        return {"ok": False, "error": "already running"}
    cfg = _load_cfg()
    state.running = True
    state.stop_event.clear()
    state.reset()
    threading.Thread(
        target=_run_pipeline, args=(req.url, cfg), daemon=True
    ).start()
    return {"ok": True}


@app.post("/api/monitor/stop")
async def api_stop():
    state.stop_event.set()
    return {"ok": True}


@app.post("/api/speaker")
async def api_speaker(req: SpeakerReq):
    updated = state.update_speaker(req.segment_id, req.name, req.apply_to_group)
    await _broadcast({
        "type": "speaker_update",
        "data": {"segment_ids": updated, "name": req.name},
    })
    return {"ok": True, "updated": len(updated)}


@app.post("/api/summary")
async def api_summary(req: SummaryReq):
    if req.session_id >= len(state.sessions):
        return {"ok": False, "error": "session not found"}

    session = state.sessions[req.session_id]
    cfg = _load_cfg()
    ocfg = cfg["ollama"]

    await _broadcast({"type": "status", "data": {
        "status": "analyzing", "session_id": req.session_id,
    }})

    def _analyze():
        from analysis.summarizer import Summarizer
        from analysis.dispatcher import Dispatcher
        summarizer = Summarizer(host=ocfg["host"], model=ocfg["summary_model"])
        dispatcher = Dispatcher(host=ocfg["host"], model=ocfg["classify_model"])
        summary = summarizer.generate(
            transcript=session["transcript"],
            timestamp=session["timestamp"],
        )
        if summary is None:
            return None, None
        return summary, dispatcher.dispatch(summary)

    loop = asyncio.get_event_loop()
    summary, dispatch = await loop.run_in_executor(None, _analyze)

    if summary is None:
        await _broadcast({"type": "status", "data": {"status": "stopped"}})
        return {"ok": False, "error": "摘要生成失敗"}

    result = {
        "session_id":  req.session_id,
        "質詢主題":    summary.質詢主題,
        "議員姓名":    summary.議員姓名,
        "質詢對象":    summary.質詢對象,
        "質詢摘要":    summary.質詢摘要,
        "局方回應":    summary.局方回應,
        "緊急程度":    summary.緊急程度,
        "承諾事項":    summary.承諾事項,
        "待辦追蹤":    summary.待辦追蹤,
        "關鍵數字":    summary.關鍵數字,
        "主責單位":    dispatch.primary,
        "協辦單位":    dispatch.secondary,
    }
    state.sessions[req.session_id]["summary"] = result
    await _broadcast({"type": "summary", "data": result})
    await _broadcast({"type": "status", "data": {"status": "stopped"}})
    return {"ok": True, "summary": result}


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} {level} {message}")
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
