"""
P4 - SQLite 存檔 + 逐字稿 txt

資料表：
  sessions     - 每筆質詢主記錄（含影片時間範圍）
  commitments  - 承諾事項（可獨立追蹤狀態）
  dispatches   - 分派記錄（主責/協辦）
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from analysis.summarizer import SessionSummary
from analysis.dispatcher import DispatchResult

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    date            TEXT NOT NULL,
    councilor       TEXT,
    target          TEXT,
    topic           TEXT,
    summary         TEXT,
    response        TEXT,
    urgency         TEXT,
    emotion         TEXT,
    key_numbers     TEXT,   -- JSON
    video_start_sec REAL,
    video_end_sec   REAL,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS commitments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT REFERENCES sessions(id),
    content     TEXT NOT NULL,
    deadline    TEXT,
    status      TEXT DEFAULT '待處理',
    unit        TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS dispatches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT REFERENCES sessions(id),
    unit        TEXT NOT NULL,
    role        TEXT NOT NULL,   -- 主責 / 協辦
    source      TEXT,            -- 規則引擎 / LLM / fallback
    confidence  REAL
);

CREATE INDEX IF NOT EXISTS idx_sessions_date    ON sessions(date);
CREATE INDEX IF NOT EXISTS idx_sessions_urgency ON sessions(urgency);
CREATE INDEX IF NOT EXISTS idx_commit_status    ON commitments(status);
CREATE INDEX IF NOT EXISTS idx_commit_deadline  ON commitments(deadline);
"""

_MIGRATE_SQL = [
    "ALTER TABLE sessions ADD COLUMN video_start_sec REAL",
    "ALTER TABLE sessions ADD COLUMN video_end_sec   REAL",
]


def _get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    # 為既有資料庫補欄位（忽略已存在的錯誤）
    for sql in _MIGRATE_SQL:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


def _deadline(urgency: str) -> str:
    days = {"立即": 3, "本週": 7, "本月": 30, "追蹤": 90}
    return (datetime.now() + timedelta(days=days.get(urgency, 30))).strftime("%Y-%m-%d")


def _session_id(summary: SessionSummary) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = summary.質詢主題[:8].replace(" ", "")
    return f"{ts}_{slug}"


def _fmt(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def save_session(
    summary: SessionSummary,
    dispatch: DispatchResult,
    session: dict,
    cfg: dict,
) -> str:
    """
    存入 SQLite 並寫逐字稿 txt。
    session 為 SessionManager._end() 回傳的完整 dict。
    回傳 session_id。
    """
    db_path = cfg["storage"]["db_path"]
    transcript_dir = cfg["storage"]["transcript_dir"]
    youtube_url = cfg.get("youtube", {}).get("url", "")
    Path(transcript_dir).mkdir(exist_ok=True)

    sid = _session_id(summary)
    now = datetime.now().isoformat()
    today = now[:10]

    video_start = session.get("video_start_sec", 0.0)
    video_end   = session.get("video_end_sec",   0.0)

    conn = _get_conn(db_path)
    try:
        conn.execute(
            """INSERT INTO sessions
               (id, date, councilor, target, topic, summary, response,
                urgency, emotion, key_numbers,
                video_start_sec, video_end_sec, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sid, today,
                summary.議員姓名, summary.質詢對象,
                summary.質詢主題, summary.質詢摘要,
                summary.局方回應, summary.緊急程度,
                summary.情緒張力,
                json.dumps(summary.關鍵數字, ensure_ascii=False),
                video_start, video_end,
                now,
            ),
        )

        deadline = _deadline(summary.緊急程度)
        primary_unit = dispatch.primary[0] if dispatch.primary else "未分派"
        for item in summary.承諾事項:
            conn.execute(
                """INSERT INTO commitments
                   (session_id, content, deadline, status, unit, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (sid, item, deadline, "待處理", primary_unit, now),
            )

        for unit in dispatch.primary:
            conn.execute(
                "INSERT INTO dispatches (session_id, unit, role, source, confidence)"
                " VALUES (?,?,?,?,?)",
                (sid, unit, "主責", dispatch.source, dispatch.confidence),
            )
        for unit in dispatch.secondary:
            conn.execute(
                "INSERT INTO dispatches (session_id, unit, role, source, confidence)"
                " VALUES (?,?,?,?,?)",
                (sid, unit, "協辦", dispatch.source, dispatch.confidence),
            )

        conn.commit()

        # 逐字稿 txt
        txt_path = os.path.join(transcript_dir, f"{today}_{sid}.txt")
        timestamped = session.get(
            "timestamped_transcript",
            session.get("transcript", ""),
        )

        jump_url = ""
        if youtube_url and video_start:
            from pipeline.audio_stream import get_youtube_jump_url
            jump_url = get_youtube_jump_url(youtube_url, video_start)

        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"# {summary.質詢主題}\n")
            f.write(f"日期：{today}　議員：{summary.議員姓名}\n")
            f.write(f"對象：{summary.質詢對象}　緊急：{summary.緊急程度}\n")
            f.write(f"影片時間：{_fmt(video_start)} – {_fmt(video_end)}\n")
            if jump_url:
                f.write(f"YouTube 連結：{jump_url}\n")
            f.write("\n")
            f.write(timestamped)

        logger.info(f"存檔完成｜session_id：{sid}｜逐字稿：{txt_path}")
        return sid

    finally:
        conn.close()


def query_pending(db_path: str, unit: str | None = None) -> list[sqlite3.Row]:
    """查詢未完成承諾事項（供手動查詢或週報使用）"""
    conn = _get_conn(db_path)
    try:
        sql = """
            SELECT c.id, c.content, c.deadline, c.status, c.unit,
                   s.topic, s.councilor, s.date
            FROM commitments c
            JOIN sessions s ON c.session_id = s.id
            WHERE c.status != '已完成'
        """
        params = []
        if unit:
            sql += " AND c.unit = ?"
            params.append(unit)
        sql += " ORDER BY c.deadline"
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def mark_done(db_path: str, commitment_id: int) -> None:
    """將承諾事項標記為已完成"""
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "UPDATE commitments SET status='已完成', updated_at=? WHERE id=?",
            (datetime.now().isoformat(), commitment_id),
        )
        conn.commit()
        logger.info(f"承諾 #{commitment_id} 標記為已完成")
    finally:
        conn.close()
