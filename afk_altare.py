# Tool: AFK Altare Auto-Farmer v2.0
# Yêu cầu: pip install requests colorama
# Cách dùng: Điền thông tin vào afk_config.json rồi chạy python afk_altare.py

import requests
import time
import threading
import sys
import json
import os
from datetime import datetime, timezone
from colorama import init, Fore, Style

init(autoreset=True)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "afk_config.json")
BASE = "https://altare.sh"

DEFAULT_CONFIG = {
    "token": "",
    "tenant_id": "",
    "discord_webhook": "",
    "heartbeat_interval": 30,
    "stats_interval": 60,
    "notify_interval_seconds": 10
}

BANNER = f"""
{Fore.MAGENTA}╔══════════════════════════════════════════╗
║       AFK ALTARE AUTO FARMER v2.0        ║
║         altare.sh — Credit Farmer        ║
╚══════════════════════════════════════════╝{Style.RESET_ALL}
"""

running = True
session_start = None
credits_start = 0
total_credits = 0
heartbeat_ok = 0
heartbeat_fail = 0


def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"{Fore.RED}[!] Không tìm thấy afk_config.json! Tạo file mẫu...{Style.RESET_ALL}")
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        print(f"{Fore.YELLOW}[!] Điền token và discord_webhook vào afk_config.json rồi chạy lại.{Style.RESET_ALL}")
        sys.exit(0)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            data.setdefault(k, v)
        return data


def log(msg, color=Fore.WHITE):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{Fore.CYAN}[{ts}]{Style.RESET_ALL} {color}{msg}{Style.RESET_ALL}")


def make_headers(token, tenant_id=""):
    h = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://altare.sh",
        "Referer": "https://altare.sh/billing",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    if tenant_id:
        h["altare-selected-tenant-id"] = tenant_id
    return h


def auto_detect_tenant(headers):
    try:
        r = requests.get(f"{BASE}/api/tenants", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            if items:
                tid = items[0].get("id") or items[0].get("tenantId")
                if tid:
                    return tid
    except Exception as e:
        log(f"auto_detect_tenant lỗi: {e}", Fore.RED)
    return None


def get_rewards(headers, tenant_id):
    try:
        r = requests.get(f"{BASE}/api/tenants/{tenant_id}/rewards", headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None


def get_balance(headers, tenant_id):
    try:
        r = requests.get(f"{BASE}/api/tenants", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            for item in items:
                if item.get("id") == tenant_id:
                    cents = item.get("creditsCents")
                    if cents is not None:
                        return round(cents / 100, 2)
            if items:
                cents = items[0].get("creditsCents")
                if cents is not None:
                    return round(cents / 100, 2)
    except:
        pass
    return None


def start_afk_session(headers, tenant_id):
    try:
        r = requests.post(f"{BASE}/api/tenants/{tenant_id}/rewards/afk/start", headers=headers, json={}, timeout=10)
        if r.status_code in (200, 201, 204):
            log(f"Start AFK OK ✓: {r.text[:80]}", Fore.GREEN)
            return True
        else:
            log(f"Start AFK {r.status_code}: {r.text[:80]}", Fore.YELLOW)
    except Exception as e:
        log(f"Start AFK lỗi: {e}", Fore.RED)
    return False


def stop_afk_session(headers, tenant_id):
    try:
        r = requests.post(f"{BASE}/api/tenants/{tenant_id}/rewards/afk/stop", headers=headers, json={}, timeout=10)
        log(f"Stop AFK {r.status_code}: {r.text[:80]}", Fore.YELLOW)
    except Exception as e:
        log(f"Stop AFK lỗi: {e}", Fore.RED)


def send_heartbeat(headers, tenant_id):
    try:
        r = requests.post(f"{BASE}/api/tenants/{tenant_id}/rewards/afk/heartbeat", headers=headers, json={}, timeout=10)
        if r.status_code in (200, 201, 204):
            return True
    except:
        pass
    return False


def sse_loop(token, tenant_id):
    raw = token.replace("Bearer ", "")
    url = f"https://api.altare.sh/subscribe?token={raw}"
    sse_headers = {
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
        "Authorization": token,
        "Origin": "https://altare.sh",
        "Referer": "https://altare.sh/billing",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    first_connect = True
    while running:
        try:
            with requests.get(url, headers=sse_headers, stream=True, timeout=(10, None)) as r:
                if r.status_code == 200:
                    if first_connect:
                        log("SSE: Connected ✓", Fore.GREEN)
                        first_connect = False
                    for line in r.iter_lines(chunk_size=1):
                        if not running:
                            break
                else:
                    first_connect = True
                    time.sleep(10)
        except:
            first_connect = True
            if running:
                time.sleep(10)
        if running:
            time.sleep(3)


def heartbeat_loop(cfg, headers, tenant_id):
    global heartbeat_ok, heartbeat_fail, running
    interval = cfg.get("heartbeat_interval", 30)
    while running:
        if send_heartbeat(headers, tenant_id):
            heartbeat_ok += 1
        else:
            heartbeat_fail += 1
        time.sleep(interval)


def stats_loop(cfg, headers, tenant_id):
    global credits_start, total_credits
    interval = cfg.get("stats_interval", 60)
    while running:
        bal = get_balance(headers, tenant_id)
        if bal is not None:
            if not credits_start:
                credits_start = bal
            total_credits = bal
        time.sleep(interval)


def _make_bar(value, max_val=1000, length=18):
    filled = int((min(value, max_val) / max_val) * length)
    return "█" * filled + "░" * (length - filled)


def discord_loop(cfg, headers, tenant_id):
    global credits_start
    webhook = cfg.get("discord_webhook", "")
    if not webhook:
        log("Discord webhook chưa cấu hình, bỏ qua.", Fore.YELLOW)
        return
    interval = cfg.get("notify_interval_seconds", 10)
    time.sleep(3)

    notify_count = 0
    message_id = None

    while running:
        rewards = get_rewards(headers, tenant_id)
        bal = get_balance(headers, tenant_id) or 0
        per_min = 0.35
        if rewards:
            afk_data = rewards.get("afk") if isinstance(rewards.get("afk"), dict) else {}
            per_min = afk_data.get("perMinute") or rewards.get("perMinute") or 0.35
        if bal and not credits_start:
            credits_start = bal
        _cs = credits_start or 0
        earned = round(bal - _cs, 4) if _cs else 0
        delta  = datetime.now() - session_start if session_start else None
        uptime = str(delta).split(".")[0] if delta else "?"
        notify_count += 1
        ts_now  = datetime.now().strftime("%H:%M:%S — %d/%m/%Y")
        bar     = _make_bar(bal)
        hb_rate = round(heartbeat_ok / max(heartbeat_ok + heartbeat_fail, 1) * 100)

        payload = {
            "username": "Altare AFK Bot",
            "avatar_url": "https://altare.sh/favicon.ico",
            "embeds": [{
                "author": {
                    "name": "⚡ AFK ALTARE — AUTO FARMER",
                    "url": "https://altare.sh/billing",
                    "icon_url": "https://altare.sh/favicon.ico"
                },
                "color": 0x00d4aa,
                "description": (
                    "```ansi\n"
                    "\u001b[1;35m╔══════════════════════════════╗\n"
                    "\u001b[1;35m║   \u001b[1;36mALTARE.SH CREDIT FARMER\u001b[1;35m   ║\n"
                    "\u001b[1;35m╚══════════════════════════════╝\u001b[0m\n"
                    "```"
                ),
                "fields": [
                    {"name": "💰 Credits Balance", "value": f"```\n{bal:.4f} cr\n{bar}\n```", "inline": False},
                    {"name": "📈 Session Earned",  "value": f"```diff\n+ {earned:.4f} cr\n```", "inline": True},
                    {"name": "⚡ Per Minute",       "value": f"```\n{per_min} cr/min\n```", "inline": True},
                    {"name": "⏱️ Uptime",           "value": f"```\n{uptime}\n```", "inline": True},
                    {"name": "🫀 Heartbeat",        "value": f"```diff\n+ OK   : {heartbeat_ok}\n- Fail : {heartbeat_fail}\n  Rate : {hb_rate}%\n```", "inline": True},
                    {"name": "🔗 Session",          "value": f"```\nTenant: {tenant_id[:8]}...\nNotify #: {notify_count}\n```", "inline": True},
                    {"name": "🟢 Status",           "value": "```diff\n+ ACTIVE & CONNECTED\n```", "inline": True},
                ],
                "footer": {"text": f"AFK Altare v2.0  •  {ts_now}", "icon_url": "https://altare.sh/favicon.ico"},
                "timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            }]
        }

        try:
            if message_id is None:
                r = requests.post(webhook + "?wait=true", json=payload, timeout=10)
                if r.status_code in (200, 204):
                    message_id = r.json().get("id")
                    log(f"Discord OK ✓ (ID: {message_id})", Fore.GREEN)
            else:
                r = requests.patch(f"{webhook}/messages/{message_id}", json=payload, timeout=10)
                if r.status_code not in (200, 204):
                    message_id = None
        except:
            message_id = None

        time.sleep(interval)


def main():
    global running, session_start

    print(BANNER)
    cfg   = load_config()
    token = cfg.get("token", "")

    if not token:
        print(f"{Fore.RED}[!] Chưa điền token vào afk_config.json!{Style.RESET_ALL}")
        sys.exit(1)

    headers = make_headers(token)
    log(f"Token:  ...{token[-16:]}", Fore.CYAN)

    tenant_id = cfg.get("tenant_id", "")
    if not tenant_id:
        log("Đang tự dò tenant ID...", Fore.CYAN)
        tenant_id = auto_detect_tenant(headers)

    if not tenant_id:
        log("Không tìm được tenant ID! Điền thủ công vào afk_config.json", Fore.RED)
        sys.exit(1)

    log(f"Tenant: {tenant_id}", Fore.GREEN)
    headers = make_headers(token, tenant_id)
    start_afk_session(headers, tenant_id)

    session_start = datetime.now()
    threads = [
        threading.Thread(target=sse_loop,       args=(token, tenant_id),        daemon=True),
        threading.Thread(target=heartbeat_loop, args=(cfg, headers, tenant_id), daemon=True),
        threading.Thread(target=stats_loop,     args=(cfg, headers, tenant_id), daemon=True),
        threading.Thread(target=discord_loop,   args=(cfg, headers, tenant_id), daemon=True),
    ]
    for t in threads:
        t.start()

    log(f"AFK đang chạy! Nhấn {Fore.RED}Ctrl+C{Fore.RESET} để dừng.", Fore.GREEN)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        running = False
        log("Đang dừng AFK session...", Fore.YELLOW)
        stop_afk_session(headers, tenant_id)
        log("Done.", Fore.YELLOW)
        sys.exit(0)


if __name__ == "__main__":
    main()
