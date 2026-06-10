import smtplib
import os
import imaplib
import email as _email_lib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from datetime import datetime

SMTP_SERVER = "smtp.163.com"
SMTP_PORT = 465
SENDER = "tingshurain@163.com"
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

OUTLOOK_SERVER = "imap-mail.outlook.com"
OUTLOOK_SENDER = "tingshurain@outlook.com"
OUTLOOK_PASSWORD = os.environ.get("OUTLOOK_PASSWORD", "")

def send_email(to: str, subject: str, body: str) -> dict:
    try:
        msg = MIMEMultipart()
        msg["From"] = SENDER
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SENDER, SMTP_PASSWORD)
            server.sendmail(SENDER, to, msg.as_string())

        return {"success": True, "message": f"邮件已发送至 {to}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def read_emails(limit: int = 5) -> list:
    try:
        mail = imaplib.IMAP4_SSL(OUTLOOK_SERVER)
        mail.login(OUTLOOK_SENDER, OUTLOOK_PASSWORD)
        typ, data = mail.select("INBOX", readonly=True)
        if typ != "OK":
            return [{"error": f"选择收件箱失败: {typ} {data}"}]
        _, search_data = mail.search(None, "ALL")
        ids = search_data[0].split()
        ids = ids[-limit:][::-1]
        results = []
        for uid in ids:
            _, msg_data = mail.fetch(uid, "(RFC822)")
            msg = _email_lib.message_from_bytes(msg_data[0][1])
            subject, enc = decode_header(msg["Subject"])[0]
            if isinstance(subject, bytes):
                subject = subject.decode(enc or "utf-8", errors="replace")
            sender = msg.get("From", "")
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
            results.append({"from": sender, "subject": subject, "body": body[:500]})
        mail.logout()
        return results
    except Exception as e:
        return [{"error": str(e)}]
