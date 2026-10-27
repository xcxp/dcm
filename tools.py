import base64
import hashlib
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands

COMMON_TZ = [
    "Asia/Bangkok", "Asia/Tokyo", "Asia/Singapore", "Asia/Hong_Kong", "Asia/Shanghai",
    "Asia/Kolkata", "Asia/Dubai", "Europe/London", "Europe/Berlin", "Europe/Paris",
    "America/New_York", "America/Chicago", "America/Los_Angeles", "Australia/Sydney", "UTC",
]

STYLES = [
    ("t", "เวลาสั้น"), ("T", "เวลายาว"), ("d", "วันที่สั้น"), ("D", "วันที่ยาว"),
    ("f", "วันที่+เวลา"), ("F", "วันที่+เวลา (มีวัน)"), ("R", "แบบสัมพัทธ์"),
]


class Tools(commands.Cog):
    async def tz_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cur = current.lower()
        return [app_commands.Choice(name=z, value=z) for z in COMMON_TZ if cur in z.lower()][:25]

    @app_commands.command(name="tz", description="แปลงเวลาข้ามไทม์โซน (ของวันนี้)")
    @app_commands.describe(time="เวลา รูปแบบ HH:MM เช่น 21:30", from_tz="ไทม์โซนต้นทาง", to_tz="ไทม์โซนปลายทาง")
    async def tz(
        self,
        interaction: discord.Interaction,
        time: str,
        from_tz: str = "Asia/Bangkok",
        to_tz: str = "America/New_York",
    ) -> None:
        try:
            hh, mm = (int(x) for x in time.strip().split(":"))
            src, dst = ZoneInfo(from_tz.strip()), ZoneInfo(to_tz.strip())
            src_dt = datetime.now(src).replace(hour=hh, minute=mm, second=0, microsecond=0)
        except (ValueError, ZoneInfoNotFoundError):
            await interaction.response.send_message(
                "รูปแบบไม่ถูกต้อง ใช้เวลาแบบ `21:30` และชื่อโซนแบบ `Asia/Bangkok`", ephemeral=True
            )
            return
        dst_dt = src_dt.astimezone(dst)
        diff = (dst_dt.date() - src_dt.date()).days
        note = f" ({diff:+d} วัน)" if diff else ""
        await interaction.response.send_message(
            f"🕒 **{src_dt:%H:%M}** ({from_tz}) → **{dst_dt:%H:%M}**{note} ({to_tz})", ephemeral=True
        )

    tz.autocomplete("from_tz")(tz_autocomplete)
    tz.autocomplete("to_tz")(tz_autocomplete)

    @app_commands.command(name="timestamp", description="สร้างโค้ดเวลาของ Discord ที่แสดงตามเขตเวลาผู้อ่าน")
    @app_commands.describe(when="รูปแบบ YYYY-MM-DD HH:MM เช่น 2026-11-01 09:30", tz="ไทม์โซนของเวลาที่ใส่")
    async def timestamp(self, interaction: discord.Interaction, when: str, tz: str = "Asia/Bangkok") -> None:
        try:
            dt = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(tz.strip()))
        except (ValueError, ZoneInfoNotFoundError):
            await interaction.response.send_message(
                "ใช้รูปแบบ `2026-11-01 09:30` และชื่อโซนแบบ `Asia/Bangkok`", ephemeral=True
            )
            return
        ts = int(dt.timestamp())
        lines = [f"`<t:{ts}:{code}>` → <t:{ts}:{code}>  ({label})" for code, label in STYLES]
        embed = discord.Embed(title="🕓 Discord Timestamp", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="hash", description="คำนวณ hash ของข้อความ")
    @app_commands.choices(
        algo=[app_commands.Choice(name=n, value=n) for n in ("sha256", "sha512", "sha1", "md5")]
    )
    async def hash_cmd(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 1000],
        algo: app_commands.Choice[str],
    ) -> None:
        digest = hashlib.new(algo.value, text.encode("utf-8")).hexdigest()
        await interaction.response.send_message(f"**{algo.value}**\n```{digest}```", ephemeral=True)

    @app_commands.command(name="b64", description="เข้ารหัส/ถอดรหัส Base64")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="encode", value="encode"),
            app_commands.Choice(name="decode", value="decode"),
        ]
    )
    async def b64(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        text: app_commands.Range[str, 1, 1000],
    ) -> None:
        try:
            if mode.value == "encode":
                out = base64.b64encode(text.encode("utf-8")).decode("ascii")
            else:
                out = base64.b64decode(text.strip(), validate=True).decode("utf-8")
        except ValueError:
            await interaction.response.send_message("ข้อมูลไม่ใช่ Base64 ที่ถูกต้อง", ephemeral=True)
            return
        out = out.replace("```", "'''")[:1800]
        await interaction.response.send_message(f"```{out}```", ephemeral=True)

    @app_commands.command(name="uuid", description="สร้าง UUID v4")
    async def uuid_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(f"`{uuid.uuid4()}`", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tools())