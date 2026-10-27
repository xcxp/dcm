import asyncio
import csv
import io
import logging
import os
import re
import time
from datetime import time as dtime
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from storage import load_json, save_json

log = logging.getLogger("finance")

TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Bangkok"))
ALERT_FILE = "alerts.json"
MAX_ALERTS_PER_USER = 10
SYMBOL_RE = re.compile(r"^[a-z0-9.^\-]{1,15}$")

COIN_ALIASES = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "bnb": "binancecoin",
    "xrp": "ripple", "ada": "cardano", "doge": "dogecoin", "dot": "polkadot",
    "avax": "avalanche-2", "link": "chainlink", "ton": "the-open-network",
    "trx": "tron", "usdt": "tether", "usdc": "usd-coin",
}


def resolve_coin(text: str) -> str:
    t = text.strip().lower()
    return COIN_ALIASES.get(t, t)


def fmt_price(x: float) -> str:
    if x >= 1:
        return f"{x:,.2f}"
    return f"{x:.6f}".rstrip("0").rstrip(".")


def error_embed(msg: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {msg}", color=discord.Color.red())


class RefreshView(discord.ui.View):
    """ปุ่มรีเฟรช: builder คือฟังก์ชัน async ที่คืน Embed"""

    def __init__(self, builder) -> None:
        super().__init__(timeout=180)
        self.builder = builder
        self.message: discord.Message | None = None

    @discord.ui.button(label="รีเฟรช", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        embed = await self.builder()
        await interaction.edit_original_response(embed=embed, view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Finance(commands.Cog):
    alert = app_commands.Group(name="alert", description="แจ้งเตือนราคาคริปโต")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.alerts: list[dict] = []
        self._fx_cache: tuple[float, dict] = (0.0, {})
        self.digest_channel_id = int(os.getenv("DIGEST_CHANNEL_ID") or 0)

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (compatible; FinTechBot/1.0)"},
            timeout=aiohttp.ClientTimeout(total=15),
        )
        self.alerts = load_json(ALERT_FILE, [])
        self.alert_loop.start()
        if self.digest_channel_id:
            self.daily_digest.start()

    async def cog_unload(self) -> None:
        self.alert_loop.cancel()
        self.daily_digest.cancel()
        if self.session:
            await self.session.close()

    def save_alerts(self) -> None:
        save_json(ALERT_FILE, self.alerts)

    # ---------- ดึงข้อมูล ----------
    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        async with self.session.get(url, params=params, headers=headers) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def crypto_prices(self, ids: list[str], vs: str = "usd") -> dict:
        key = os.getenv("COINGECKO_API_KEY", "").strip()
        return await self.get_json(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": ",".join(ids),
                "vs_currencies": vs,
                "include_24hr_change": "true",
                "include_market_cap": "true",
            },
            headers={"x-cg-demo-api-key": key} if key else None,  # ส่งเฉพาะ CoinGecko
        )

    async def fx_rates(self) -> dict:
        ts, rates = self._fx_cache
        if rates and time.time() - ts < 600:
            return rates
        data = await self.get_json("https://open.er-api.com/v6/latest/USD")
        if data.get("result") != "success":
            raise RuntimeError("FX API error")
        self._fx_cache = (time.time(), data["rates"])
        return data["rates"]

    # ---------- สร้าง Embed ----------
    async def crypto_embed(self, coin: str, currency: str = "usd") -> discord.Embed:
        cid, vs = resolve_coin(coin), currency.strip().lower()
        try:
            data = await self.crypto_prices([cid], vs)
        except Exception:
            log.exception("ดึงราคาคริปโตไม่สำเร็จ")
            return error_embed("ดึงราคาไม่สำเร็จ (API อาจจำกัดจำนวนครั้ง) ลองใหม่อีกสักครู่")
        d = data.get(cid)
        if not d or vs not in d:
            return error_embed(f"ไม่พบเหรียญ/สกุลเงิน `{coin}` ลองใช้ id จาก coingecko.com เช่น `bitcoin`")
        change = d.get(f"{vs}_24h_change") or 0.0
        embed = discord.Embed(
            title=f"🪙 {cid} / {vs.upper()}",
            color=discord.Color.green() if change >= 0 else discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="ราคา", value=f"**{fmt_price(d[vs])}** {vs.upper()}")
        embed.add_field(name="24 ชม.", value=f"{'▲' if change >= 0 else '▼'} {change:+.2f}%")
        mcap = d.get(f"{vs}_market_cap")
        if mcap:
            embed.add_field(name="Market Cap", value=f"{mcap:,.0f}")
        embed.set_footer(text="ข้อมูลจาก CoinGecko")
        return embed

    async def stock_embed(self, symbol: str) -> discord.Embed:
        sym = symbol.strip().lower()
        if not SYMBOL_RE.match(sym):
            return error_embed("สัญลักษณ์หุ้นไม่ถูกต้อง")
        if "." not in sym and not sym.startswith("^"):
            sym += ".us"
        url = f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
        try:
            async with self.session.get(url) as r:
                r.raise_for_status()
                text = await r.text()
            row = list(csv.DictReader(io.StringIO(text)))[0]
            close, open_ = float(row["Close"]), float(row["Open"])
            high, low = float(row["High"]), float(row["Low"])
        except (aiohttp.ClientError, asyncio.TimeoutError, IndexError, KeyError, ValueError):
            return error_embed(f"ไม่พบ `{symbol}` หรือดึงข้อมูลไม่สำเร็จ (รองรับตลาดสหรัฐเป็นหลัก เช่น `aapl`)")
        pct = (close - open_) / open_ * 100 if open_ else 0.0
        embed = discord.Embed(
            title=f"📈 {row.get('Symbol', sym).upper()}",
            color=discord.Color.green() if pct >= 0 else discord.Color.red(),
        )
        embed.add_field(name="ล่าสุด", value=f"**{fmt_price(close)}**")
        embed.add_field(name="เทียบราคาเปิด", value=f"{'▲' if pct >= 0 else '▼'} {pct:+.2f}%")
        embed.add_field(name="สูง / ต่ำ", value=f"{fmt_price(high)} / {fmt_price(low)}")
        embed.set_footer(text=f"ข้อมูลจาก Stooq • วันที่ {row.get('Date', '-')} • อาจล่าช้า")
        return embed

    async def digest_embed(self) -> discord.Embed:
        coins = [c.strip() for c in os.getenv("DIGEST_COINS", "bitcoin,ethereum,solana").split(",") if c.strip()]
        fx_list = [c.strip().upper() for c in os.getenv("DIGEST_FX", "USD,EUR,JPY,CNY").split(",") if c.strip()]
        base = os.getenv("DIGEST_BASE_CURRENCY", "THB").strip().upper()
        embed = discord.Embed(
            title="☀️ สรุปตลาดตอนนี้",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        try:
            ids = [resolve_coin(c) for c in coins]
            data = await self.crypto_prices(ids, "usd")
            lines = []
            for cid in ids:
                d = data.get(cid)
                if d:
                    ch = d.get("usd_24h_change") or 0.0
                    lines.append(f"{'🟢' if ch >= 0 else '🔴'} **{cid}** ${fmt_price(d['usd'])} ({ch:+.2f}%)")
            embed.add_field(name="🪙 Crypto (USD)", value="\n".join(lines) or "-", inline=False)
        except Exception:
            log.exception("digest: crypto ล้มเหลว")
            embed.add_field(name="🪙 Crypto", value="ดึงข้อมูลไม่สำเร็จ", inline=False)
        try:
            rates = await self.fx_rates()
            lines = [
                f"1 {c} = {rates[base] / rates[c]:,.2f} {base}"
                for c in fx_list
                if c in rates and base in rates and c != base
            ]
            embed.add_field(name=f"💱 อัตราแลกเปลี่ยน → {base}", value="\n".join(lines) or "-", inline=False)
        except Exception:
            log.exception("digest: FX ล้มเหลว")
            embed.add_field(name="💱 FX", value="ดึงข้อมูลไม่สำเร็จ", inline=False)
        embed.set_footer(text="CoinGecko • open.er-api.com")
        return embed

    # ---------- คำสั่ง ----------
    @app_commands.command(name="crypto", description="ดูราคาคริปโต เช่น btc, eth, sol")
    @app_commands.describe(coin="ตัวย่อหรือ id เช่น btc / bitcoin", currency="สกุลเงิน เช่น usd, thb")
    @app_commands.checks.cooldown(1, 5.0)
    async def crypto(self, interaction: discord.Interaction, coin: str, currency: str = "usd") -> None:
        await interaction.response.defer()
        view = RefreshView(lambda: self.crypto_embed(coin, currency))
        embed = await self.crypto_embed(coin, currency)
        view.message = await interaction.followup.send(embed=embed, view=view, wait=True)

    @app_commands.command(name="stock", description="ดูราคาหุ้น (ค่าเริ่มต้นตลาดสหรัฐ เช่น aapl, nvda)")
    @app_commands.describe(symbol="สัญลักษณ์ เช่น aapl หรือ bmw.de")
    @app_commands.checks.cooldown(1, 5.0)
    async def stock(self, interaction: discord.Interaction, symbol: str) -> None:
        await interaction.response.defer()
        view = RefreshView(lambda: self.stock_embed(symbol))
        embed = await self.stock_embed(symbol)
        view.message = await interaction.followup.send(embed=embed, view=view, wait=True)

    @app_commands.command(name="fx", description="แปลงสกุลเงิน เช่น 100 USD เป็น THB")
    @app_commands.describe(amount="จำนวนเงิน", from_cur="สกุลต้นทาง เช่น USD", to_cur="สกุลปลายทาง เช่น THB")
    @app_commands.rename(from_cur="from", to_cur="to")
    @app_commands.checks.cooldown(1, 3.0)
    async def fx(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[float, 0, 1e12],
        from_cur: str = "USD",
        to_cur: str = "THB",
    ) -> None:
        await interaction.response.defer()
        f, t = from_cur.strip().upper(), to_cur.strip().upper()
        try:
            rates = await self.fx_rates()
        except Exception:
            log.exception("ดึง FX ไม่สำเร็จ")
            await interaction.followup.send(embed=error_embed("ดึงอัตราแลกเปลี่ยนไม่สำเร็จ ลองใหม่อีกครั้ง"))
            return
        if f not in rates or t not in rates:
            await interaction.followup.send(embed=error_embed("ไม่รู้จักสกุลเงินที่ระบุ (ใช้รหัส 3 ตัว เช่น USD, THB)"))
            return
        rate = rates[t] / rates[f]
        embed = discord.Embed(
            title="💱 แปลงสกุลเงิน",
            description=f"**{amount:,.2f} {f}** = **{amount * rate:,.2f} {t}**",
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"1 {f} = {rate:,.4f} {t} • open.er-api.com")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="digest", description="สรุปตลาดตอนนี้ (คริปโต + อัตราแลกเปลี่ยน)")
    @app_commands.checks.cooldown(1, 15.0)
    async def digest(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view = RefreshView(self.digest_embed)
        embed = await self.digest_embed()
        view.message = await interaction.followup.send(embed=embed, view=view, wait=True)

    # ---------- แจ้งเตือนราคา ----------
    @alert.command(name="add", description="แจ้งเตือนเมื่อราคา (USD) แตะเป้า")
    @app_commands.describe(coin="เช่น btc", direction="ทิศทาง", price="ราคาเป้าหมาย (USD)")
    @app_commands.choices(
        direction=[
            app_commands.Choice(name="สูงกว่าหรือเท่ากับ (above)", value="above"),
            app_commands.Choice(name="ต่ำกว่าหรือเท่ากับ (below)", value="below"),
        ]
    )
    async def alert_add(
        self,
        interaction: discord.Interaction,
        coin: str,
        direction: app_commands.Choice[str],
        price: app_commands.Range[float, 0, 1e12],
    ) -> None:
        mine = [a for a in self.alerts if a["user_id"] == interaction.user.id]
        if len(mine) >= MAX_ALERTS_PER_USER:
            await interaction.response.send_message(
                f"ตั้งได้สูงสุด {MAX_ALERTS_PER_USER} รายการ ลบอันเก่าด้วย `/alert remove`", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        cid = resolve_coin(coin)
        try:
            data = await self.crypto_prices([cid], "usd")
        except Exception:
            log.exception("alert add: ดึงราคาไม่สำเร็จ")
            await interaction.followup.send(embed=error_embed("ดึงราคาไม่สำเร็จ ลองใหม่อีกครั้ง"), ephemeral=True)
            return
        if cid not in data or "usd" not in data[cid]:
            await interaction.followup.send(embed=error_embed(f"ไม่พบเหรียญ `{coin}`"), ephemeral=True)
            return
        alert_id = max((a["id"] for a in self.alerts), default=0) + 1
        self.alerts.append(
            {
                "id": alert_id,
                "user_id": interaction.user.id,
                "channel_id": interaction.channel_id,
                "coin": cid,
                "direction": direction.value,
                "target": price,
            }
        )
        self.save_alerts()
        sign = "≥" if direction.value == "above" else "≤"
        await interaction.followup.send(
            f"✅ ตั้งแจ้งเตือน #{alert_id}: **{cid}** {sign} ${fmt_price(price)} (ตอนนี้ ${fmt_price(data[cid]['usd'])})",
            ephemeral=True,
        )

    @alert.command(name="list", description="ดูแจ้งเตือนของคุณ")
    async def alert_list(self, interaction: discord.Interaction) -> None:
        mine = [a for a in self.alerts if a["user_id"] == interaction.user.id]
        if not mine:
            await interaction.response.send_message("ยังไม่มีแจ้งเตือน", ephemeral=True)
            return
        lines = [
            f"`#{a['id']}` {a['coin']} {'≥' if a['direction'] == 'above' else '≤'} ${fmt_price(a['target'])}"
            for a in mine
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @alert.command(name="remove", description="ลบแจ้งเตือนตามหมายเลข")
    @app_commands.describe(alert_id="หมายเลขจาก /alert list")
    async def alert_remove(self, interaction: discord.Interaction, alert_id: int) -> None:
        for a in self.alerts:
            if a["id"] == alert_id and a["user_id"] == interaction.user.id:
                self.alerts.remove(a)
                self.save_alerts()
                await interaction.response.send_message(f"🗑️ ลบ #{alert_id} แล้ว", ephemeral=True)
                return
        await interaction.response.send_message("ไม่พบรายการนี้ของคุณ", ephemeral=True)

    @tasks.loop(minutes=2)
    async def alert_loop(self) -> None:
        try:
            if not self.alerts:
                return
            ids = sorted({a["coin"] for a in self.alerts})
            data = await self.crypto_prices(ids, "usd")
            fired = False
            for a in list(self.alerts):
                price = (data.get(a["coin"]) or {}).get("usd")
                if price is None:
                    continue
                hit = price >= a["target"] if a["direction"] == "above" else price <= a["target"]
                if not hit:
                    continue
                self.alerts.remove(a)
                fired = True
                text = (
                    f"🔔 <@{a['user_id']}> **{a['coin']}** ตอนนี้ ${fmt_price(price)} "
                    f"({'≥' if a['direction'] == 'above' else '≤'} เป้า ${fmt_price(a['target'])})"
                )
                mentions = discord.AllowedMentions(users=[discord.Object(id=a["user_id"])])
                channel = self.bot.get_channel(a["channel_id"])
                try:
                    if isinstance(channel, discord.abc.Messageable):
                        await channel.send(text, allowed_mentions=mentions)
                    else:
                        user = await self.bot.fetch_user(a["user_id"])
                        await user.send(text)
                except discord.HTTPException:
                    log.warning("ส่งแจ้งเตือน #%s ไม่สำเร็จ", a["id"])
            if fired:
                self.save_alerts()
        except Exception:
            log.exception("alert_loop error")

    @alert_loop.before_loop
    async def before_alert_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ---------- สรุปตลาดอัตโนมัติ 08:30 ----------
    @tasks.loop(time=dtime(hour=8, minute=30, tzinfo=TZ))
    async def daily_digest(self) -> None:
        try:
            channel = self.bot.get_channel(self.digest_channel_id)
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(embed=await self.digest_embed())
        except Exception:
            log.exception("daily_digest error")

    @daily_digest.before_loop
    async def before_daily_digest(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Finance(bot))