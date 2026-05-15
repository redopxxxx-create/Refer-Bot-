import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional, List

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
    InputMediaPhoto,
    InputMediaVideo,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DATABASE_URL = os.getenv("DATABASE_URL", "bot.db")
START_MEDIA = [m.strip() for m in os.getenv("START_MEDIA", "").split(",") if m.strip()]
POINTS_PER_REFERRAL = int(os.getenv("POINTS_PER_REFERRAL", "10") or 10)
MIN_WITHDRAW_POINTS = int(os.getenv("MIN_WITHDRAW_POINTS", "100") or 100)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")


@dataclass
class UserRow:
    user_id: int
    ref_code: str
    ref_by: Optional[int]
    joins: int
    points: int
    created_at: int


class DB:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, ref_code TEXT UNIQUE NOT NULL, ref_by INTEGER, joins INTEGER NOT NULL DEFAULT 0, points INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)")
            await db.execute("CREATE TABLE IF NOT EXISTS referrals (referrer_id INTEGER NOT NULL, referred_id INTEGER NOT NULL UNIQUE, created_at INTEGER NOT NULL)")
            await db.execute("CREATE TABLE IF NOT EXISTS channels (channel_id INTEGER PRIMARY KEY, title TEXT, active INTEGER NOT NULL DEFAULT 1)")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS rewards (id INTEGER PRIMARY KEY AUTOINCREMENT, ott_section TEXT NOT NULL DEFAULT 'General', name TEXT NOT NULL, stock INTEGER NOT NULL DEFAULT 0, cost_points INTEGER NOT NULL DEFAULT 100, reward_login_id TEXT, reward_pass TEXT, active INTEGER NOT NULL DEFAULT 1, UNIQUE(ott_section, name))"
            )
            await db.execute("CREATE TABLE IF NOT EXISTS withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, reward_id INTEGER NOT NULL, points_spent INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL)")
            await db.commit()

    async def get_user(self, user_id: int) -> Optional[UserRow]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT user_id, ref_code, ref_by, joins, points, created_at FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            return UserRow(*row) if row else None

    async def get_user_id_by_ref_code(self, code: str) -> Optional[int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT user_id FROM users WHERE ref_code = ?", (code,))
            row = await cur.fetchone()
            return row[0] if row else None

    async def create_user(self, user_id: int, ref_by: Optional[int]):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO users (user_id, ref_code, ref_by, created_at) VALUES (?, ?, ?, ?)", (user_id, uuid.uuid4().hex[:10], ref_by, int(time.time())))
            await db.commit()

    async def add_referral_if_new(self, referrer_id: int, referred_id: int) -> bool:
        if referrer_id == referred_id:
            return False
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT 1 FROM referrals WHERE referred_id = ?", (referred_id,))
            if await cur.fetchone():
                return False
            await db.execute("INSERT INTO referrals (referrer_id, referred_id, created_at) VALUES (?, ?, ?)", (referrer_id, referred_id, int(time.time())))
            await db.execute("UPDATE users SET joins = joins + 1, points = points + ? WHERE user_id = ?", (POINTS_PER_REFERRAL, referrer_id))
            await db.commit()
            return True

    async def leaderboard(self, limit: int = 10):
        async with aiosqlite.connect(self.path) as db:
            return await (await db.execute("SELECT user_id, joins, points FROM users ORDER BY points DESC, joins DESC LIMIT ?", (limit,))).fetchall()

    async def global_stats(self):
        async with aiosqlite.connect(self.path) as db:
            users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
            refs = (await (await db.execute("SELECT COUNT(*) FROM referrals")).fetchone())[0]
            return users, refs

    async def add_channel(self, channel_id: int, title: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO channels(channel_id,title,active) VALUES(?,?,1) ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title, active=1", (channel_id, title))
            await db.commit()

    async def list_channels(self):
        async with aiosqlite.connect(self.path) as db:
            return await (await db.execute("SELECT channel_id, title FROM channels WHERE active=1 ORDER BY rowid")).fetchall()

    async def upsert_reward(self, ott_section: str, name: str, stock: int, cost_points: int, reward_login_id: str, reward_pass: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO rewards(ott_section,name,stock,cost_points,reward_login_id,reward_pass,active) VALUES(?,?,?,?,?,?,1) ON CONFLICT(ott_section,name) DO UPDATE SET stock=excluded.stock, cost_points=excluded.cost_points, reward_login_id=excluded.reward_login_id, reward_pass=excluded.reward_pass, active=1",
                (ott_section, name, stock, cost_points, reward_login_id, reward_pass),
            )
            await db.commit()

    async def list_reward_sections(self):
        async with aiosqlite.connect(self.path) as db:
            return await (await db.execute("SELECT DISTINCT ott_section FROM rewards WHERE active=1 ORDER BY ott_section")).fetchall()

    async def list_rewards(self, section: Optional[str] = None):
        async with aiosqlite.connect(self.path) as db:
            if section:
                cur = await db.execute("SELECT id,ott_section,name,stock,cost_points FROM rewards WHERE active=1 AND ott_section=? ORDER BY id", (section,))
            else:
                cur = await db.execute("SELECT id,ott_section,name,stock,cost_points FROM rewards WHERE active=1 ORDER BY ott_section, id")
            return await cur.fetchall()

    async def create_withdrawal(self, user_id: int, reward_id: int):
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT stock,cost_points FROM rewards WHERE id=? AND active=1", (reward_id,))).fetchone()
            if not row:
                return "not_found", None
            stock, cost = row
            user = await (await db.execute("SELECT points FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not user or user[0] < cost or user[0] < MIN_WITHDRAW_POINTS:
                return "low_points", None
            if stock <= 0:
                return "out_stock", None
            await db.execute("UPDATE users SET points=points-? WHERE user_id=?", (cost, user_id))
            await db.execute("UPDATE rewards SET stock=stock-1 WHERE id=?", (reward_id,))
            await db.execute("INSERT INTO withdrawals(user_id,reward_id,points_spent,status,created_at) VALUES(?,?,?,?,?)", (user_id, reward_id, cost, "pending", int(time.time())))
            creds = await (await db.execute("SELECT ott_section,name,reward_login_id,reward_pass FROM rewards WHERE id=?", (reward_id,))).fetchone()
            await db.commit()
            return "ok", creds


def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Refer", callback_data="refer"), InlineKeyboardButton(text="👤 Profile", callback_data="profile")],
        [InlineKeyboardButton(text="🏆 Leaderboard", callback_data="top"), InlineKeyboardButton(text="🎁 Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton(text="🛒 Reward Stock", callback_data="stock"), InlineKeyboardButton(text="📊 Stats", callback_data="stats")],
    ])


async def is_forced_ok(bot: Bot, user_id: int) -> bool:
    for ch, _ in await db.list_channels():
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
            if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                return False
        except Exception:
            return False
    return True


async def forced_kb(bot: Bot) -> InlineKeyboardMarkup:
    rows = []
    for i, (ch, title) in enumerate(await db.list_channels(), start=1):
        try:
            url = (await bot.create_chat_invite_link(chat_id=ch, expire_date=int(time.time()) + 7200, member_limit=1)).invite_link
        except Exception:
            url = f"https://t.me/c/{str(ch).replace('-100', '')}"
        rows.append([InlineKeyboardButton(text=f"🔒 Join {title or f'Channel {i}'}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Check Again", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_media_items(items: List[str]):
    parsed = []
    for raw in items:
        if raw.startswith("video:"):
            parsed.append(("video", raw.split("video:", 1)[1]))
        elif raw.startswith("photo:"):
            parsed.append(("photo", raw.split("photo:", 1)[1]))
        else:
            parsed.append(("photo", raw))
    return parsed


async def send_start_media(msg: Message, caption: str):
    media_items = parse_media_items(START_MEDIA)
    if not media_items:
        await msg.answer(caption, parse_mode="Markdown", reply_markup=main_kb())
        return
    album = []
    for idx, (kind, file_id_or_url) in enumerate(media_items[:10]):
        cap = caption if idx == 0 else None
        album.append(InputMediaVideo(media=file_id_or_url, caption=cap, parse_mode="Markdown") if kind == "video" else InputMediaPhoto(media=file_id_or_url, caption=cap, parse_mode="Markdown"))
    await msg.answer_media_group(album)
    await msg.answer("✨ Choose section:", reply_markup=main_kb())


def home_text(user_id: int, ref_link: str) -> str:
    return f"🔥 *Advanced OTT Refer Bot*\n\n🆔 `{user_id}`\n🔗 `{ref_link}`\n\nEarn *{POINTS_PER_REFERRAL}* points/invite. Minimum *{MIN_WITHDRAW_POINTS}* to withdraw."


db = DB(DATABASE_URL)
dp = Dispatcher()


@dp.message(Command("start"))
async def start_handler(msg: Message, bot: Bot):
    uid = msg.from_user.id
    ref_arg = msg.text.split(maxsplit=1)[1] if msg.text and len(msg.text.split()) > 1 else None
    ref_by = await db.get_user_id_by_ref_code(ref_arg.replace("ref_", "", 1)) if ref_arg and ref_arg.startswith("ref_") else None
    if not await db.get_user(uid):
        await db.create_user(uid, ref_by)
        if ref_by:
            await db.add_referral_if_new(ref_by, uid)
    if not await is_forced_ok(bot, uid):
        await msg.answer("Join all required channels first:", reply_markup=await forced_kb(bot))
        return
    me = await db.get_user(uid)
    await send_start_media(msg, home_text(uid, f"https://t.me/{(await bot.get_me()).username}?start=ref_{me.ref_code}"))


@dp.message(Command("addchannel"))
async def add_channel_cmd(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 2:
        await msg.answer("Usage: /addchannel -100xxxx Title")
        return
    await db.add_channel(int(parts[1]), parts[2] if len(parts) > 2 else parts[1])
    await msg.answer("✅ Channel added/updated.")


@dp.message(Command("addreward"))
async def add_reward_cmd(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    parts = msg.text.split(maxsplit=6)
    if len(parts) < 7:
        await msg.answer("Usage: /addreward <OTT> <RewardName> <Stock> <CostPoints> <LoginID> <Password>")
        return
    ott, name = parts[1], parts[2]
    stock, cost = int(parts[3]), int(parts[4])
    await db.upsert_reward(ott, name, stock, cost, parts[5], parts[6])
    await msg.answer(f"✅ {ott} section updated: {name} | stock {stock} | cost {cost}")


@dp.callback_query(F.data.in_({"profile", "refer", "top", "withdraw", "stock", "stats", "check_sub"}) | F.data.startswith("wdsec_") | F.data.startswith("wd_"))
async def menu_cb(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    data = cb.data

    if data == "check_sub":
        if await is_forced_ok(bot, uid):
            await cb.message.edit_text("✅ Subscription verified. Press /start")
        else:
            await cb.answer("Still not joined.", show_alert=True)
        return

    me = await db.get_user(uid)
    if data == "profile":
        await cb.message.reply(f"👤 ID: {uid}\n👥 Joins: {me.joins}\n💰 Points: {me.points}")
    elif data == "refer":
        await cb.message.reply(f"🔗 Your referral link:\nhttps://t.me/{(await bot.get_me()).username}?start=ref_{me.ref_code}")
    elif data == "top":
        rows = await db.leaderboard(10)
        await cb.message.reply("🏆 Top Referrers\n" + "".join([f"{i}) {u} • {j} joins • {p} pts\n" for i, (u, j, p) in enumerate(rows, 1)]))
    elif data == "stock":
        rewards = await db.list_rewards()
        if not rewards:
            await cb.message.reply("No reward sections yet.")
        else:
            text = "🛒 OTT Rewards\n\n"
            for rid, section, name, stock, cost in rewards:
                text += f"[{section}] {rid}. {name} | stock: {stock} | cost: {cost} pts\n"
            await cb.message.reply(text)
    elif data == "withdraw":
        sections = await db.list_reward_sections()
        if not sections:
            await cb.message.reply("No withdrawal sections yet.")
        else:
            buttons = [[InlineKeyboardButton(text=row[0], callback_data=f"wdsec_{row[0]}")] for row in sections]
            await cb.message.reply("Choose OTT section:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    elif data.startswith("wdsec_"):
        section = data.split("wdsec_", 1)[1]
        rewards = await db.list_rewards(section)
        buttons = [[InlineKeyboardButton(text=f"{name} ({cost} pts)", callback_data=f"wd_{rid}")] for rid, _, name, stock, cost in rewards if stock > 0]
        if not buttons:
            await cb.message.reply(f"No in-stock rewards in {section}.")
        else:
            await cb.message.reply(f"{section} withdrawal options:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    elif data.startswith("wd_"):
        result, creds = await db.create_withdrawal(uid, int(data.split("_", 1)[1]))
        if result == "ok":
            section, reward_name, login_id, password = creds
            await cb.message.reply(
                f"✅ Withdrawal request created for {section}/{reward_name}.\n"
                f"Reward Login ID: `{login_id or '-'}`\nReward Pass: `{password or '-'}`",
                parse_mode="Markdown",
            )
        elif result == "low_points":
            await cb.message.reply(f"❌ Not enough points. Need at least {MIN_WITHDRAW_POINTS} and reward cost.")
        elif result == "out_stock":
            await cb.message.reply("❌ Out of stock.")
        else:
            await cb.message.reply("❌ Reward not found.")
    elif data == "stats":
        u, r = await db.global_stats()
        await cb.message.reply(f"📊 Users: {u}\n🔗 Referrals: {r}")
    await cb.answer()


async def main():
    await db.init()
    await dp.start_polling(Bot(BOT_TOKEN))


if __name__ == "__main__":
    asyncio.run(main())
