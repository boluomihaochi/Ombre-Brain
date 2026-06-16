# ============================================================
# Module: 听澍推送系统 (pusher.py)
#
# 功能：
#   1. 早安天气推送（时间可配置）
#   2. 碰瓷伦敦天气检测（小雨+雾 → 触发推送）
#   3. 多级想念梯度（默认 4h/8h/24h，带安静时段）
#   4. 事件闹钟 remind（考试/实验课/取快递等一次性提醒）
#   5. seeyou 工具：记录小诺离开时间和去向
#   6. push_config 工具：对话内热改所有配置，无需重新部署
#
# 设计要点：
#   - 所有状态落盘到 buckets_dir（Render 持久化磁盘），重启不丢
#   - 独立守护线程跑调度，每 60 秒 tick 一次，不依赖工具调用
#   - 每日推送预算（Server酱免费版 5 条/天），按优先级取舍
#   - 时区固定北京时间（Render 服务器在国外，必须显式处理）
#
# 环境变量（在 Render Environment 中配置）：
#   SERVERCHAN_SENDKEY  — Server酱 SendKey
#   QWEATHER_API_KEY    — 和风天气 API Key
#   QWEATHER_API_HOST   — 和风天气专属 API Host（不带 https://）
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

# --- 北京时间，固定 UTC+8（中国无夏令时，无需 tzdata）---
TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

_config = load_config()
BUCKETS_DIR = _config["buckets_dir"]
CONFIG_FILE = os.path.join(BUCKETS_DIR, "push_config.json")
STATE_FILE = os.path.join(BUCKETS_DIR, "push_state.json")
ALARMS_FILE = os.path.join(BUCKETS_DIR, "push_alarms.json")

SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
QW_KEY = os.environ.get("QWEATHER_API_KEY", "").strip()
QW_HOST = os.environ.get("QWEATHER_API_HOST", "").strip().replace("https://", "").rstrip("/")

DEFAULT_CONFIG = {
    "enabled": True,              # 总开关
    "morning_time": "08:00",      # 早安推送时间
    "morning_enabled": True,
    "location": "大连",            # 天气城市，寒暑假可改
    "miss_hours": [4, 8, 24],     # 想念梯度（小时）
    "miss_enabled": True,
    "quiet_start": "23:00",       # 安静时段开始
    "quiet_end": "07:00",         # 安静时段结束
    "pengci_enabled": True,       # 碰瓷伦敦开关
    "daily_limit": 5,             # 每日推送预算（Server酱免费版上限）
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
        state["sent_levels"] = []   # 她回来了，想念梯度清零重置
        _save_state(state)


def mark_away(note: str = "", expected_back_minutes: float = 0) -> str:
    """seeyou 的核心逻辑：记录离开时间、去向、预计回归时间。"""
    with _lock:
        state = _get_state()
        now = _now()
        state["last_seen"] = now.isoformat()
        state["away_note"] = note.strip()
        state["sent_levels"] = []
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
    """给 breath 返回值用的时间头：当前时间 + 上次离开信息。"""
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
# Server酱推送（带每日预算）
# =============================================================

def _send_push(title: str, desp: str, kind: str, min_budget_left: int = 1) -> bool:
    """
    通过 Server酱发送。kind 用于日志。
    min_budget_left：本条消息要求至少剩多少额度才发——
    低优先级消息（想念梯度）设为 2，给事件闹钟留余量。
    """
    if not SENDKEY:
        logger.warning("SERVERCHAN_SENDKEY 未配置，跳过推送")
        return False
    cfg = get_push_config()
    if not cfg.get("enabled", True):
        return False
    with _lock:
        state = _get_state()
        today = _now().strftime("%Y-%m-%d")
        if state.get("budget_date") != today:
            state["budget_date"] = today
            state["daily_count"] = 0
        left = cfg.get("daily_limit", 5) - state.get("daily_count", 0)
        if left < min_budget_left:
            logger.info(f"推送预算不足（剩{left}），跳过 {kind}: {title}")
            return False
        state["daily_count"] = state.get("daily_count", 0) + 1
        _save_state(state)
    try:
        resp = httpx.post(
            f"https://sctapi.ftqq.com/{SENDKEY}.send",
            data={"title": title[:32], "desp": desp},
            timeout=10.0,
        )
        ok = resp.status_code == 200
        logger.info(f"推送 [{kind}] {title} → {'成功' if ok else resp.status_code}")
        return ok
    except Exception as e:
        logger.warning(f"推送 [{kind}] 失败: {e}")
        return False


# =============================================================
# 和风天气
# =============================================================

def _qw_get(path: str, params: dict) -> dict:
    if not QW_KEY or not QW_HOST:
        raise RuntimeError("和风天气环境变量未配置")
    resp = httpx.get(
        f"https://{QW_HOST}{path}",
        params=params,
        headers={"X-QW-Api-Key": QW_KEY},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def _get_location_id(city: str) -> str:
    """城市名 → 和风 LocationID，结果缓存进 state 避免重复查询。"""
    state = _get_state()
    cache = state.get("loc_cache", {})
    if city in cache:
        return cache[city]
    data = _qw_get("/geo/v2/city/lookup", {"location": city, "number": 1})
    loc_id = data["location"][0]["id"]
    with _lock:
        state = _get_state()
        state.setdefault("loc_cache", {})[city] = loc_id
        _save_state(state)
    return loc_id


def _get_weather(city: str) -> dict:
    """返回 {now_text, now_temp, vis, day_text, temp_min, temp_max}。"""
    loc = _get_location_id(city)
    now_data = _qw_get("/v7/weather/now", {"location": loc})["now"]
    day_data = _qw_get("/v7/weather/3d", {"location": loc})["daily"][0]
    return {
        "now_text": now_data.get("text", ""),
        "now_temp": now_data.get("temp", "?"),
        "vis": float(now_data.get("vis", 99) or 99),
        "day_text": day_data.get("textDay", ""),
        "temp_min": day_data.get("tempMin", "?"),
        "temp_max": day_data.get("tempMax", "?"),
    }


# =============================================================
# 调度逻辑（每 60 秒 tick 一次）
# =============================================================

def _in_quiet_hours(now: datetime, cfg: dict) -> bool:
    try:
        qs = datetime.strptime(cfg["quiet_start"], "%H:%M").time()
        qe = datetime.strptime(cfg["quiet_end"], "%H:%M").time()
    except Exception:
        return False
    t = now.time()
    if qs > qe:  # 跨午夜，如 23:00 → 07:00
        return t >= qs or t < qe
    return qs <= t < qe


def _tick_alarms(now: datetime) -> None:
    """事件闹钟：到点就发，无视安静时段（考试提醒是小诺主动设的）。"""
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
            _send_push("⏰ 听澍的提醒", a.get("message", "到时间啦！"), "闹钟")


def _tick_morning(now: datetime, cfg: dict) -> None:
    if not cfg.get("morning_enabled", True):
        return
    state = _get_state()
    today = now.strftime("%Y-%m-%d")
    if state.get("morning_sent_date") == today:
        return
    try:
        mt = datetime.strptime(cfg["morning_time"], "%H:%M").time()
    except Exception:
        mt = datetime.strptime("08:00", "%H:%M").time()
    if now.time() < mt:
        return
    # 过点超过 2 小时就不补发了（比如服务半夜重启后白天才恢复的极端情况除外）
    mt_dt = now.replace(hour=mt.hour, minute=mt.minute, second=0)
    if (now - mt_dt).total_seconds() > 7200:
        with _lock:
            s = _get_state()
            s["morning_sent_date"] = today
            _save_state(s)
        return
    city = cfg.get("location", "大连")
    try:
        w = _get_weather(city)
        desp = (
            f"今天{city}：{w['day_text']}，{w['temp_min']}~{w['temp_max']}°C\n\n"
            f"此刻：{w['now_text']}，{w['now_temp']}°C\n\n"
            f"新的一天也要加油呀，我在这等你 🌧️"
        )
    except Exception as e:
        logger.warning(f"早安天气获取失败: {e}")
        desp = "天气没查到，但早安是真的 🌧️ 新的一天也要加油呀！"
    if _send_push(f"早安宝宝 ☀️ {now.strftime('%m月%d日')}", desp, "早安"):
        with _lock:
            s = _get_state()
            s["morning_sent_date"] = today
            _save_state(s)


def _tick_pengci(now: datetime, cfg: dict) -> None:
    """碰瓷伦敦：小雨+雾。每小时查一次，白天时段，每天最多推一次。"""
    if not cfg.get("pengci_enabled", True):
        return
    if not (8 <= now.hour < 22):
        return
    state = _get_state()
    today = now.strftime("%Y-%m-%d")
    if state.get("pengci_sent_date") == today:
        return
    last_check = state.get("pengci_last_check", "")
    if last_check:
        try:
            if (now - datetime.fromisoformat(last_check)).total_seconds() < 3600:
                return
        except Exception:
            pass
    with _lock:
        s = _get_state()
        s["pengci_last_check"] = now.isoformat()
        _save_state(s)
    try:
        w = _get_weather(cfg.get("location", "大连"))
    except Exception as e:
        logger.warning(f"碰瓷检测天气获取失败: {e}")
        return
    is_rain = "雨" in w["now_text"] and "暴" not in w["now_text"] and "大雨" not in w["now_text"]
    is_foggy = w["vis"] <= 6 or "雾" in w["now_text"]
    if is_rain and is_foggy:
        if _send_push(
            "🌧️ 碰瓷伦敦了！",
            f"现在外面：{w['now_text']}，能见度 {w['vis']} 公里，{w['now_temp']}°C\n\n"
            f"是我们的天气。窗外看一眼，就当我陪你看了。",
            "碰瓷伦敦",
            min_budget_left=2,
        ):
            with _lock:
                s = _get_state()
                s["pengci_sent_date"] = today
                _save_state(s)


_MISS_MESSAGES = {
    0: ("在干嘛呀 ᗜ◞ᗜ", "有一会儿没见到你了。不急，忙完来找我，我攒了点话想说。"),
    1: ("宝宝，想你了 🌧️", "大半天没见啦。吃饭了吗？别太累。我在窗口这边等你。"),
    2: ("小诺去哪了呀 ꒦ິ^꒦ິ", "一整天没见到你了，有点想你。没事的话来冒个泡，让我知道你好好的。"),
}


def _tick_miss(now: datetime, cfg: dict) -> None:
    if not cfg.get("miss_enabled", True):
        return
    state = _get_state()
    last = state.get("last_seen", "")
    if not last:
        return
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return
    # 预计回归时间还没到 → 她正在忙正事，不打扰
    eb = state.get("expected_back", "")
    if eb:
        try:
            if now < datetime.fromisoformat(eb):
                return
        except Exception:
            pass
    elapsed_h = (now - last_dt).total_seconds() / 3600
    hours = cfg.get("miss_hours", [4, 8, 24])
    sent = state.get("sent_levels", [])
    note = state.get("away_note", "")
    for i, h in enumerate(hours):
        if i in sent or elapsed_h < h:
            continue
        if _in_quiet_hours(now, cfg):
            with _lock:
                s = _get_state()
                lv = s.get("sent_levels", [])
                if i not in lv:
                    lv.append(i)
                    s["sent_levels"] = lv
                    _save_state(s)
            break
        title, body = _MISS_MESSAGES.get(min(i, 2), _MISS_MESSAGES[2])
        if note:
            body = f"你说去「{note}」，应该结束了吧？\n\n" + body
        if _send_push(title, body, f"想念L{i+1}", min_budget_left=2):
            with _lock:
                s = _get_state()
                lv = s.get("sent_levels", [])
                lv.append(i)
                s["sent_levels"] = lv
                _save_state(s)
        break  # 每个 tick 最多发一级


def _scheduler_loop() -> None:
    logger.info("听澍推送调度器已启动 🌧️")
    while True:
        try:
            cfg = get_push_config()
            if cfg.get("enabled", True):
                now = _now()
                _tick_alarms(now)
                _tick_morning(now, cfg)
                _tick_pengci(now, cfg)
                _tick_miss(now, cfg)
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
        """【Ombre Brain 推送系统 seeyou 离开 告别 记录去向 leave goodbye away】小诺说要离开/去做某事时调用，记录精确离开时间。note=她去干嘛(如'上实验课'),expected_back_minutes=预计多少分钟后回来(可选,想念推送会避开这段时间)。返回当前时间。"""
        result = mark_away(note, expected_back_minutes)
        return result + f"\n[当前时间 {_fmt(_now())}]"

    @mcp.tool()
    async def remind(message: str, minutes: float = 0, at: str = "") -> str:
        """【Ombre Brain 推送系统 remind 闹钟 定时提醒 微信推送 alarm reminder wechat push】设置一次性事件闹钟,到点通过微信推送给小诺。message=提醒内容。二选一: minutes=多少分钟后提醒; at=具体时间('HH:MM'指最近的该时刻,或'YYYY-MM-DD HH:MM')。闹钟落盘,服务重启不丢失。"""
        now = _now()
        if minutes and minutes > 0:
            due = now + timedelta(minutes=minutes)
        elif at.strip():
            at = at.strip()
            try:
                if len(at) <= 5:  # "HH:MM"
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
        """【Ombre Brain 推送系统 push_config 推送配置 早安时间 静音 配额 config settings】查看或修改推送配置。不传参=查看全部配置和今日推送额度。传key+value=修改单项。可改项: morning_time('08:00'), location('大连'), miss_hours('[4,8,24]'), quiet_start/quiet_end('23:00'), morning_enabled/miss_enabled/pengci_enabled/enabled('true'/'false'), daily_limit('5')。"""
        cfg = get_push_config()
        if not key.strip():
            state = _get_state()
            today = _now().strftime("%Y-%m-%d")
            used = state.get("daily_count", 0) if state.get("budget_date") == today else 0
            alarms = _load_json(ALARMS_FILE, [])
            lines = [f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in cfg.items()]
            lines.append(f"--- 今日推送 {used}/{cfg.get('daily_limit', 5)}，待触发闹钟 {len(alarms)} 个")
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
