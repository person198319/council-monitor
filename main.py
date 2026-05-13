"""
議會質詢監控系統 — 主程式入口

用法：
  python main.py --stage p1                    # P1：ASR 逐字稿驗證
  python main.py --stage p1 --test-file a.wav  # P1：本機音檔測試
  python main.py --stage p2                    # P2：加入關鍵詞偵測 + session 管理
  python main.py --stage p2 --test-text        # P2：直接輸入文字測試偵測邏輯
"""
import argparse
import sys
from datetime import datetime
import yaml
from loguru import logger

from pipeline.audio_stream import robust_stream
from pipeline.transcriber import Transcriber
from pipeline.session_manager import SessionManager
from analysis.summarizer import Summarizer, SessionSummary
from analysis.dispatcher import Dispatcher, DispatchResult
from output.storage import save_session
from output.notifier import notify
from output.digest import send_weekly_digest


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_p1(cfg: dict, test_file: str | None = None) -> None:
    """P1：音訊串流 + 即時逐字稿，驗證 ASR 品質"""
    wcfg = cfg["whisper"]
    transcriber = Transcriber(
        model_size=wcfg["model"],
        device=wcfg["device"],
        compute_type=wcfg["compute_type"],
        language=wcfg["language"],
        num_workers=wcfg["num_workers"],
        initial_prompt=wcfg["initial_prompt"],
    )

    acfg = cfg["audio"]

    if test_file:
        # 本機音檔測試模式
        import numpy as np
        from faster_whisper import WhisperModel
        logger.info(f"測試模式：讀取 {test_file}")
        # 直接用 faster-whisper 轉整個檔案，確認 ASR 品質
        model = WhisperModel(
            wcfg["model"],
            device=wcfg["device"],
            compute_type=wcfg["compute_type"],
        )
        segments, _ = model.transcribe(
            test_file,
            language=wcfg["language"],
            initial_prompt=wcfg["initial_prompt"],
            beam_size=5,
            vad_filter=True,
            word_timestamps=True,
        )
        print("\n=== 逐字稿 ===")
        for seg in segments:
            print(f"[{seg.start:.1f}s – {seg.end:.1f}s] {seg.text.strip()}")
        return

    # 直播串流模式
    ycfg = cfg["youtube"]
    logger.info(f"監控直播：{ycfg['url']}")

    try:
        for audio_chunk in robust_stream(
            youtube_url=ycfg["url"],
            sample_rate=acfg["sample_rate"],
            chunk_sec=acfg["chunk_sec"],
            overlap_sec=acfg["overlap_sec"],
            reconnect_delay=ycfg["reconnect_delay"],
        ):
            transcriber.feed(audio_chunk)

            for result in transcriber.get_results():
                # P1：只印出逐字稿，不做後續處理
                low = f" ⚠️低信心：{result['low_conf_words']}" if result["low_conf_words"] else ""
                print(f"[{result['start']}s] {result['text']}{low}")

    except KeyboardInterrupt:
        logger.info("手動停止")
    finally:
        transcriber.stop()


def _print_session(session: dict) -> None:
    """P2 階段輸出（無 LLM）"""
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"✅ Session 完結｜{session['timestamp']}｜{session['duration_sec']}s")
    print(f"\n逐字稿：\n{session['transcript']}")
    print(f"{sep}\n")


def _print_analysis(
    session: dict,
    summary: SessionSummary,
    dispatch: DispatchResult,
) -> None:
    """P3 階段輸出（含摘要 + 分派）"""
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"✅ 質詢分析完成｜{session['timestamp']}")
    print(f"\n【{summary.質詢主題}】")
    print(f"  議員：{summary.議員姓名}　對象：{summary.質詢對象}")
    print(f"  緊急：{summary.緊急程度}　情緒：{summary.情緒張力}")
    print(f"\n摘要：\n  {summary.質詢摘要}")

    if summary.局方回應:
        print(f"\n局方回應：\n  {summary.局方回應}")

    if summary.承諾事項:
        print(f"\n承諾事項：")
        for item in summary.承諾事項:
            print(f"  ・{item}")

    if summary.待辦追蹤:
        print(f"\n待辦追蹤：")
        for item in summary.待辦追蹤:
            print(f"  ・{item}")

    if summary.關鍵數字:
        print(f"\n關鍵數字：")
        for k, v in summary.關鍵數字.items():
            print(f"  {k}：{v}")

    print(f"\n分派：主責 {dispatch.primary}　協辦 {dispatch.secondary}")
    print(f"      來源：{dispatch.source}　信心：{dispatch.confidence:.2f}")
    print(f"{sep}\n")


def run_p2(cfg: dict, test_file: str | None = None) -> None:
    """P2：ASR + 關鍵詞偵測 + session 管理"""
    wcfg = cfg["whisper"]
    acfg = cfg["audio"]
    ycfg = cfg["youtube"]

    transcriber = Transcriber(
        model_size=wcfg["model"],
        device=wcfg["device"],
        compute_type=wcfg["compute_type"],
        language=wcfg["language"],
        num_workers=wcfg["num_workers"],
        initial_prompt=wcfg["initial_prompt"],
    )
    session_mgr = SessionManager(cfg)

    stream_src = (
        _file_stream(test_file, wcfg, acfg)
        if test_file
        else robust_stream(
            youtube_url=ycfg["url"],
            sample_rate=acfg["sample_rate"],
            chunk_sec=acfg["chunk_sec"],
            overlap_sec=acfg["overlap_sec"],
            reconnect_delay=ycfg["reconnect_delay"],
        )
    )

    try:
        for audio_chunk in stream_src:
            transcriber.feed(audio_chunk)

            for result in transcriber.get_results():
                status = "🔴錄製中" if session_mgr.is_recording else "⚪待機"
                low = f" ⚠️{result['low_conf_words']}" if result["low_conf_words"] else ""
                print(f"{status} [{result['start']}s] {result['text']}{low}")

                session_mgr.on_text(result)

            # 每個 chunk 處理完後檢查靜默逾時
            completed = session_mgr.check_timeout()
            if completed:
                _print_session(completed)

    except KeyboardInterrupt:
        logger.info("手動停止")
    finally:
        transcriber.stop()


def run_p2_text_test(cfg: dict) -> None:
    """
    P2 文字測試模式：直接輸入逐字稿片段，驗證關鍵詞偵測邏輯。
    不需要 GPU 或 YouTube，適合在任何環境快速測試。
    用法：python main.py --stage p2 --test-text
    """
    from pipeline.keyword_detector import KeywordDetector
    detector = KeywordDetector(cfg)

    print("P2 關鍵詞偵測測試（輸入文字，按 Enter 測試，輸入 q 離開）")
    print("-" * 50)

    fake_start = 0.0
    while True:
        text = input("輸入文字 > ").strip()
        if text.lower() == "q":
            break
        if not text:
            continue

        result = {"text": text, "start": fake_start, "end": fake_start + 5.0}
        triggered, conf = detector.check(result)
        fake_start += 5.0

        status = f"🔴 觸發（信心 {conf:.2f}）" if triggered else "⚪ 未觸發"
        print(f"  → {status}\n")


def run_p4(cfg: dict, test_file: str | None = None) -> None:
    """P4：完整流程（P3 + 存檔 + Email 通知）"""
    _run_with_llm(cfg, test_file=test_file, enable_output=True)


def run_p3(cfg: dict, test_file: str | None = None) -> None:
    """P3：P2 全流程 + Gemma 27B 摘要 + 單位分派（不存檔/不通知）"""
    _run_with_llm(cfg, test_file=test_file, enable_output=False)


def _run_with_llm(
    cfg: dict,
    test_file: str | None = None,
    enable_output: bool = False,
) -> None:
    wcfg = cfg["whisper"]
    acfg = cfg["audio"]
    ycfg = cfg["youtube"]
    ocfg = cfg["ollama"]

    transcriber = Transcriber(
        model_size=wcfg["model"],
        device=wcfg["device"],
        compute_type=wcfg["compute_type"],
        language=wcfg["language"],
        num_workers=wcfg["num_workers"],
        initial_prompt=wcfg["initial_prompt"],
    )
    session_mgr = SessionManager(cfg)
    summarizer = Summarizer(host=ocfg["host"], model=ocfg["summary_model"])
    dispatcher = Dispatcher(host=ocfg["host"], model=ocfg["classify_model"])

    stream_src = (
        _file_stream(test_file, wcfg, acfg)
        if test_file
        else robust_stream(
            youtube_url=ycfg["url"],
            sample_rate=acfg["sample_rate"],
            chunk_sec=acfg["chunk_sec"],
            overlap_sec=acfg["overlap_sec"],
            reconnect_delay=ycfg["reconnect_delay"],
        )
    )

    try:
        for audio_chunk in stream_src:
            transcriber.feed(audio_chunk)

            for result in transcriber.get_results():
                status = "🔴" if session_mgr.is_recording else "⚪"
                print(f"{status} [{result['start']}s] {result['text']}")
                session_mgr.on_text(result)

            completed = session_mgr.check_timeout()
            if completed:
                _analyze_session(
                    completed, summarizer, dispatcher,
                    cfg=cfg if enable_output else None,
                )

    except KeyboardInterrupt:
        logger.info("手動停止")
    finally:
        transcriber.stop()


def run_p3_text_test(cfg: dict, enable_output: bool = False) -> None:
    """
    P3/P4 文字測試模式：直接貼入逐字稿，測試摘要生成與單位分派。
    Ollama 需已在本機執行。
    """
    ocfg = cfg["ollama"]
    summarizer = Summarizer(host=ocfg["host"], model=ocfg["summary_model"])
    dispatcher = Dispatcher(host=ocfg["host"], model=ocfg["classify_model"])
    stage = "P4（含存檔/通知）" if enable_output else "P3（不存檔）"

    print(f"{stage} 測試（貼入逐字稿後輸入 END 送出，q 離開）")
    print("-" * 50)

    while True:
        print("貼入逐字稿：")
        lines = []
        while True:
            line = input()
            if line.strip() == "END":
                break
            if line.strip() == "q":
                return
            lines.append(line)

        transcript = "\n".join(lines).strip()
        if not transcript:
            continue

        fake_session = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_sec": 300,
            "transcript": transcript,
            "chunk_count": len(lines),
            "trigger_confidence": 0.9,
        }
        _analyze_session(
            fake_session, summarizer, dispatcher,
            cfg=cfg if enable_output else None,
        )


def _analyze_session(
    session: dict,
    summarizer: "Summarizer",
    dispatcher: "Dispatcher",
    cfg: dict | None = None,
) -> None:
    """摘要生成 + 單位分派 + 存檔 + 通知"""
    logger.info(f"開始分析 session（{session['duration_sec']}s）")

    summary = summarizer.generate(
        transcript=session["transcript"],
        timestamp=session["timestamp"],
    )
    if summary is None:
        logger.error("摘要生成失敗，跳過此 session")
        _print_session(session)
        return

    dispatch = dispatcher.dispatch(summary)
    _print_analysis(session, summary, dispatch)

    if cfg:
        # P4：存檔 + 通知
        sid = save_session(summary, dispatch, session["transcript"], cfg)
        notify(summary, dispatch, sid, cfg)


def _file_stream(test_file: str, wcfg: dict, acfg: dict):
    """用 ffmpeg 讀本機音檔，切成 chunk yield numpy array，行為與直播串流相同"""
    import subprocess
    import numpy as np

    sr = acfg["sample_rate"]
    chunk_sec = acfg["chunk_sec"]
    overlap_sec = acfg["overlap_sec"]
    chunk_bytes = sr * 2 * chunk_sec
    overlap_bytes = sr * 2 * overlap_sec

    cmd = [
        "ffmpeg", "-i", test_file,
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", str(sr), "-ac", "1",
        "-loglevel", "error", "pipe:1",
    ]
    logger.info(f"測試音檔：{test_file}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    buf = b""

    try:
        while True:
            data = proc.stdout.read(sr * 2)  # 每次讀 1 秒
            if not data:
                break
            buf += data
            if len(buf) >= chunk_bytes:
                audio = (
                    np.frombuffer(buf[:chunk_bytes], dtype=np.int16)
                    .astype(np.float32) / 32768.0
                )
                yield audio
                buf = buf[chunk_bytes - overlap_bytes:]

        # 剩餘不足一個 chunk 的音訊也送出
        if len(buf) >= sr * 2:
            audio = (
                np.frombuffer(buf, dtype=np.int16)
                .astype(np.float32) / 32768.0
            )
            yield audio
    finally:
        proc.terminate()
        proc.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="議會質詢監控系統")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--stage", default="p1", choices=["p1", "p2", "p3", "p4"])
    parser.add_argument("--test-file", help="本機音檔路徑（P1/P2 測試用）")
    parser.add_argument("--test-text", action="store_true", help="文字輸入測試模式（P2/P3/P4）")
    parser.add_argument("--digest", action="store_true", help="產生並寄出本週週報")
    args = parser.parse_args()

    cfg = load_config(args.config)

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} {level} {message}")
    logger.add("council_monitor.log", rotation="1 day", retention="30 days", level="DEBUG")

    if args.digest:
        send_weekly_digest(cfg)
    elif args.stage == "p1":
        run_p1(cfg, test_file=args.test_file)
    elif args.stage == "p2":
        if args.test_text:
            run_p2_text_test(cfg)
        else:
            run_p2(cfg, test_file=args.test_file)
    elif args.stage == "p3":
        if args.test_text:
            run_p3_text_test(cfg, enable_output=False)
        else:
            run_p3(cfg, test_file=args.test_file)
    elif args.stage == "p4":
        if args.test_text:
            run_p3_text_test(cfg, enable_output=True)
        else:
            run_p4(cfg, test_file=args.test_file)
    else:
        logger.error(f"stage={args.stage} 尚未實作")
        sys.exit(1)


if __name__ == "__main__":
    main()
