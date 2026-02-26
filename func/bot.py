import os
import asyncio
import aiohttp
import discord
from discord.ext import commands
from datetime import datetime, timezone
from dotenv import load_dotenv

from func.state import (
    sessions, lock, session_snapshot,
    new_session, add_log, detect_tenant, short as mk_short,
    db_save_token, db_delete_token,
    set_log_channel,
)
from func.afk import start_afk_session, stop_afk_session, load_all_tokens

load_dotenv()
DISCORD_TOKEN = os.getenv("TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("TOKEN chưa được đặt trong .env")

KENH_LOG = int(os.getenv("LOG_CHANNEL_ID", "0"))

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

STATUS_TEXT = {
    "farming":  "Đang farm",
    "resting":  "Nghỉ định kỳ",
    "starting": "Đang khởi động",
    "stopped":  "Đã dừng",
    "idle":     "Chờ",
}


def _ts():
    return datetime.now(timezone.utc)


@bot.event
async def on_ready():
    print(f"[BOT] {bot.user}")
    if KENH_LOG:
        ch = bot.get_channel(KENH_LOG) or await bot.fetch_channel(KENH_LOG)
        set_log_channel(ch)
        print(f"[BOT] Kênh log: #{ch.name}")
    await load_all_tokens()
    synced = await bot.tree.sync()
    print(f"[BOT] {len(synced)} lệnh đã sync")


@bot.tree.command(name="danh-sach", description="Xem tất cả session đang chạy")
async def cmd_ds(interaction: discord.Interaction):
    async with lock:
        snaps = [session_snapshot(s) for s in sessions.values()]

    if not snaps:
        await interaction.response.send_message("Chưa có session nào.", ephemeral=True)
        return

    farming = sum(1 for s in snaps if s["afk_status"] == "farming")
    em = discord.Embed(title="Farm Sessions", color=0x00E5F0, timestamp=_ts())
    em.description = f"{len(snaps)} token | {farming} đang farm"

    for s in snaps[:20]:
        lt  = s.get("lifetime", {})
        val = (
            f"{STATUS_TEXT.get(s['afk_status'], s['afk_status'])}\n"
            f"Uptime: `{s['uptime']}` | HB: `{s['hb_ok']}/{s['hb_fail']}`\n"
            f"HB lifetime: `{lt.get('total_hb_ok', 0)}/{lt.get('total_hb_fail', 0)}`"
        )
        if s["afk_error"]:
            val += f"\n`{s['afk_error'][:80]}`"
        em.add_field(name=f"...{s['token_tail']}", value=val, inline=False)

    await interaction.response.send_message(embed=em, ephemeral=True)


@bot.tree.command(name="them-token", description="Thêm token Altare vào hệ thống")
@discord.app_commands.describe(token="Bearer token")
async def cmd_them(interaction: discord.Interaction, token: str):
    await interaction.response.defer(ephemeral=True)

    token = token.strip()
    if not token.startswith("Bearer "):
        token = f"Bearer {token}"
    key = mk_short(token)

    async with lock:
        if key in sessions:
            await interaction.followup.send("Token đã tồn tại.", ephemeral=True)
            return

    async with aiohttp.ClientSession() as http:
        result = await detect_tenant(http, token)

    if not result:
        await interaction.followup.send("Token không hợp lệ hoặc hết hạn.", ephemeral=True)
        return

    tenant_id = result[0]

    async with lock:
        s = new_session(token, tenant_id)
        sessions[key] = s
        add_log(s, f"thêm bởi {interaction.user}", "success")

    db_save_token(key, token, tenant_id, s["added_at"])
    await start_afk_session(key)

    em = discord.Embed(title="Token đã thêm", color=0x4caf50, timestamp=_ts())
    em.add_field(name="Tenant ID",    value=f"`{tenant_id}`",     inline=True)
    em.add_field(name="Trạng thái",   value="`Đang khởi động`",   inline=True)
    em.add_field(name="Token đầy đủ", value=f"```{token}```",     inline=False)
    await interaction.followup.send(embed=em, ephemeral=True)


@bot.tree.command(name="xoa-token", description="Xóa vĩnh viễn token khỏi hệ thống")
@discord.app_commands.describe(tail="8 ký tự cuối của token")
async def cmd_xoa(interaction: discord.Interaction, tail: str):
    await interaction.response.defer(ephemeral=True)

    target_key = None
    async with lock:
        for k, s in sessions.items():
            if s["token"][-8:] == tail.strip():
                target_key = k
                break

    if not target_key:
        await interaction.followup.send("Không tìm thấy token.", ephemeral=True)
        return

    async with lock:
        if target_key not in sessions:
            await interaction.followup.send("Session đã biến mất.", ephemeral=True)
            return
        s = sessions.pop(target_key)

    for attr in ("afk_task", "afk_stats_task"):
        tk = s.get(attr)
        if tk and not tk.done():
            tk.cancel()

    db_delete_token(target_key)

    em = discord.Embed(title="Token đã xóa", color=0xf85149, timestamp=_ts())
    em.add_field(name="Token", value=f"`...{tail}`", inline=True)
    await interaction.followup.send(embed=em, ephemeral=True)


@bot.tree.command(name="restart-token", description="Khởi động lại một token cụ thể")
@discord.app_commands.describe(tail="8 ký tự cuối của token")
async def cmd_restart(interaction: discord.Interaction, tail: str):
    await interaction.response.defer(ephemeral=True)

    target_key = None
    async with lock:
        for k, s in sessions.items():
            if s["token"][-8:] == tail.strip():
                target_key = k
                break

    if not target_key:
        await interaction.followup.send("Không tìm thấy token.", ephemeral=True)
        return

    await stop_afk_session(target_key)
    await asyncio.sleep(1)
    await start_afk_session(target_key)

    em = discord.Embed(title="Đã restart", color=0xd29922, timestamp=_ts())
    em.add_field(name="Token", value=f"`...{tail}`", inline=True)
    await interaction.followup.send(embed=em, ephemeral=True)


async def run_bot():
    async with bot:
        await bot.start(DISCORD_TOKEN)
