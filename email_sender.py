import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

SMTP_SERVER = "smtp.163.com"
SMTP_PORT = 465
SENDER = "tingshurain@163.com"
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

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
