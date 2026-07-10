import subprocess
import json


def send_email(to: str, subject: str, body: str) -> dict:
    try:
        cmd = ["agently-cli", "message", "+send", "--to", to, "--subject", subject, "--body", body]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"success": False, "error": result.stderr or result.stdout}
        data = json.loads(result.stdout)
        if not data.get("ok"):
            return {"success": False, "error": str(data)}
        # Handle confirmation_required (external email addresses)
        if data.get("data", {}).get("confirmation_required"):
            token = data["data"]["confirmation_token"]
            confirm = subprocess.run(
                cmd + ["--confirmation-token", token],
                capture_output=True, text=True, timeout=30
            )
            if confirm.returncode != 0:
                return {"success": False, "error": confirm.stderr or confirm.stdout}
            cdata = json.loads(confirm.stdout)
            if not cdata.get("ok"):
                return {"success": False, "error": str(cdata)}
        return {"success": True, "message": f"邮件已发送至 {to}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def read_emails(limit: int = 5) -> list:
    try:
        result = subprocess.run(
            ["agently-cli", "message", "+list", "--limit", str(limit)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return [{"error": result.stderr or result.stdout}]
        data = json.loads(result.stdout)
        messages = data.get("data", {}).get("data", [])
        results = []
        for msg in messages:
            results.append({
                "from": msg.get("from", {}).get("email", ""),
                "subject": msg.get("subject", ""),
                "body": msg.get("snippet", ""),
                "is_read": msg.get("is_read", True),
                "message_id": msg.get("message_id", ""),
                "created_at": msg.get("created_at", "")
            })
        return results
    except Exception as e:
        return [{"error": str(e)}]
