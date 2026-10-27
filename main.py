import itertools
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
EXTENSIONS = ["cogs.mirror", "cogs.finance", "cogs.productivity", "cogs.tools"]

STATUSES = [
    (discord.ActivityType.watching, "ตลาดการเงิน 📈"),
    (discord.ActivityType.listening, "/help"),
    (discord.ActivityType.playing, "จัดการงานให้ทีม ⚡"),
]


class FinTechBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # จำเป็นสำหรับ Mirror (ต้องเปิดใน Developer Portal ด้วย)
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )
        self._status_cycle = itertools.cycle(STATUSES)

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
                log.info("โหลด %s สำเร็จ", ext)
            except Exception:
                log.exception("โหลด %s ไม่สำเร็จ", ext)
        if os.getenv("SYNC_COMMANDS", "1") == "1":
            synced = await self.tree.sync()
            log.info("sync slash commands: %d คำสั่ง", len(synced))
        self.rotate_status.start()

    @tasks.loop(minutes=5)
    async def rotate_status(self) -> None:
        try:
            kind, name = next(self._status_cycle)
            await self.change_presence(activity=discord.Activity(type=kind, name=name))
        except Exception:
            log.exception("rotate_status error")

    @rotate_status.before_loop
    async def before_rotate(self) -> None:
        await self.wait_until_ready()

    async def on_ready(self) -> None:
        log.info("ออนไลน์แล้วในชื่อ %s (อยู่ใน %d เซิร์ฟเวอร์)", self.user, len(self.guilds))


bot = FinTechBot()


@bot.tree.command(name="help", description="ดูคำสั่งทั้งหมดของบอท")
async def help_cmd(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="📖 คู่มือคำสั่ง", color=discord.Color.blurple())
    embed.add_field(
        name="📈 การเงิน",
        value="`/crypto` `/stock` `/fx` `/digest`\n`/alert add|list|remove` แจ้งเตือนราคา",
        inline=False,
    )
    embed.add_field(
        name="✅ ผลิตภาพ",
        value="`/remind` `/reminders` `/remind_cancel` `/pomodoro`\n`/todo add|list|done|clear` `/poll`",
        inline=False,
    )
    embed.add_field(
        name="🛠️ เครื่องมือ Tech",
        value="`/tz` `/timestamp` `/hash` `/b64` `/uuid`",
        inline=False,
    )
    embed.add_field(name="🪞 Mirror (แอดมิน)", value="`/mirror_status`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    log.error("คำสั่งผิดพลาด: %s", error, exc_info=error)
    msg = "❌ เกิดข้อผิดพลาด ลองใหม่อีกครั้ง"
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"⏳ ช้าหน่อย ลองใหม่ใน {error.retry_after:.0f} วินาที"
    elif isinstance(error, app_commands.MissingPermissions):
        msg = "🚫 คุณไม่มีสิทธิ์ใช้คำสั่งนี้"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("ไม่พบ DISCORD_TOKEN — ใส่ในไฟล์ .env หรือ Environment Variables")
    bot.run(TOKEN, log_handler=None)