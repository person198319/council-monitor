"""
P4 - Email 通知

緊急程度對應：
  立即 → 立即寄出，抄送院長室
  本週 → 立即寄出，只寄主責單位
  本月/追蹤 → 不立即寄，進入週報彙整

SMTP 密碼優先從環境變數 SMTP_PASSWORD 讀取，其次從 config.yaml。
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

from loguru import logger

from analysis.summarizer import SessionSummary
from analysis.dispatcher import DispatchResult

_URGENCY_EMOJI = {
    "立即": "🔴",
    "本週": "🟡",
    "本月": "🟢",
    "追蹤": "⚪",
}


def notify(
    summary: SessionSummary,
    dispatch: DispatchResult,
    session_id: str,
    cfg: dict,
) -> None:
    """
    依緊急程度決定是否立即寄信。
    本月/追蹤 → 只 log，由週報彙整。
    """
    urgency = summary.緊急程度

    if urgency in ("立即", "本週"):
        _send_alert(summary, dispatch, session_id, cfg)
    else:
        logger.info(
            f"緊急程度「{urgency}」→ 不立即通知，將納入週報"
        )


def _send_alert(
    summary: SessionSummary,
    dispatch: DispatchResult,
    session_id: str,
    cfg: dict,
) -> None:
    ecfg = cfg["notification"]["email"]
    recipients = _collect_recipients(summary, dispatch, ecfg)

    if not recipients:
        logger.warning("無有效收件人，跳過寄信（請確認 config.yaml contacts 已填寫）")
        return

    subject = _build_subject(summary)
    html = _build_html(summary, dispatch, session_id)
    _send(subject, html, recipients, ecfg)


def _collect_recipients(
    summary: SessionSummary,
    dispatch: DispatchResult,
    ecfg: dict,
) -> list[str]:
    contacts = ecfg.get("contacts", {})
    recipients: set[str] = set()

    # 主責 + 協辦單位的聯絡人
    for unit in dispatch.primary + dispatch.secondary:
        for email in contacts.get(unit, []):
            if email:
                recipients.add(email)

    # 立即級：抄送院長室
    if summary.緊急程度 == "立即":
        for email in ecfg.get("director_emails", []):
            if email:
                recipients.add(email)

    return list(recipients)


def _build_subject(summary: SessionSummary) -> str:
    mark = _URGENCY_EMOJI.get(summary.緊急程度, "")
    return (
        f"{mark} [議會質詢] {summary.緊急程度}"
        f" ─ {summary.質詢主題}"
    )


def _build_html(
    summary: SessionSummary,
    dispatch: DispatchResult,
    session_id: str,
) -> str:
    mark = _URGENCY_EMOJI.get(summary.緊急程度, "")
    commitments_html = "".join(
        f"<li>{item}</li>" for item in summary.承諾事項
    ) or "<li>（無）</li>"
    todos_html = "".join(
        f"<li>{item}</li>" for item in summary.待辦追蹤
    ) or "<li>（無）</li>"
    numbers_html = "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>"
        for k, v in summary.關鍵數字.items()
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: "Microsoft JhengHei", Arial, sans-serif; color: #333; max-width: 700px; margin: 0 auto; padding: 20px; }}
  h1 {{ color: #1a1a2e; font-size: 20px; border-bottom: 2px solid #e63946; padding-bottom: 8px; }}
  h2 {{ color: #457b9d; font-size: 15px; margin-top: 20px; }}
  .meta-table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  .meta-table td {{ padding: 6px 10px; border: 1px solid #ddd; }}
  .meta-table td:first-child {{ background: #f1f3f4; font-weight: bold; width: 100px; }}
  .summary-box {{ background: #f8f9fa; border-left: 4px solid #457b9d; padding: 12px; margin: 12px 0; }}
  ul {{ margin: 6px 0; padding-left: 20px; }}
  li {{ margin: 4px 0; }}
  .urgency-立即 {{ color: #e63946; font-weight: bold; }}
  .urgency-本週 {{ color: #f4a261; font-weight: bold; }}
  .footer {{ color: #888; font-size: 12px; margin-top: 30px; border-top: 1px solid #eee; padding-top: 10px; }}
</style>
</head>
<body>
<h1>{mark} 議會質詢通知：{summary.質詢主題}</h1>

<table class="meta-table">
  <tr><td>議員</td><td>{summary.議員姓名}</td></tr>
  <tr><td>質詢對象</td><td>{summary.質詢對象}</td></tr>
  <tr><td>緊急程度</td><td><span class="urgency-{summary.緊急程度}">{summary.緊急程度}</span></td></tr>
  <tr><td>情緒張力</td><td>{summary.情緒張力}</td></tr>
  <tr><td>主責單位</td><td>{', '.join(dispatch.primary)}</td></tr>
  <tr><td>協辦單位</td><td>{', '.join(dispatch.secondary) or '無'}</td></tr>
</table>

<h2>質詢摘要</h2>
<div class="summary-box">{summary.質詢摘要}</div>

<h2>局方回應</h2>
<div class="summary-box">{summary.局方回應 or '（本次無明確回應記錄）'}</div>

<h2>承諾事項</h2>
<ul>{commitments_html}</ul>

<h2>待辦追蹤</h2>
<ul>{todos_html}</ul>

{'<h2>關鍵數字</h2><table class="meta-table">' + numbers_html + '</table>' if summary.關鍵數字 else ''}

<div class="footer">
  session ID：{session_id}<br>
  系統自動產生 · 台北市立聯合醫院議會質詢監控系統 · {datetime.now().strftime('%Y-%m-%d %H:%M')}
</div>
</body>
</html>"""


def _send(subject: str, html: str, recipients: list[str], ecfg: dict) -> None:
    password = os.environ.get("SMTP_PASSWORD") or ecfg.get("password", "")
    if not password:
        logger.warning("SMTP_PASSWORD 未設定，跳過寄信")
        logger.info(f"[模擬寄信] 收件人：{recipients}　主旨：{subject}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{ecfg.get('from_name', '')} <{ecfg['from']}>"
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(ecfg["smtp_host"], ecfg["smtp_port"]) as server:
            if ecfg.get("use_tls", True):
                server.starttls()
            server.login(ecfg["username"], password)
            server.sendmail(ecfg["from"], recipients, msg.as_string())
        logger.info(f"Email 寄出｜收件人：{recipients}｜主旨：{subject}")
    except Exception as e:
        logger.error(f"Email 寄送失敗：{e}")
