import asyncio
import logging
import os
import re

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from storage import load_json, save_json

log = logging.getLogger("mirror")

MAP_FILE = "mirror_map.json"
MAX_FILE_BYTES = 8 * 1024 * 1024      # ต่อไฟล์
MAX_TOTAL_BYTES = 20 * 1024 * 1024    # รวมต่อข้อความ
BAD_WORDS = re.compile(r"(clyde|discord)", re.IGNORECASE)


def clean_name(name: str) -> str:
    """Webhook ปฏิเสธชื่อที่มีคำว่า discord/clyde"""
    name = BAD_WORDS.sub(lambda m: m.group(0)[:-1] + "_", name).strip()
    return (name or "unknown")[:80]


def split_text(text: str, limit: int = 2000) -> list[str]:
    text = text.strip()
    if not text:
        return [""]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def parse_ids(raw: str) -> set[int]:
    return {int(x) for x in re.findall(r"\d+", raw or "")}


class Mirror(commands.Cog):
    """รวมข้อความจากทุก text channel ใน server เข้าห้องเดียว"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.guild_id = int(os.getenv("MIRROR_SOURCE_GUILD_ID") or 0)
        self.target_id = int(os.getenv("MIRROR_TARGET_CHANNEL_ID") or 0)
        self.enabled = bool(self.guild_id and self.target_id)
        self.include_private = os.getenv("MIRROR_INCLUDE_PRIVATE", "0") == "1"
        self.excluded = parse_ids(os.getenv("MIRROR_EXCLUDE_CHANNEL_IDS", ""))
        self.session: aiohttp.ClientSession | None = None
        self.webhook: discord.Webhook | None = None
        self.hook_lock = asyncio.Lock()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()
        if self.enabled:
            self.worker_task = asyncio.create_task(self.worker())
            log.info("Mirror เปิดอยู่: ทุกห้องใน %s -> ห้อง %s", self.guild_id, self.target_id)
        else:
            log.warning(
                "Mirror ปิดอยู่ — ตั้ง MIRROR_SOURCE_GUILD_ID และ MIRROR_TARGET_CHANNEL_ID ใน .env"
            )

    async def cog_unload(self) -> None:
        if self.worker_task:
            self.worker_task.cancel()
        if self.session:
            await self.session.close()

    # ---------- webhook ของห้องปลายทาง ----------
    async def get_webhook(self) -> discord.Webhook | None:
        if self.webhook:
            return self.webhook
        async with self.hook_lock:
            if self.webhook:
                return self.webhook
            channel = self.bot.get_channel(self.target_id)
            if not isinstance(channel, discord.TextChannel):
                log.error("ไม่พบห้องปลายทาง %s (ID ผิด หรือบอทมองไม่เห็นห้อง)", self.target_id)
                return None
            saved = load_json(MAP_FILE, {}).get("webhook")
            if saved and saved.get("channel_id") == self.target_id:
                self.webhook = discord.Webhook.partial(
                    saved["webhook_id"], saved["webhook_token"], session=self.session
                )
            else:
                created = await channel.create_webhook(name="Mirror", reason="Mirror")
                save_json(
                    MAP_FILE,
                    {
                        "webhook": {
                            "channel_id": self.target_id,
                            "webhook_id": created.id,
                            "webhook_token": created.token,
                        }
                    },
                )
                self.webhook = discord.Webhook.partial(
                    created.id, created.token, session=self.session
                )
            return self.webhook

    # ---------- รับข้อความ ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self.enabled or message.guild is None or message.guild.id != self.guild_id:
            return
        if message.author.id == self.bot.user.id:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return
        if channel.id == self.target_id or channel.id in self.excluded:
            return  # ข้ามห้องปลายทางเอง (กันลูป) และห้องที่ยกเว้น
        if not self.include_private:
            if not channel.permissions_for(message.guild.default_role).view_channel:
                return
        self.queue.put_nowait(message)

    async def worker(self) -> None:
        """ส่งทีละข้อความตามลำดับที่เข้ามา"""
        while True:
            message = await self.queue.get()
            try:
                await self.forward(message)
            except Exception:
                log.exception("Mirror ข้อความ %s ไม่สำเร็จ", message.id)
            finally:
                self.queue.task_done()

    async def forward(self, message: discord.Message) -> None:
        webhook = await self.get_webhook()
        if webhook is None:
            return

        body = message.clean_content
        ref = message.reference.resolved if message.reference else None
        if isinstance(ref, discord.Message):
            snippet = (ref.clean_content or "[ไฟล์แนบ]").replace("\n", " ")[:80]
            body = f"> ↪ **{ref.author.display_name}**: {snippet}\n{body}"

        files: list[discord.File] = []
        links: list[str] = []
        budget = MAX_TOTAL_BYTES
        for att in message.attachments:
            if len(files) < 10 and att.size <= MAX_FILE_BYTES and att.size <= budget:
                try:
                    files.append(await att.to_file())
                    budget -= att.size
                    continue
                except discord.HTTPException:
                    pass
            links.append(att.url)
        if links:
            body = body + "\n" + "\n".join(links)

        embeds = [
            discord.Embed.from_dict(e.to_dict()) for e in message.embeds if e.type == "rich"
        ][:10]

        if not body.strip() and not files and not embeds:
            return

        header = f"<#{message.channel.id}> [↗]({message.jump_url})"
        chunks = split_text(f"{header}\n{body}")

        name = clean_name(message.author.display_name)
        avatar = message.author.display_avatar.url
        for i, chunk in enumerate(chunks):
            first = i == 0
            try:
                await webhook.send(
                    content=chunk,
                    username=name,
                    avatar_url=avatar,
                    files=files if (first and files) else discord.utils.MISSING,
                    embeds=embeds if (first and embeds) else discord.utils.MISSING,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound:
                log.warning("Webhook หาย — จะสร้างใหม่ตอนข้อความถัดไป")
                self.webhook = None
                save_json(MAP_FILE, {})
                return

    @app_commands.command(name="mirror_status", description="ดูสถานะการ mirror (แอดมิน)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def mirror_status(self, interaction: discord.Interaction) -> None:
        target = self.bot.get_channel(self.target_id)
        embed = discord.Embed(title="🪞 สถานะ Mirror", color=discord.Color.teal())
        embed.add_field(name="เปิดใช้งาน", value="✅" if self.enabled else "❌ ตรวจไฟล์ .env")
        embed.add_field(name="ห้องปลายทาง", value=target.mention if target else "❌ ไม่พบห้อง")
        embed.add_field(name="ข้อความที่รอส่ง", value=str(self.queue.qsize()))
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Mirror(bot))