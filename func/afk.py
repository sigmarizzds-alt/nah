import asyncio
import aiohttp
import time
from datetime import datetime
from func.state import (
    sessions, lock, make_headers, api_post, add_log, BASE,
    db_update_lifetime, db_save_token,
    new_session, detect_tenant,
)

HB_INTERVAL   = 30    # giây giữa các heartbeat
REST_INTERVAL = 3600  # farm bao nhiêu giây thì nghỉ (1 giờ)
REST_DURATION = 60    # nghỉ bao nhiêu giây rồi chạy lại
STAGGER_STEP  = 3     # giây cách nhau khi boot nhiều token


async def _stop_remote(http: aiohttp.ClientSession, token: str, tenant_id: str):
    try:
        async with http.post(
            f"{BASE}/api/tenants/{tenant_id}/rewards/afk/stop",
            headers=make_headers(token), json={},
            timeout=aiohttp.ClientTimeout(total=12),
        ) as r:
            return r.status in (200, 201, 204)
    except Exception:
        return False


async def _try_start(http: aiohttp.ClientSession, token: str, tenant_id: str, short: str) -> bool:
    """Retry start vô hạn đến khi thành công. Trả False nếu bị dừng bởi người dùng."""
    afk_base = f"{BASE}/api/tenants/{tenant_id}/rewards/afk"
    attempt  = 0

    while True:
        async with lock:
            if short not in sessions or not sessions[short]["afk_running"]:
                return False

        await _stop_remote(http, token, tenant_id)
        await asyncio.sleep(2)

        async with lock:
            if short not in sessions or not sessions[short]["afk_running"]:
                return False

        ok, err = await api_post(http, f"{afk_base}/start", token)

        if ok:
            return True

        attempt += 1
        wait = min(15 * attempt, 120)
        async with lock:
            if short in sessions:
                add_log(sessions[short],
                        f"start lần {attempt} thất bại: {err} — thử lại sau {wait}s", "warn")
        await asyncio.sleep(wait)


async def _worker(short: str):
    cycle = 0

    while True:
        async with lock:
            if short not in sessions or not sessions[short]["afk_running"]:
                return
            s         = sessions[short]
            token     = s["token"]
            tenant_id = s["tenant_id"]
            s["afk_status"] = "starting"

        afk_base    = f"{BASE}/api/tenants/{tenant_id}/rewards/afk"
        cycle_start = time.time()
        cycle      += 1

        async with aiohttp.ClientSession() as http:
            started = await _try_start(http, token, tenant_id, short)
            if not started:
                return  # dừng bởi người dùng

            async with lock:
                if short not in sessions:
                    return
                s = sessions[short]
                s["afk_status"] = "farming"
                s["afk_error"]  = None
                s["farm_start"] = time.time()
                add_log(s, f"farming chu kỳ {cycle}", "success")

            hb_count = 0

            while True:
                await asyncio.sleep(HB_INTERVAL)

                async with lock:
                    if short not in sessions or not sessions[short]["afk_running"]:
                        return

                hb_count += 1
                ok, err = await api_post(http, f"{afk_base}/heartbeat", token)

                async with lock:
                    if short not in sessions or not sessions[short]["afk_running"]:
                        return
                    s = sessions[short]

                    if ok:
                        s["hb_ok"]     += 1
                        s["hb_last"]    = datetime.now().strftime("%H:%M:%S")
                        s["afk_status"] = "farming"
                        s["afk_error"]  = None
                        add_log(s, f"HB #{hb_count} ok ({s['hb_ok']} tổng)", "success")
                    else:
                        s["hb_fail"]   += 1
                        s["afk_error"]  = err
                        add_log(s, f"HB #{hb_count} thất bại: {err}", "error")

                if time.time() - cycle_start >= REST_INTERVAL:
                    break  # hết chu kỳ, chuyển sang nghỉ

        # --- Nghỉ định kỳ ---
        async with lock:
            if short not in sessions or not sessions[short]["afk_running"]:
                return
            s = sessions[short]
            s["afk_status"] = "resting"
            s["afk_error"]  = None
            add_log(s, f"nghỉ {REST_DURATION}s sau chu kỳ {cycle}", "info")

        await asyncio.sleep(REST_DURATION)

        async with lock:
            if short not in sessions or not sessions[short]["afk_running"]:
                return
            s = sessions[short]
            s["hb_ok"]         = 0
            s["hb_fail"]       = 0
            s["farm_start"]    = None
            s["_last_hb_ok"]   = 0
            s["_last_hb_fail"] = 0
            s["_last_stat_ts"] = time.time()
            add_log(s, f"bắt đầu chu kỳ {cycle + 1}", "info")


async def _stats_worker(short: str):
    while True:
        await asyncio.sleep(60)
        async with lock:
            if short not in sessions:
                return
            s = sessions[short]
            if not s["afk_running"]:
                continue
            now       = time.time()
            hb_ok_d   = s["hb_ok"]  - s.get("_last_hb_ok",  0)
            hb_fail_d = s["hb_fail"] - s.get("_last_hb_fail", 0)
            uptime_d  = int(now      - s.get("_last_stat_ts", now))
            s["_last_hb_ok"]   = s["hb_ok"]
            s["_last_hb_fail"] = s["hb_fail"]
            s["_last_stat_ts"] = now

        if hb_ok_d > 0 or hb_fail_d > 0:
            try:
                db_update_lifetime(short, hb_ok_d, hb_fail_d, uptime_d)
            except Exception as e:
                async with lock:
                    if short in sessions:
                        add_log(sessions[short], f"lỗi ghi stats: {e}", "error")


async def start_afk_session(short: str):
    async with lock:
        if short not in sessions:
            return
        s = sessions[short]

        for attr in ("afk_task", "afk_stats_task"):
            tk = s.get(attr)
            if tk and not tk.done():
                tk.cancel()

        s.update({
            "afk_running":   True,
            "afk_status":    "starting",
            "hb_ok":         0,
            "hb_fail":       0,
            "afk_error":     None,
            "farm_start":    None,
            "_last_hb_ok":   0,
            "_last_hb_fail": 0,
            "_last_stat_ts": time.time(),
        })
        add_log(s, f"khởi động (tenant: {s['tenant_id']})", "info")

        s["afk_task"]       = asyncio.create_task(_worker(short))
        s["afk_stats_task"] = asyncio.create_task(_stats_worker(short))


async def stop_afk_session(short: str):
    async with lock:
        if short not in sessions:
            return
        s = sessions[short]
        s["afk_running"] = False
        s["afk_status"]  = "stopped"

        for attr in ("afk_task", "afk_stats_task"):
            tk = s.get(attr)
            if tk and not tk.done():
                tk.cancel()

        add_log(s, "dừng bởi người dùng", "warn")


async def load_all_tokens():
    from func.state import db_load_tokens

    rows = db_load_tokens()
    if not rows:
        print("[BOOT] Không có token nào trong DB")
        return

    print(f"[BOOT] Tải {len(rows)} token, stagger {STAGGER_STEP}s/token")

    async def _load(row, idx: int):
        await asyncio.sleep(idx * STAGGER_STEP)
        key       = row["short"]
        token     = row["token"]
        tenant_id = row["tenant_id"]
        try:
            async with lock:
                if key in sessions:
                    return

            try:
                async with aiohttp.ClientSession() as http:
                    result = await detect_tenant(http, token)
                if result:
                    tenant_id = result[0]
                else:
                    print(f"[BOOT] Không detect được tenant ...{token[-8:]}, dùng tenant cũ: {tenant_id}")
            except Exception as e:
                print(f"[BOOT] detect_tenant lỗi ...{token[-8:]}: {e}")

            async with lock:
                if key in sessions:
                    return
                s = new_session(token, tenant_id, added_at=row["added_at"])
                sessions[key] = s
                add_log(s, "tải từ DB", "info")

            await start_afk_session(key)
            print(f"[BOOT] OK ...{token[-8:]}")
        except Exception as e:
            print(f"[BOOT] Lỗi ...{token[-8:]}: {e}")

    await asyncio.gather(*[_load(row, i) for i, row in enumerate(rows)])
