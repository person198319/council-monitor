"""
P4 - 週報生成與寄送

每週五執行：python main.py --digest
彙整本週所有質詢 session，標出逾期承諾，寄給局長/院長。
"""
import sqlite3
from datetime import datetime, timedelta

from loguru import logger

from output.notifier import _send
from output.storage import _get_conn


def send_weekly_digest(cfg: dict) -> None:
    """生成本週週報並寄送"""
    db_path = cfg["storage"]["db_path"]
    ecfg = cfg["notification"]["email"]
    recipients = [r for r in ecfg.get("digest_recipients", []) if r]

    if not recipients:
        logger.warning("週報收件人未設定（digest_recipients 為空），跳過")
        return

    html = _build_digest_html(db_path)
    subject = f"📋 議會質詢週報 {_week_range()}"
    _send(subject, html, recipients, ecfg)
    logger.info(f"週報已寄出｜收件人：{recipients}")


def _week_range() -> str:
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    return f"{monday.strftime('%m/%d')} – {today.strftime('%m/%d')}"


def _build_digest_html(db_path: str) -> str:
    conn = _get_conn(db_path)
    try:
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        # 本週 sessions
        sessions = conn.execute(
            """
            SELECT s.*,
                   GROUP_CONCAT(CASE WHEN d.role='主責' THEN d.unit END) AS primary_units
            FROM sessions s
            LEFT JOIN dispatches d ON s.id = d.session_id
            WHERE s.date >= ?
            GROUP BY s.id
            ORDER BY
                CASE s.urgency
                    WHEN '立即' THEN 1 WHEN '本週' THEN 2
                    WHEN '本月' THEN 3 ELSE 4 END,
                s.date DESC
            """,
            (week_ago,),
        ).fetchall()

        # 逾期未結承諾
        overdue = conn.execute(
            """
            SELECT c.id, c.content, c.deadline, c.unit, s.topic, s.councilor
            FROM commitments c
            JOIN sessions s ON c.session_id = s.id
            WHERE c.status != '已完成'
              AND c.deadline < date('now')
            ORDER BY c.deadline
            """,
        ).fetchall()

        # 待處理承諾（未逾期）
        pending = conn.execute(
            """
            SELECT c.id, c.content, c.deadline, c.unit, s.topic
            FROM commitments c
            JOIN sessions s ON c.session_id = s.id
            WHERE c.status = '待處理'
              AND c.deadline >= date('now')
            ORDER BY c.deadline
            """,
        ).fetchall()

    finally:
        conn.close()

    return _render_html(sessions, overdue, pending)


def _render_html(sessions, overdue, pending) -> str:
    week = _week_range()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 逾期區塊
    overdue_html = ""
    if overdue:
        rows = "".join(
            f"""<tr>
              <td style="color:#e63946">{r['deadline']}</td>
              <td>{r['content']}</td>
              <td>{r['unit']}</td>
              <td>{r['topic']}</td>
              <td>{r['councilor']}</td>
            </tr>"""
            for r in overdue
        )
        overdue_html = f"""
<h2 style="color:#e63946">⚠️ 逾期未結 {len(overdue)} 件</h2>
<table class="data-table">
<tr><th>截止日</th><th>承諾事項</th><th>負責單位</th><th>質詢主題</th><th>議員</th></tr>
{rows}
</table>"""

    # 待辦區塊
    pending_html = ""
    if pending:
        rows = "".join(
            f"""<tr>
              <td>{r['deadline']}</td>
              <td>{r['content']}</td>
              <td>{r['unit']}</td>
              <td>{r['topic']}</td>
            </tr>"""
            for r in pending
        )
        pending_html = f"""
<h2>📋 待辦追蹤 {len(pending)} 件</h2>
<table class="data-table">
<tr><th>截止日</th><th>承諾事項</th><th>負責單位</th><th>質詢主題</th></tr>
{rows}
</table>"""

    # 本週質詢列表
    urgency_mark = {"立即": "🔴", "本週": "🟡", "本月": "🟢", "追蹤": "⚪"}
    session_rows = "".join(
        f"""<tr>
          <td>{s['date']}</td>
          <td>{urgency_mark.get(s['urgency'], '')} {s['urgency']}</td>
          <td>{s['councilor']}</td>
          <td>{s['topic']}</td>
          <td>{s['primary_units'] or '未分派'}</td>
          <td>{s['emotion']}</td>
        </tr>"""
        for s in sessions
    )
    sessions_html = f"""
<h2>本週質詢紀錄 {len(sessions)} 件</h2>
<table class="data-table">
<tr><th>日期</th><th>緊急</th><th>議員</th><th>主題</th><th>主責單位</th><th>情緒</th></tr>
{session_rows or '<tr><td colspan="6">本週無質詢紀錄</td></tr>'}
</table>"""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: "Microsoft JhengHei", Arial, sans-serif; color: #333;
         max-width: 900px; margin: 0 auto; padding: 20px; }}
  h1 {{ color: #1a1a2e; border-bottom: 2px solid #457b9d; padding-bottom: 8px; }}
  h2 {{ color: #457b9d; margin-top: 28px; }}
  .data-table {{ border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 14px; }}
  .data-table th {{ background: #457b9d; color: white; padding: 8px 10px; text-align: left; }}
  .data-table td {{ padding: 7px 10px; border-bottom: 1px solid #ddd; }}
  .data-table tr:hover {{ background: #f5f5f5; }}
  .footer {{ color: #888; font-size: 12px; margin-top: 30px; border-top: 1px solid #eee; padding-top: 10px; }}
</style>
</head>
<body>
<h1>📋 議會質詢週報　{week}</h1>
{overdue_html}
{pending_html}
{sessions_html}
<div class="footer">系統自動產生 · 台北市立聯合醫院議會質詢監控系統 · {now}</div>
</body>
</html>"""
