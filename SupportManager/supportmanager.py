"""
A *full* Red V3 port of the original SupportBot script, plus:
• per-guild PDF storage & upload
• Config-based persistence (no manual JSON)
• optional channel/category IDs configurable at runtime
"""

import asyncio
import datetime
from collections import defaultdict
from pathlib import Path
import discord
from redbot.core import commands, Config, checks
from redbot.core.utils.chat_formatting import box

__all__ = ("SupportManager",)


# ─────────────────────────────
# ░ Role-gate decorators
# ─────────────────────────────
def _role_check(names):
    def predicate(ctx):
        return any(r.name in names for r in ctx.author.roles)
    return commands.check(predicate)


is_small_council = _role_check({"Small Council"})
is_support_staff = _role_check(
    {"Cupbearer", "Unlanded Knight", "Goldcloak", "Imperial Guard"}
)


# ─────────────────────────────
# ░ Award reason table
# ─────────────────────────────
AWARD_REASONS = {
    "help":         (5,  "for effectively helping a member"),
    "firstreply":   (3,  "for being the first to reply to a support question"),
    "followup":     (2,  "for answering multiple follow-up questions in a thread"),
    "link":         (3,  "for providing a helpful link or resource"),
    "checkin":      (3,  "for completing a weekly check-in"),
    "bugreport":    (5,  "for reporting a bug from user feedback"),
    "above":        (15, "for going above and beyond"),
    "reminder":     (2,  "for reminding others to be respectful or helpful"),
    "correction":   (2,  "for spotting and correcting incorrect info"),
    "escalation":   (3,  "for escalating an issue to team leads appropriately"),
    # penalties
    "missedcheckin": (-5, "for missing weekly check-ins without notice"),
    "ignoredping":   (-3, "for ignoring direct support pings without reason"),
    "wronginfo":     (-1, "for giving incorrect information (unintentional)"),
    "rude":         (-15, "for being rude / unprofessional"),
}


# ─────────────────────────────
# ░ Cog
# ─────────────────────────────
class SupportManager(commands.Cog):
    """Full-fat ADOD support-team manager (check-ins, points, PDFs, promotions…)."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0xAD0D515, force_registration=True
        )
        self.config.register_guild(
            checkins_open=False,
            submitted_this_week=[],
            excused_this_week=[],
            pdfs={},           # display_name -> stored filename
            category_id=None,
            channels={         # optional hard IDs if you want them
                "checkins": None,
                "weekly_summary": None,
                "checkin_log": None,
                "promotion_log": None,
            },
        )
        self.config.register_member(
            points=0,
            points_log=[],     # dict{amount,timestamp}
            checkins=[],       # dict{timestamp,message}
        )

    # ===============  Internal helpers  ===============

    @property
    def _data_path(self) -> Path:
        return Path(str(self.config._get_base_dir()))  # type: ignore

    async def _pdf_path(self, guild: discord.Guild, display: str) -> Path:
        g_folder = self._data_path / str(guild.id)
        g_folder.mkdir(parents=True, exist_ok=True)
        return g_folder / f"{display}.pdf"

    # ----- points
    async def _change_points(self, member: discord.Member, delta: int):
        async with self.config.member(member).points() as pts:
            pts += delta
        async with self.config.member(member).points_log() as log:
            log.append({"amount": delta, "timestamp": datetime.datetime.utcnow().isoformat()})

    async def _points(self, member: discord.Member) -> int:
        return await self.config.member(member).points()

    # ----- channel helpers
    async def _get_chan(self, guild: discord.Guild, key: str):
        cid = (await self.config.guild(guild).channels())[key]
        return guild.get_channel(cid) if cid else None

    # ===============  Administration setup  ===============

    @commands.group(name="supportset", invoke_without_command=True)
    @commands.guild_only()
    @checks.is_owner()
    async def supportset(self, ctx):
        """Owner-only: configure channel IDs or category."""
        await ctx.send_help()

    @supportset.command()
    async def channel(self, ctx, slot: str.lower, channel: discord.TextChannel):
        """Set a channel slot (`checkins`, `weekly_summary`, `checkin_log`, `promotion_log`)."""
        if slot not in ("checkins", "weekly_summary", "checkin_log", "promotion_log"):
            await ctx.send("Valid slots: checkins, weekly_summary, checkin_log, promotion_log")
            return
        async with self.config.guild(ctx.guild).channels() as ch:
            ch[slot] = channel.id
        await ctx.send(f"✅ `{slot}` channel set to {channel.mention}")

    @supportset.command()
    async def category(self, ctx, category: discord.CategoryChannel):
        await self.config.guild(ctx.guild).category_id.set(category.id)
        await ctx.send(f"✅ Check-in channels will be created under **{category.name}**")

    # ===============  PDF management  ===============

    @commands.command(name="uploadpdf")
    @commands.guild_only()
    @is_small_council
    async def upload_pdf(self, ctx, *, display_name: str):
        """Attach one PDF and give it a display name."""
        if not ctx.message.attachments:
            await ctx.send("Attach a PDF with this command.")
            return
        att = ctx.message.attachments[0]
        if not att.filename.lower().endswith(".pdf"):
            await ctx.send("That isn’t a PDF.")
            return
        dest = await self._pdf_path(ctx.guild, display_name)
        await att.save(dest)
        async with self.config.guild(ctx.guild).pdfs() as pdfs:
            pdfs[display_name] = att.filename
        await ctx.send(f"📄 Stored **{display_name}** for this server.")

    @commands.command(name="listpdfs")
    @commands.guild_only()
    async def list_pdfs(self, ctx):
        pdfs = await self.config.guild(ctx.guild).pdfs()
        if not pdfs:
            await ctx.send("No PDFs uploaded yet.")
            return
        await ctx.send(box("\n".join(f"- {n}" for n in pdfs)))

    # ===============  Onboarding & role DM-invite  ===============

    @commands.command()
    @commands.guild_only()
    @is_small_council
    async def onboard(self, ctx, member: discord.Member):
        """Give Cupbearer + ADOD Staff and DM all onboarding PDFs."""
        roles = []
        cupbearer = discord.utils.get(ctx.guild.roles, name="Cupbearer")
        staff = discord.utils.get(ctx.guild.roles, name="ADOD Staff")
        if cupbearer:
            roles.append(cupbearer)
        if staff:
            roles.append(staff)
        if roles:
            await member.add_roles(*roles, reason="Support onboarding")
        pdfs = await self.config.guild(ctx.guild).pdfs()
        files = []
        for disp in pdfs:
            p = await self._pdf_path(ctx.guild, disp)
            if p.exists():
                files.append(discord.File(p, filename=p.name))
        try:
            await member.send(
                "👋 Welcome to the Support Team!\nPlease read the attached guides.",
                files=files,
            )
        except discord.Forbidden:
            await ctx.send("Couldn’t DM the user (DMs closed).")
        else:
            await ctx.send(f"Onboarded {member.mention}")

    # ---------- DM invite on role-grant (command to register a role+message)
    @commands.group(name="roledm", invoke_without_command=True)
    @is_small_council
    async def roledm(self, ctx):
        """Set or view automated DMs when a role is granted."""
        await ctx.send_help()

    @roledm.command(name="set")
    async def roledm_set(self, ctx, role: discord.Role, *, message: str):
        """`roledm set @Role <message>` – DM <message> whenever someone gains <Role>."""
        async with self.config.guild(ctx.guild).setdefault("role_dms", {}) as rd:
            rd[str(role.id)] = message
        await ctx.send(f"✅ Stored DM for `{role.name}`.")

    @roledm.command(name="list")
    async def roledm_list(self, ctx):
        rd = (await self.config.guild(ctx.guild).get_raw("role_dms", default={}))
        if not rd:
            await ctx.send("No role DMs set.")
            return
        lines = [f"- <@&{rid}>: {msg[:40]}…"
                 for rid, msg in rd.items()]
        await ctx.send(box("\n".join(lines)))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles:
            return
        added = [r for r in after.roles if r not in before.roles]
        if not added:
            return
        rd = (await self.config.guild(after.guild).get_raw("role_dms", default={}))
        for role in added:
            msg = rd.get(str(role.id))
            if msg:
                try:
                    await after.send(msg)
                except discord.Forbidden:
                    pass

    # ===============  Points & ranking  ===============

    @commands.command()
    async def points(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        await ctx.send(f"{member.mention} has **{await self._points(member)}** points.")

    @commands.command()
    @is_small_council
    async def addpoints(self, ctx, member: discord.Member, pts: int):
        await self._change_points(member, pts)
        await ctx.send(f"Added {pts} points → {await self._points(member)} total.")

    @commands.command()
    @is_small_council
    async def removepoints(self, ctx, member: discord.Member, pts: int):
        await self._change_points(member, -pts)
        await ctx.send(f"Removed {pts} points → {await self._points(member)} total.")

    @commands.command()
    @is_small_council
    async def award(self, ctx, member: discord.Member, reason: str.lower):
        if reason not in AWARD_REASONS:
            await ctx.send(f"Invalid reason. Use one of: {', '.join(AWARD_REASONS)}")
            return
        delta, desc = AWARD_REASONS[reason]
        await self._change_points(member, delta)
        verb = "awarded" if delta > 0 else "deducted"
        await ctx.send(
            f"{verb.title()} **{abs(delta)}** points {desc} → total {await self._points(member)}."
        )

    @commands.command()
    async def awardreasons(self, ctx):
        lines = [f"- {r}: {pts:+}" for r, (pts, _) in AWARD_REASONS.items()]
        await ctx.send(box("\n".join(lines)))

    # ---------- leaderboard
    @commands.command()
    @is_small_council
    async def leaderboard(self, ctx, top: int = 10):
        mems = await self.config.all_members(ctx.guild)
        top = max(1, top)
        sorted_m = sorted(mems.items(), key=lambda kv: kv[1]["points"], reverse=True)[:top]
        lines = [
            f"{i:>2}. {ctx.guild.get_member(uid).display_name:<25} {d['points']} pts"
            for i, (uid, d) in enumerate(sorted_m, 1)
            if ctx.guild.get_member(uid)
        ]
        await ctx.send(box("\n".join(lines)))

    # ===============  Weekly check-ins ===============

    @commands.command()
    @is_small_council
    async def opencheckins(self, ctx):
        await self.config.guild(ctx.guild).checkins_open.set(True)
        await self.config.guild(ctx.guild).submitted_this_week.set([])
        await self.config.guild(ctx.guild).excused_this_week.set([])
        ping_roles = [discord.utils.get(ctx.guild.roles, name=n)
                      for n in ("Cupbearer", "Unlanded Knight", "Goldcloak", "Imperial Guard")]
        ping = " ".join(r.mention for r in ping_roles if r) or "@here"
        channel = await self._get_chan(ctx.guild, "checkins") or ctx.channel
        await channel.send(f"✅ Check-ins are now **open**.\n{ping}")

    @commands.command()
    @is_support_staff
    async def checkin(self, ctx):
        gconf = self.config.guild(ctx.guild)
        if not await gconf.checkins_open():
            await ctx.send("Check-ins are closed.")
            return
        if str(ctx.author.id) in await gconf.submitted_this_week():
            await ctx.send("You already submitted this week.")
            return

        cat_id = await gconf.category_id()
        category = discord.utils.get(ctx.guild.categories, id=cat_id) if cat_id else None
        ch = await ctx.guild.create_text_channel(
            f"checkin-{ctx.author.name}".lower().replace(" ", "-"),
            overwrites={
                ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                ctx.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                discord.utils.get(ctx.guild.roles, name="Small Council"): discord.PermissionOverwrite(view_channel=True)
            },
            category=category,
        )

        questions = [
            "1. How many hours are you available to support this week?",
            "2. Do you have any specific goals for this week?",
            "3. Did you encounter any issues with users or the mod?",
            "4. Do you have any suggestions to improve the support system?",
            "5. How are you feeling about your workload? (1–5)",
            "6. Would you like to discuss anything privately with a team lead?",
        ]

        await ch.send(f"{ctx.author.mention}, please answer each of the following:")
        answers = []
        def check(m): return m.author == ctx.author and m.channel == ch
        try:
            for q in questions:
                await ch.send(q)
                m = await self.bot.wait_for("message", check=check, timeout=300)
                answers.append(m.content)
        except asyncio.TimeoutError:
            await ch.send("⏰ Time-out. Please run `!checkin` again.")
            return

        summary = "\n".join(f"**{q[3:]}** {a}" for q, a in zip(questions, answers))
        async with self.config.member(ctx.author).checkins() as arr:
            arr.append({"timestamp": datetime.datetime.utcnow().isoformat(), "message": summary})
        async with gconf.submitted_this_week() as s:
            s.append(str(ctx.author.id))

        # log to private thread
        log_channel = await self._get_chan(ctx.guild, "checkin_log")
        if log_channel:
            thread = discord.utils.get(log_channel.threads, name=ch.name) \
                     or await log_channel.create_thread(name=ch.name, type=discord.ChannelType.private_thread)
            await thread.send(f"📅 **Weekly check-in from {ctx.author.mention}**\n\n{summary}")

        await self._change_points(ctx.author, 3)
        await ch.send("✅ Check-in recorded (+3 points). This channel will vanish in 10 s.")
        await asyncio.sleep(10)
        await ch.delete()

    # individual accept (mirror of old `!accept`)
    @commands.command()
    @is_small_council
    async def accept(self, ctx):
        if not ctx.channel.name.startswith("checkin-"):
            await ctx.send("Run this inside a check-in channel.")
            return
        await ctx.send("✅ Closing check-in channel.")
        await asyncio.sleep(1)
        await ctx.channel.delete()

    # excuse
    @commands.command()
    @is_small_council
    async def excuse(self, ctx, member: discord.Member):
        async with self.config.guild(ctx.guild).excused_this_week() as ex:
            if str(member.id) in ex:
                await ctx.send(f"{member.display_name} already excused.")
                return
            ex.append(str(member.id))
        await ctx.send(f"{member.mention} excused from this week’s penalties.")

    # closecheckins (+penalties)
    @commands.command()
    @is_small_council
    async def closecheckins(self, ctx):
        gconf = self.config.guild(ctx.guild)
        await gconf.checkins_open.set(False)
        submitted = await gconf.submitted_this_week()
        excused = await gconf.excused_this_week()
        missed = []
        for m in ctx.guild.members:
            if m.bot:
                continue
            if not any(r.name in {"Cupbearer","Unlanded Knight","Goldcloak"} for r in m.roles):
                continue
            uid = str(m.id)
            if uid in submitted or uid in excused:
                continue
            missed.append(m)
            await self._change_points(m, -5)
            try:
                await m.send("You missed this week’s check-in. -5 points.")
            except discord.Forbidden:
                pass
        await ctx.send(f"Check-ins closed. Penalised {len(missed)} members.")

    # ===============  Weekly summary & inactivity report ===============

    @commands.command()
    @is_small_council
    async def summary(self, ctx):
        now = datetime.datetime.utcnow()
        week_ago = now - datetime.timedelta(days=7)
        members = await self.config.all_members(ctx.guild)

        point_earnings = defaultdict(int)
        for uid, data in members.items():
            for log in data["points_log"]:
                if datetime.datetime.fromisoformat(log["timestamp"]) > week_ago:
                    point_earnings[uid] += log["amount"]

        top_earners = sorted(point_earnings.items(), key=lambda kv: kv[1], reverse=True)[:3]
        lines = [f"{ctx.guild.get_member(int(uid)).mention}: +{pts} pts"
                 for uid, pts in top_earners if ctx.guild.get_member(int(uid))]

        embed = discord.Embed(
            title="📊 Weekly Summary",
            description=f"{week_ago.date()} — {now.date()}",
            color=0x00ffcc,
        )
        submitted = len(await self.config.guild(ctx.guild).submitted_this_week())
        support_members = [m for m in ctx.guild.members
                           if any(r.name in {"Cupbearer","Unlanded Knight","Goldcloak"} for r in m.roles)]
        embed.add_field(name="Check-ins submitted",
                        value=f"{submitted}/{len(support_members)}", inline=False)
        embed.add_field(name="Top Earners", value="\n".join(lines) or "None", inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    @is_small_council
    async def inactive(self, ctx):
        now = datetime.datetime.utcnow()
        threshold = now - datetime.timedelta(days=7)
        inactive = []
        for m in ctx.guild.members:
            if m.bot or not any(r.name in {"Cupbearer","Unlanded Knight","Goldcloak"} for r in m.roles):
                continue
            checkins = await self.config.member(m).checkins()
            last = max((datetime.datetime.fromisoformat(c["timestamp"]) for c in checkins), default=None)
            if not last or last < threshold:
                inactive.append((m, last))
        if not inactive:
            await ctx.send("No inactive support members.")
            return
        embed = discord.Embed(title="🛑 Inactive Support Members (7 days+)", color=0xe74c3c)
        embed.description = "\n".join(f"{m.mention} - {last.date() if last else 'Never'}"
                                      for m, last in inactive)
        await ctx.send(embed=embed)

    # ===============  Promotions / demotions ===============

    PROMO_THRESHOLDS = {75: "Unlanded Knight", 200: "Goldcloak", 500: "Imperial Guard"}

    async def _check_promotion(self, member: discord.Member):
        pts = await self._points(member)
        for thresh, rank in sorted(self.PROMO_THRESHOLDS.items()):
            if pts >= thresh:
                return thresh, rank
        return None

    @commands.command()
    @is_small_council
    async def promote(self, ctx, member: discord.Member):
        ranks = ["Cupbearer", "Unlanded Knight", "Goldcloak", "Imperial Guard"]
        idx = max(i for i, r in enumerate(ranks) if discord.utils.get(member.roles, name=r))
        if idx == len(ranks) - 1:
            await ctx.send(f"{member.display_name} is already at top rank.")
            return
        cur = discord.utils.get(ctx.guild.roles, name=ranks[idx])
        nxt = discord.utils.get(ctx.guild.roles, name=ranks[idx + 1])
        if cur:
            await member.remove_roles(cur, reason="Manual promotion")
        if nxt:
            await member.add_roles(nxt, reason="Manual promotion")
        log_ch = await self._get_chan(ctx.guild, "promotion_log")
        if log_ch:
            await log_ch.send(f"📈 {member.mention} promoted to `{nxt.name}` by {ctx.author.mention}")
        await ctx.send(f"✅ Promoted {member.display_name} to **{nxt.name}**")

    @commands.command()
    @is_small_council
    async def demote(self, ctx, member: discord.Member):
        ranks = ["Cupbearer", "Unlanded Knight", "Goldcloak", "Imperial Guard"]
        idxs = [i for i, r in enumerate(ranks) if discord.utils.get(member.roles, name=r)]
        if not idxs:
            await ctx.send("Member has no support rank.")
            return
        idx = max(idxs)
        if idx == 0:
            await ctx.send("Already at lowest rank.")
            return
        cur = discord.utils.get(ctx.guild.roles, name=ranks[idx])
        prv = discord.utils.get(ctx.guild.roles, name=ranks[idx - 1])
        if cur:
            await member.remove_roles(cur, reason="Manual demotion")
        if prv:
            await member.add_roles(prv, reason="Manual demotion")
        log_ch = await self._get_chan(ctx.guild, "promotion_log")
        if log_ch:
            await log_ch.send(f"📉 {member.mention} demoted to `{prv.name}` by {ctx.author.mention}")
        await ctx.send(f"⬇️ Demoted {member.display_name} to **{prv.name}**")

    # ===============  Utility  ===============

    @commands.command()
    @is_small_council
    async def clearchat(self, ctx, limit: int = 100):
        deleted = await ctx.channel.purge(limit=limit)
        m = await ctx.send(f"Cleared {len(deleted)} messages.")
        await asyncio.sleep(5)
        await m.delete()

    # optional: auto-notify owners when member crosses threshold
    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        # points change handled elsewhere
        pass