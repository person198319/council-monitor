"""
P1 - YouTube 直播音訊串流擷取
yt-dlp 取得 HLS URL → ffmpeg 解碼為 16kHz PCM → 滑動視窗 yield numpy array
"""
import subprocess
import time
import numpy as np
import yt_dlp
from loguru import logger


def get_stream_url(youtube_url: str) -> str:
    """從 YouTube 直播取得 HLS 串流 URL（約 6 小時後過期，需重取）"""
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)
        url = info.get("url") or info["formats"][-1]["url"]
        logger.info(f"取得串流 URL（前50字）：{url[:50]}...")
        return url


def stream_audio_chunks(
    stream_url: str,
    sample_rate: int = 16000,
    chunk_sec: int = 20,
    overlap_sec: int = 5,
):
    """
    從 HLS 串流持續讀取音訊，以滑動視窗 yield numpy float32 array。
    重疊 overlap_sec 秒避免關鍵詞被截斷在段落邊界。
    """
    cmd = [
        "ffmpeg",
        "-i", stream_url,
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ar", str(sample_rate),
        "-ac", "1",           # mono
        "-loglevel", "error",
        "pipe:1",
    ]

    chunk_bytes = sample_rate * 2 * chunk_sec
    overlap_bytes = sample_rate * 2 * overlap_sec
    read_unit = sample_rate * 2  # 每次讀 1 秒
    buf = b""

    logger.info("ffmpeg 串流啟動")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        while True:
            data = proc.stdout.read(read_unit)
            if not data:
                logger.warning("ffmpeg 串流結束")
                break

            buf += data
            if len(buf) >= chunk_bytes:
                audio = (
                    np.frombuffer(buf[:chunk_bytes], dtype=np.int16)
                    .astype(np.float32) / 32768.0
                )
                yield audio
                buf = buf[chunk_bytes - overlap_bytes:]  # 保留尾端重疊

    finally:
        proc.terminate()
        proc.wait()


def robust_stream(
    youtube_url: str,
    sample_rate: int = 16000,
    chunk_sec: int = 20,
    overlap_sec: int = 5,
    reconnect_delay: int = 5,
):
    """
    自動重連的串流包裝器。
    HLS URL 約 6 小時過期，每次重連都重新取得新 URL。
    """
    while True:
        try:
            logger.info(f"取得直播串流：{youtube_url}")
            stream_url = get_stream_url(youtube_url)
            yield from stream_audio_chunks(stream_url, sample_rate, chunk_sec, overlap_sec)
        except Exception as e:
            logger.error(f"串流中斷：{e}，{reconnect_delay} 秒後重連")
            time.sleep(reconnect_delay)
