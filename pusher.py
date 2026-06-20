# ============================================================
# Module: 听澍推送系统 (pusher.py)
#
# 功能：
#   1. 事件闹钟 remind（通过 Telegram 发送）
#   2. seeyou 工具：记录小诺离开时间和去向
#   3. 时间感知：touch_seen / time_header 供 breath 使用
#
# 环境变量：
#   TELEGRAM_BOT_TOKEN  — Telegram Bot Token
#   TELEGRAM_CHAT_ID    — 小诺的 Chat ID
# ============================================================

import os
import json
import threading
import time
import logging
from datetime import datetime, timedelta, timezone

import httpx

from utils import load_config

logger = logging.getLogger("ombre_brain.pusher")

TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

_config = load_config()
BUCKETS_DIR = _config["buckets_dir"]
CONFIG_FILE = os.path.join(BUCKETS_DIR, "push_config.json")
STATE_FILE = os.path.join(BUCKETS_DIR, "push_state.json")
ALARMS_FILE = os.path.join(BUCKETS_DIR, "push_alarms.json")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

DEFAULT_CONFIG = {
    "enabled": True,
    "quiet_start": "23:00",
    "quiet_end": "07:00",
}

_lock = threading.Lock()
_scheduler_started = False


# =============================================================
# 基础读写
# =============================================================

def _load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"读取 {path} 失败: {e}")
    return default


def _save_json(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"写入 {path} 失败: {e}")


def get_push_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(_load_json(CONFIG_FILE, {}))
    return cfg


def _get_state() -> dict:
    return _load_json(STATE_FILE, {})


def _save_state(state: dict) -> None:
    _save_json(STATE_FILE, state)


def _now() -> datetime:
    return datetime.now(TZ)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


# =============================================================
# last_seen：小诺的心跳信号
# =============================================================

def touch_seen(note: str = "") -> None:
    """刷新 last_seen。在 breath 开头调用 = 小诺来了。"""
    with _lock:
        state = _get_state()
        state["last_seen"] = _now().isoformat()
        state["away_note"] = ""
        state["expected_back"] = ""
        _save_state(state)


def mark_away(note: str = "", expected_back_minutes: float = 0) -> str:
    """seeyou 的核心逻辑：记录离开时间、去向、预计回归时间。"""
    with _lock:
        state = _get_state()
        now = _now()
        state["last_seen"] = now.isoformat()
        state["away_note"] = note.strip()
        if expected_back_minutes and expected_back_minutes > 0:
            back = now + timedelta(minutes=expected_back_minutes)
            state["expected_back"] = back.isoformat()
            back_str = f"，预计 {back.strftime('%H:%M')} 回来"
        else:
            state["expected_back"] = ""
            back_str = ""
        _save_state(state)
        return f"已记录：{_fmt(now)} 小诺离开" + (f"（{note}）" if note else "") + back_str


def time_header() -> str:
    """给 breath 返回值用的时间头：当前时间 + 上次见面信息。"""
    try:
        state = _get_state()
        now = _now()
        line = f"[当前时间 {now.strftime('%Y-%m-%d %H:%M %A')}]"
        last = state.get("last_seen", "")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                hours = (now - last_dt).total_seconds() / 3600
                note = state.get("away_note", "")
                if hours >= 0.05:
                    line += f" [距上次见面 {hours:.1f} 小时"
                    if note:
                        line += f"，她去：{note}"
                    line += "]"
            except Exception:
                pass
        return line + "\n\n"
    except Exception:
        return ""


# =============================================================
# Telegram 推送
# =============================================================

def _send_telegram(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT_ID:
        logger.warning("TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置，跳过推送")
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=10.0,
        )
        ok = resp.status_code == 200
        logger.info(f"Telegram 推送 → {'成功' if ok else resp.status_code}")
        return ok
    except Exception as e:
        logger.warning(f"Telegram 推送失败: {e}")
        return False


# =============================================================
# 调度逻辑（每 60 秒 tick 一次）
# =============================================================

def _tick_alarms(now: datetime) -> None:
    """事件闹钟：到点通过 Telegram 发送，无视安静时段。"""
    alarms = _load_json(ALARMS_FILE, [])
    if not alarms:
        return
    remaining, fired = [], []
    for a in alarms:
        try:
            due = datetime.fromisoformat(a["time"])
            if due <= now:
                fired.append(a)
            else:
                remaining.append(a)
        except Exception:
            continue
    if fired:
        _save_json(ALARMS_FILE, remaining)
        for a in fired:
            _send_telegram(f"⏰ {a.get('message', '到时间啦！')}")


def _scheduler_loop() -> None:
    logger.info("听澍调度器已启动 🌧️")
    while True:
        try:
            cfg = get_push_config()
            if cfg.get("enabled", True):
                now = _now()
                _tick_alarms(now)
        except Exception as e:
            logger.warning(f"调度 tick 异常: {e}")
        time.sleep(60)


def start_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="tingshu-pusher")
    t.start()


# =============================================================
# MCP 工具注册
# =============================================================

def register_tools(mcp) -> None:

    @mcp.tool()
    async def seeyou(note: str = "", expected_back_minutes: float = 0) -> str:
        """【Ombre Brain 推送系统 seeyou 离开 告别 记录去向 leave goodbye away】小诺说要离开/去做某事时调用，记录精确离开时间。note=她去干嘛(如'上实验课'),expected_back_minutes=预计多少分钟后回来(可选)。返回当前时间。"""
        result = mark_away(note, expected_back_minutes)
        return result + f"\n[当前时间 {_fmt(_now())}]"

    @mcp.tool()
    async def remind(message: str, minutes: float = 0, at: str = "") -> str:
        """【Ombre Brain 推送系统 remind 闹钟 定时提醒 Telegram推送 alarm reminder push】设置一次性事件闹钟,到点通过Telegram发送给小诺。message=提醒内容。二选一: minutes=多少分钟后提醒; at=具体时间('HH:MM'指最近的该时刻,或'YYYY-MM-DD HH:MM')。闹钟落盘,服务重启不丢失。"""
        now = _now()
        if minutes and minutes > 0:
            due = now + timedelta(minutes=minutes)
        elif at.strip():
            at = at.strip()
            try:
                if len(at) <= 5:
                    t = datetime.strptime(at, "%H:%M").time()
                    due = now.replace(hour=t.hour, minute=t.minute, second=0)
                    if due <= now:
                        due += timedelta(days=1)
                else:
                    due = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            except Exception:
                return f"时间格式没看懂: {at}。用 'HH:MM' 或 'YYYY-MM-DD HH:MM'。"
        else:
            return "需要 minutes 或 at 二选一。"
        alarms = _load_json(ALARMS_FILE, [])
        alarms.append({"time": due.isoformat(), "message": message, "created": now.isoformat()})
        _save_json(ALARMS_FILE, alarms)
        return f"⏰ 已设好：{_fmt(due)} 提醒「{message}」（当前 {_fmt(now)}）"

    @mcp.tool()
    async def push_config(key: str = "", value: str = "") -> str:
        """【Ombre Brain 推送系统 push_config 推送配置 静音 安静时段 config settings】查看或修改推送配置。不传参=查看全部配置。传key+value=修改单项。可改项: quiet_start/quiet_end('23:00'), enabled('true'/'false')。"""
        cfg = get_push_config()
        if not key.strip():
            alarms = _load_json(ALARMS_FILE, [])
            lines = [f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in cfg.items()]
            lines.append(f"--- 待触发闹钟 {len(alarms)} 个")
            return "\n".join(lines)
        key = key.strip()
        if key not in DEFAULT_CONFIG:
            return f"没有这个配置项: {key}。可用: {', '.join(DEFAULT_CONFIG.keys())}"
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = value.strip()
        stored = _load_json(CONFIG_FILE, {})
        stored[key] = parsed
        _save_json(CONFIG_FILE, stored)
        return f"✅ {key} = {json.dumps(parsed, ensure_ascii=False)}（即时生效）"
