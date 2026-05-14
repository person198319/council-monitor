"""
P1 - YouTube 直播音訊串流擷取
yt-dlp 取得 HLS URL → ffmpeg 解碼為 16kHz PCM → 滑動視窗 yield (numpy array, offset_sec)

offset_sec 是該 chunk 在直播中的起始絕對秒數（直播開始後經過幾秒）。
"""
import subprocess
import threading
import time
import numpy as np
import yt_dlp
from datetime import datetime, timezone
from loguru import logger


def get_stream_url(youtube_url: str) -> str:
    """從 YouTube 取得 HLS 音訊串流 URL"""
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 60,
        "extractor_args": {
            "youtube": {"player_client": ["web"]},
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)
        url = info.get("url") or info["formats"][-1]["url"]
        logger.info(f"取得串流 URL（前50字）：{url[:50]}...")
        return url


def get_stream_start_time(youtube_url: str) -> float:
    """
    取得直播已播放秒數，用於校正時間戳。
    一般影片或取得失敗時回傳 0.0。
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 60,
        "extractor_args": {
            "youtube": {"player_client": ["web"]},
        },
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            start_ts = info.get("release_timestamp") or info.get("timestamp")
            if start_ts:
                elapsed = datetime.now(timezone.utc).timestamp() - start_ts
                elapsed = max(0.0, elapsed)
                logger.info(f"直播已進行 {elapsed:.0f} 秒（{elapsed / 60:.1f} 分鐘）")
                return elapsed
    except Exception as e:
        logger.warning(f"無法取得直播開始時間：{e}")
    return 0.0


def get_youtube_jump_url(youtube_url: str, sec: float) -> str:
    """產生跳轉到指定秒數的 YouTube 連結"""
    base = youtube_url.split("&t=")[0]
    return f"{base}&t={int(sec)}"


def stream_audio_chunks(
    stream_url: str,
    sample_rate: int = 16000,
    chunk_sec: int = 5,
    overlap_sec: int = 1,
    start_offset_sec: float = 0.0,
):
    """
    從 HLS 串流持續讀取音訊，yield (audio_array, absolute_offset_sec)。
    absolute_offset_sec 是該 chunk 在直播中的起始秒數。
    """
    cmd = [
        "ffmpeg",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto,hls,applehttp",
        "-i", stream_url,
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ar", str(sample_rate),
        "-ac", "1",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-loglevel", "error",
        "pipe:1",
    ]

    chunk_bytes = sample_rate * 2 * chunk_sec
    overlap_bytes = sample_rate * 2 * overlap_sec
    step_sec = chunk_sec - overlap_sec
    buf = b""
    elapsed_sec = start_offset_sec

    logger.info(f"ffmpeg 串流啟動（起始偏移 {start_offset_sec:.1f}s）")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _drain_stderr():
        for line in proc.stderr:
            logger.debug(f"[ffmpeg] {line.decode('utf-8', errors='ignore').strip()}")
    threading.Thread(target=_drain_stderr, daemon=True).start()

    try:
        while True:
            data = proc.stdout.read(4096)
            if not data:
                logger.warning("ffmpeg 串流結束")
                break
            buf += data
            if len(buf) >= chunk_bytes:
                audio = (
                    np.frombuffer(buf[:chunk_bytes], dtype=np.int16)
                    .astype(np.float32) / 32768.0
                )
                yield audio, elapsed_sec
                elapsed_sec += step_sec
                buf = buf[chunk_bytes - overlap_bytes:]
    finally:
        proc.terminate()
        proc.wait()


def file_stream_chunks(
    file_path: str,
    sample_rate: int = 16000,
    chunk_sec: int = 5,
    overlap_sec: int = 1,
):
    """
    從本機音檔讀取，切成 chunk yield (audio, offset_sec)。
    行為與 stream_audio_chunks 完全一致，供 --test-file 使用。
    """
    cmd = [
        "ffmpeg", "-i", file_path,
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", str(sample_rate), "-ac", "1",
        "-loglevel", "error", "pipe:1",
    ]
    chunk_bytes = sample_rate * 2 * chunk_sec
    overlap_bytes = sample_rate * 2 * overlap_sec
    step_sec = chunk_sec - overlap_sec
    buf = b""
    elapsed_sec = 0.0

    logger.info(f"測試音檔：{file_path}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            data = proc.stdout.read(4096)
            if not data:
                break
            buf += data
            if len(buf) >= chunk_bytes:
                audio = (
                    np.frombuffer(buf[:chunk_bytes], dtype=np.int16)
                    .astype(np.float32) / 32768.0
                )
                yield audio, elapsed_sec
                elapsed_sec += step_sec
                buf = buf[chunk_bytes - overlap_bytes:]
        if len(buf) >= sample_rate * 2:
            audio = (
                np.frombuffer(buf, dtype=np.int16)
                .astype(np.float32) / 32768.0
            )
            yield audio, elapsed_sec
    finally:
        proc.terminate()
        proc.wait()


def robust_stream(
    youtube_url: str,
    sample_rate: int = 16000,
    chunk_sec: int = 5,
    overlap_sec: int = 1,
    reconnect_delay: int = 5,
):
    """
    自動重連的串流包裝器。
    啟動時先校正直播已播放時長，重連時維持連續時間軸。
    """
    stream_offset = get_stream_start_time(youtube_url)
    logger.info(f"取得直播串流：{youtube_url}（偏移 {stream_offset:.1f}s）")

    while True:
        try:
            stream_url = get_stream_url(youtube_url)
            for audio, chunk_offset in stream_audio_chunks(
                stream_url,
                sample_rate=sample_rate,
                chunk_sec=chunk_sec,
                overlap_sec=overlap_sec,
                start_offset_sec=stream_offset,
            ):
                stream_offset = chunk_offset + (chunk_sec - overlap_sec)
                yield audio, chunk_offset
        except Exception as e:
            logger.error(f"串流中斷：{e}，{reconnect_delay} 秒後重連")
            try:
                new_offset = get_stream_start_time(youtube_url)
                if new_offset > stream_offset:
                    stream_offset = new_offset
            except Exception:
                pass
            time.sleep(reconnect_delay)
