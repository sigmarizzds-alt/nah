import asyncio
import time
import sqlite3
import aiohttp
from datetime import datetime
from typing import Optional

BASE = "https://api.altare.sh"
HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://altare.sh",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://altare.sh/billing/rewards/afk",
}
DB_PATH = "altare.db"

sessions: dict[str, dict] = {}
lock = asyncio.Lock()

# Kênh Discord để gửi log — được set sau khi bot sẵn sàng
_log_channel = None
_log_queue: asyncio.Queue = None


def set_log_channel(channel):
    global _log_channel, _log_queue
    _log_channel = channel
    _log_queue = asyncio.Queue()
    asyncio.create_task(_log_sender())


async def _log_sender():
    """Gửi log lên Discord channel theo hàng đợi, tránh rate limit."""
    while True:
        try:
            msg = await _log_queue.get()
            if _log_channel:
                try:
                    await _log_channel.send(msg)
                except Exception as e:
                    print(f"[LOG-SEND] lỗi gửi channel: {e}")
            _log_queue.task_done()
            await asyncio.sleep(0.6)  # ~100 msg/phút, an toàn với rate limit Discord
        except Exception:
            await asyncio.sleep(1)


def _conn():
    for _ in range(5):
        try:
            c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15.0)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            return c
        except sqlite3.OperationalError:
            time.sleep(0.5)
    raise RuntimeError("Không thể kết nối DB")


def db_init():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS tokens (
            short TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            added_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lifetime_stats (
            short TEXT PRIMARY KEY,
            total_hb_ok INTEGER DEFAULT 0,
            total_hb_fail INTEGER DEFAULT 0,
            total_uptime_secs INTEGER DEFAULT 0,
            first_seen REAL NOT NULL
        );
        """)


def db_save_token(short: str, token: str, tenant_id: str, added_at: float):
    try:
        with _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO tokens (short, token, tenant_id, added_at) VALUES (?, ?, ?, ?)",
                (short, token, tenant_id, added_at)
            )
            c.execute(
                "INSERT OR IGNORE INTO lifetime_stats (short, first_seen) VALUES (?, ?)",
                (short, added_at)
            )
    except Exception as e:
        print(f"[DB] save_token: {e}")


def db_delete_token(short: str):
    try:
        with _conn() as c:
            c.execute("DELETE FROM tokens WHERE short=?", (short,))
    except Exception as e:
        print(f"[DB] delete_token: {e}")


def db_load_tokens():
    try:
        with _conn() as c:
            return c.execute("SELECT * FROM tokens").fetchall()
    except Exception as e:
        print(f"[DB] load_tokens: {e}")
        return []


def db_update_lifetime(short: str, hb_ok: int, hb_fail: int, uptime_delta: int):
    try:
        with _conn() as c:
            c.execute("""
                INSERT INTO lifetime_stats (short, total_hb_ok, total_hb_fail, total_uptime_secs, first_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(short) DO UPDATE SET
                    total_hb_ok = total_hb_ok + excluded.total_hb_ok,
                    total_hb_fail = total_hb_fail + excluded.total_hb_fail,
                    total_uptime_secs = total_uptime_secs + excluded.total_uptime_secs
            """, (short, hb_ok, hb_fail, uptime_delta, time.time()))
    except Exception as e:
        print(f"[DB] update_lifetime: {e}")


def db_get_lifetime(short: str):
    try:
        with _conn() as c:
            return c.execute("SELECT * FROM lifetime_stats WHERE short=?", (short,)).fetchone()
    except Exception as e:
        print(f"[DB] get_lifetime: {e}")
        return None


def short(token: str) -> str:
    return token[-16:]


def make_headers(token: str) -> dict:
    return {**HEADERS_BASE, "Authorization": token}


async def _do_request(func):
    for attempt in range(5):
        try:
            return await func()
        except (asyncio.TimeoutError, aiohttp.ClientError):
            if attempt < 4:
                await asyncio.sleep(min(3 * (2 ** attempt), 60))
        except Exception:
            if attempt < 4:
                await asyncio.sleep(3)
    return None


async def detect_tenant(http: aiohttp.ClientSession, token: str) -> Optional[tuple]:
    async def _req():
        async with http.get(
            f"{BASE}/api/tenants",
            headers=make_headers(token),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            items = data.get("items", data) if isinstance(data, dict) else data
            if not items:
                return None
            item = items[0]
            tid = item.get("id") or item.get("tenantId")
            if not tid:
                return None
            return (tid,)
    return await _do_request(_req)


async def api_post(http: aiohttp.ClientSession, url: str, token: str) -> tuple:
    async def _req():
        async with http.post(
            url,
            headers=make_headers(token),
            json={},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status in (200, 201, 204):
                return True, None
            text = await r.text()
            return False, f"HTTP {r.status}: {text[:200]}"
    try:
        result = await _do_request(_req)
        if result is None:
            return False, "Request thất bại sau 5 lần thử"
        return result
    except Exception as e:
        return False, str(e)[:200]


def new_session(token: str, tenant_id: str, added_at: float = None) -> dict:
    return {
        "token": token,
        "short": short(token),
        "tenant_id": tenant_id,
        "afk_running": False,
        "afk_task": None,
        "afk_stats_task": None,
        "farm_start": None,
        "hb_ok": 0,
        "hb_fail": 0,
        "hb_last": None,
        "afk_status": "idle",
        "afk_error": None,
        "logs": [],
        "added_at": added_at or time.time(),
        "_last_hb_ok": 0,
        "_last_hb_fail": 0,
        "_last_stat_ts": time.time(),
    }


# Prefix và format cho từng level
_LEVEL_FMT = {
    "info":    ("[{ts}] [{tail}]    {msg}",  "[{ts}] [{tail}]    {msg}"),
    "success": ("[{ts}] [{tail}]  + {msg}",  "`[{ts}] [{tail}]  + {msg}`"),
    "warn":    ("[{ts}] [{tail}]  ! {msg}",  "**[{ts}] [{tail}]  ! {msg}**"),
    "error":   ("[{ts}] [{tail}]  x {msg}",  "**[{ts}] [{tail}]  x {msg}**"),
}


def add_log(s: dict, msg: str, level: str = "info"):
    if not s:
        return
    ts   = datetime.now().strftime("%H:%M:%S")
    tail = s["short"][-8:]

    s["logs"].append({"ts": ts, "msg": msg, "level": level})
    s["logs"] = s["logs"][-200:]

    console_fmt, discord_fmt = _LEVEL_FMT.get(level, _LEVEL_FMT["info"])
    console_line = console_fmt.format(ts=ts, tail=tail, msg=msg)
    discord_line = discord_fmt.format(ts=ts, tail=tail, msg=msg)

    print(console_line)

    if _log_queue is not None:
        try:
            _log_queue.put_nowait(discord_line)
        except asyncio.QueueFull:
            pass  # bỏ qua nếu hàng đợi đầy


def uptime_str(farm_start: Optional[float]) -> str:
    if not farm_start:
        return "--:--:--"
    elapsed = int(time.time() - farm_start)
    h, rem = divmod(max(elapsed, 0), 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def session_snapshot(s: dict) -> dict:
    total_hb = s["hb_ok"] + s["hb_fail"]
    lt = db_get_lifetime(s["short"])
    return {
        "short": s["short"],
        "token": s["token"],
        "token_tail": s["token"][-8:],
        "tenant_id": s["tenant_id"],
        "afk_running": s["afk_running"],
        "farm_start": s["farm_start"],
        "hb_ok": s["hb_ok"],
        "hb_fail": s["hb_fail"],
        "hb_last": s["hb_last"],
        "afk_status": s["afk_status"],
        "afk_error": s["afk_error"],
        "logs": s["logs"],
        "added_at": s["added_at"],
        "success_rate": round(s["hb_ok"] / total_hb * 100, 1) if total_hb else 0.0,
        "uptime": uptime_str(s["farm_start"]),
        "lifetime": {
            "total_hb_ok": lt["total_hb_ok"] if lt else 0,
            "total_hb_fail": lt["total_hb_fail"] if lt else 0,
            "total_uptime_secs": lt["total_uptime_secs"] if lt else 0,
        } if lt else {},
    }
