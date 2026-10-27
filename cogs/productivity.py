import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from storage import load_json, save_json

log = logging.getLogger("productivity")

FILE = "productivity.json"
DURATION_RE = re.compile(r"(\d+)\s*([smhd])")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MIN_SECONDS, MAX_SECONDS = 10, 30 * 86400
MAX_REMINDERS = 20
MAX_TODOS = 30
NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def parse_duration(text: str) -> int | None:
    text = text.strip().lower()
    matches = DURATION_RE.findall(text)
    if not matches or DURATION_RE.sub("", text).strip():
        return None
    return sum(int(n) * UNITS[u] for n, u in matches)


class Productivity(commands.Cog):
    todo = app_commands.Group(name="todo", description="จัดการรายการงานส่วนตัว")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.reminders: list[dict] = []
        self.todos: dict[str, list[dict]] = {}

    async def cog_load(self) -> None:
        data = load_json(FILE, {})
        self.reminders = data.get("reminders", [])
        self.todos = data.get("todos", {})
        self.reminder_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()

    def save(self) -> None:
        save_json(FILE, {"reminders": self.reminders, "todos": self.todos})

    # ---------- Reminder ----------
    def add_reminder(self, user_id: int, channel_id: int | None, seconds: int, text: str) -> tuple[int, int]:
        when = int(time.time() + seconds)
        rid = max((r["id"] for r in self.reminders), default=0) + 1
        self.reminders.append(
            {"id": rid, "user_id": user_id, "channel_id": channel_id, "when": when, "text": text}
        )
        self.save()
        return rid, when

    def count_mine(self, user_id: int) -> int:
        return sum(1 for r in self.reminders if r["user_id"] == user_id)

    async def deliver(self, r: dict) -> None:
        text = f"⏰ <@{r['user_id']}> {r['text']}"
        mentions = discord.AllowedMentions(
            users=[discord.Object(id=r["user_id"])], roles=False, everyone=False
        )
        channel = self.bot.get_channel(r["channel_id"]) if r["channel_id"] else None
        try:
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(text, allowed_mentions=mentions)
                return
        except discord.HTTPException:
            pass
        try:
            user = await self.bot.fetch_user(r["user_id"])
            await user.send(text)
        except discord.HTTPException:
            log.warning("ส่ง reminder #%s ไม่สำเร็จ", r["id"])

    @tasks.loop(seconds=15)
    async def reminder_loop(self) -> None:
        try:
            now = time.time()
            due = [r for r in self.reminders if r["when"] <= now]
            if not due:
                return
            self.reminders = [r for r in self.reminders if r["when"] > now]
            self.save()
            for r in due:
                await self.deliver(r)
        except Exception:
            log.exception("reminder_loop error")

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="remind", description="ตั้งเตือนความจำ เช่น 30m, 2h30m, 1d")
    @app_commands.describe(when="อีกนานแค่ไหน เช่น 10m, 2h, 1d12h", text="ข้อความที่ให้เตือน")
    async def remind(
        self, interaction: discord.Interaction, when: str, text: app_commands.Range[str, 1, 500]
    ) -> None:
        seconds = parse_duration(when)
        if seconds is None:
            await interaction.response.send_message(
                "รูปแบบเวลาไม่ถูกต้อง ลอง `10m`, `2h30m`, `1d`", ephemeral=True
            )
            return
        if not MIN_SECONDS <= seconds <= MAX_SECONDS:
            await interaction.response.send_message("ตั้งได้ตั้งแต่ 10 วินาที ถึง 30 วัน", ephemeral=True)
            return
        if self.count_mine(interaction.user.id) >= MAX_REMINDERS:
            await interaction.response.send_message(f"ตั้งได้สูงสุด {MAX_REMINDERS} รายการ", ephemeral=True)
            return
        rid, ts = self.add_reminder(interaction.user.id, interaction.channel_id, seconds, text)
        await interaction.response.send_message(
            f"⏰ โอเค จะเตือน <t:{ts}:R> (<t:{ts}:f>) • รหัส `#{rid}`", ephemeral=True
        )

    @app_commands.command(name="reminders", description="ดูรายการเตือนความจำของคุณ")
    async def reminders_list(self, interaction: discord.Interaction) -> None:
        mine = sorted(
            (r for r in self.reminders if r["user_id"] == interaction.user.id), key=lambda r: r["when"]
        )
        if not mine:
            await interaction.response.send_message("ไม่มีเตือนค้างอยู่", ephemeral=True)
            return
        lines = [f"`#{r['id']}` <t:{r['when']}:R> — {r['text'][:80]}" for r in mine]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="remind_cancel", description="ยกเลิกเตือนความจำตามรหัส")
    async def remind_cancel(self, interaction: discord.Interaction, reminder_id: int) -> None:
        for r in self.reminders:
            if r["id"] == reminder_id and r["user_id"] == interaction.user.id:
                self.reminders.remove(r)
                self.save()
                await interaction.response.send_message(f"🗑️ ยกเลิก #{reminder_id} แล้ว", ephemeral=True)
                return
        await interaction.response.send_message("ไม่พบรายการนี้ของคุณ", ephemeral=True)

    @app_commands.command(name="pomodoro", description="จับเวลา Pomodoro (โฟกัส/พัก)")
    @app_commands.describe(work="นาทีโฟกัส", rest="นาทีพัก")
    async def pomodoro(
        self,
        interaction: discord.Interaction,
        work: app_commands.Range[int, 1, 120] = 25,
        rest: app_commands.Range[int, 1, 60] = 5,
    ) -> None:
        if self.count_mine(interaction.user.id) > MAX_REMINDERS - 2:
            await interaction.response.send_message("รายการเตือนของคุณเต็ม ลองลบบางอันก่อน", ephemeral=True)
            return
        uid, cid = interaction.user.id, interaction.channel_id
        _, t1 = self.add_reminder(uid, cid, work * 60, "🍅 หมดเวลาโฟกัส พักได้แล้ว!")
        self.add_reminder(uid, cid, (work + rest) * 60, "💪 หมดเวลาพัก กลับมาลุยต่อ!")
        await interaction.response.send_message(
            f"🍅 เริ่ม Pomodoro: โฟกัส **{work}** นาที (จบ <t:{t1}:R>) แล้วพัก **{rest}** นาที"
        )

    # ---------- To-do ----------
    def my_todos(self, user_id: int) -> list[dict]:
        return self.todos.setdefault(str(user_id), [])

    @todo.command(name="add", description="เพิ่มงาน")
    async def todo_add(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 100]) -> None:
        items = self.my_todos(interaction.user.id)
        if len(items) >= MAX_TODOS:
            await interaction.response.send_message(f"เต็มแล้ว ({MAX_TODOS} รายการ) ลองใช้ `/todo clear`", ephemeral=True)
            return
        new_id = max((i["id"] for i in items), default=0) + 1
        items.append({"id": new_id, "text": text, "done": False})
        self.save()
        await interaction.response.send_message(f"➕ เพิ่ม `#{new_id}` แล้ว", ephemeral=True)

    @todo.command(name="list", description="ดูรายการงานของคุณ")
    async def todo_list(self, interaction: discord.Interaction) -> None:
        items = self.my_todos(interaction.user.id)
        if not items:
            await interaction.response.send_message("ยังไม่มีงาน 🎉", ephemeral=True)
            return
        lines = [
            f"✅ ~~{i['id']}. {i['text']}~~" if i["done"] else f"⬜ {i['id']}. {i['text']}"
            for i in items
        ]
        done = sum(1 for i in items if i["done"])
        embed = discord.Embed(
            title="📝 งานของคุณ", description="\n".join(lines), color=discord.Color.blurple()
        )
        embed.set_footer(text=f"เสร็จแล้ว {done}/{len(items)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @todo.command(name="done", description="ติ๊กงานว่าเสร็จแล้ว")
    async def todo_done(self, interaction: discord.Interaction, item_id: int) -> None:
        for i in self.my_todos(interaction.user.id):
            if i["id"] == item_id:
                i["done"] = True
                self.save()
                await interaction.response.send_message(f"✅ งาน #{item_id} เสร็จแล้ว", ephemeral=True)
                return
        await interaction.response.send_message("ไม่พบงานนี้", ephemeral=True)

    @todo.command(name="clear", description="ลบงานที่เสร็จแล้วทั้งหมด")
    async def todo_clear(self, interaction: discord.Interaction) -> None:
        items = self.my_todos(interaction.user.id)
        remaining = [i for i in items if not i["done"]]
        removed = len(items) - len(remaining)
        self.todos[str(interaction.user.id)] = remaining
        self.save()
        await interaction.response.send_message(f"🧹 ลบแล้ว {removed} รายการ", ephemeral=True)

    # ---------- Poll ----------
    @app_commands.command(name="poll", description="สร้างโพลโหวตด่วน (คั่นตัวเลือกด้วยคอมมา)")
    @app_commands.describe(question="คำถาม", options="ตัวเลือก 2-10 ข้อ คั่นด้วย , เช่น ใช่, ไม่ใช่")
    async def poll(
        self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 200], options: str
    ) -> None:
        opts = [o.strip() for o in options.split(",") if o.strip()]
        if not 2 <= len(opts) <= 10:
            await interaction.response.send_message("ใส่ตัวเลือก 2-10 ข้อ คั่นด้วยคอมมา", ephemeral=True)
            return
        body = "\n".join(f"{NUM_EMOJI[i]} {o[:100]}" for i, o in enumerate(opts))
        embed = discord.Embed(title=f"📊 {question}", description=body, color=discord.Color.orange())
        embed.set_footer(text=f"สร้างโดย {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        msg = await interaction.original_response()
        for i in range(len(opts)):
            try:
                await msg.add_reaction(NUM_EMOJI[i])
            except discord.HTTPException:
                break


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Productivity(bot))