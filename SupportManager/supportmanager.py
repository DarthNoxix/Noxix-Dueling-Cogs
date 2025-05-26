"""
Full-fat ADOD SupportManager cog  
• Per-guild PDF onboarding  
• Weekly check-ins, points & promotions  
• Owner-configurable channels, category *and* ALL role names/IDs
"""

import asyncio
import datetime
from collections import defaultdict
from pathlib import Path

import discord
from redbot.core import checks, commands, Config, data_manager
from redbot.core.utils.chat_formatting import box

__all__ = ("SupportManager",)

# ─────────────────────────────
# ░ Award-reason table
# ─────────────────────────────
AWARD_REASONS = {
    "help": (5, "for effectively helping a member"),
    "firstreply": (3, "for being the first to reply to a support question"),
    "followup": (2, "for answering multiple follow-up questions in a thread"),
    "link": (3, "for providing a helpful link or resource"),
    "checkin": (3, "for completing a weekly check-in"),
    "bugreport": (5, "for reporting a bug from user feedback"),
    "above": (15, "for going above and beyond"),
    "reminder": (2, "for reminding others to be respectful or helpful"),
    "correction": (2, "for spotting and correcting incorrect info"),
    "escalation": (3, "for escalating an issue to team leads appropriately"),
    # penalties
    "missedcheckin": (-5, "for missing weekly check-ins without notice"),
    "ignoredping": (-3, "for ignoring direct support pings without reason"),
    "wronginfo": (-1, "for giving incorrect information (unintentional)"),
    "rude": (-15, "for being rude / unprofessional"),
}

# ─────────────────────────────
# ░ Helper decorators
# ─────────────────────────────
def sc_check():
    async def predicate(ctx):
        sc_id = await ctx.cog.config.guild(ctx.guild).sc_role_id()
        return any(r.id == sc_id for r in ctx.author.roles) if sc_id else any(
            r.name == "Small Council" for r in ctx.author.roles
        )

    return commands.check(predicate)


def staff_check():
    async def predicate(ctx):
        ids = await ctx.cog.config.guild(ctx.guild).staff_role_ids()
        if ids:
            return any(r.id in ids for r in ctx.author.roles)
        return any(
            r.name in {"Cupbearer", "Unlanded Knight", "Goldcloak", "Imperial Guard"}
            for r in ctx.author.roles
        )

    return commands.check(predicate)


# ─────────────────────────────
# ░ Cog
# ─────────────────────────────
class SupportManager(commands.Cog):
    """ADOD Support-team manager (check-ins, points, PDFs, ranks)."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xAD0D515, force_registration=True)

        # per-guild
        self.config.register_guild(
            sc_role_id=None,
            staff_role_ids=[],
            checkins_open=False,
            submitted_this_week=[],
            excused_this_week=[],
            pdfs={},
            category_id=None,
            channels={
                "checkins": None,
                "weekly_summary": None,
                "checkin_log": None,
                "promotion_log": None,
            },
        )

        # per-member
        self.config.register_member(points=0, points_log=[], checkins=[])

    # ===============  path helpers  ===============
    @property
    def _data_path(self) -> Path:
        return data_manager.cog_data_path(self)

    async def _pdf_path(self, guild: discord.Guild, display: str) -> Path:
        gfolder = self._data_path / str(guild.id)
        gfolder.mkdir(parents=True, exist_ok=True)
        return gfolder / f"{display}.pdf"

    # ===============  point helpers  ===============
    async def _change_points(self, member: discord.Member, delta: int):
        cur = await self.config.member(member).points()
        await self.config.member(member).points.set(cur + delta)

        async with self.config.member(member).points_log() as log:
            log.append(
                {"amount": delta, "timestamp": datetime.datetime.utcnow().isoformat()}
            )

    async def _points(self, member: discord.Member) -> int:
        return await self.config.member(member).points()

    # ===============  channel helper  ===============
    async def _get_chan(self, guild: discord.Guild, key: str):
        cid = (await self.config.guild(guild).channels()).get(key)
        return guild.get_channel(cid) if cid else None

    # ===============  role helpers  ===============
    async def _staff_role_ids(self, guild):
        ids = await self.config.guild(guild).staff_role_ids()
        if ids:
            return ids
        return [r.id for r in guild.roles if r.name in {"Cupbearer", "Unlanded Knight", "Goldcloak", "Imperial Guard"}]

    async def _sc_role_id(self, guild):
        rid = await self.config.guild(guild).sc_role_id()
        if rid:
            return rid
        role = discord.utils.get(guild.roles, name="Small Council")
        return role.id if role else None

    # ─────────────────────────────
    # ░ Owner-level setup (supportset …)
    # ─────────────────────────────
    @commands.group(name="supportset", invoke_without_command=True)
    @commands.guild_only()
    @checks.is_owner()
    async def supportset(self, ctx):
        await ctx.send_help()

    @supportset.command()
    async def channel(self, ctx, slot: str.lower, channel: discord.TextChannel):
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

    @supportset.group(name="roles", invoke_without_command=True)
    async def roles(self, ctx):
        await ctx.send_help()

    @roles.command(name="sc")
    async def roles_sc(self, ctx, role: discord.Role):
        await self.config.guild(ctx.guild).sc_role_id.set(role.id)
        await ctx.send(f"✅ Small-Council role set to {role.name}")

    @roles.command(name="addstaff")
    async def roles_addstaff(self, ctx, role: discord.Role):
        async with self.config.guild(ctx.guild).staff_role_ids() as lst:
            if role.id in lst:
                await ctx.send("Already in list.")
                return
            lst.append(role.id)
        await ctx.send(f"✅ Added **{role.name}** to staff roles.")

    @roles.command(name="removestaff")
    async def roles_removestaff(self, ctx, role: discord.Role):
        async with self.config.guild(ctx.guild).staff_role_ids() as lst:
            if role.id not in lst:
                await ctx.send("That role isn’t in the list.")
                return
            lst.remove(role.id)
        await ctx.send(f"✅ Removed **{role.name}** from staff roles.")

    @roles.command(name="list")
    async def roles_list(self, ctx):
        sc = await self.config.guild(ctx.guild).sc_role_id()
        staff = await self.config.guild(ctx.guild).staff_role_ids()
        sc_disp = f"<@&{sc}>" if sc else "“Small Council” (by name)"
        staff_disp = ", ".join(f"<@&{rid}>" for rid in staff) or "Default hard-coded names"
        await ctx.send(f"**Small Council:** {sc_disp}\n**Support roles:** {staff_disp}")

    # ─────────────────────────────
    # ░ PDF onboarding
    # ─────────────────────────────
    @commands.command(name="uploadpdf")
    @sc_check()
    async def upload_pdf(self, ctx, *, display_name: str):
        if not ctx.message.attachments:
            await ctx.send("Attach one PDF to this command.")
            return
        att = ctx.message.attachments[0]
        if not att.filename.lower().endswith(".pdf"):
            await ctx.send("That’s not a PDF.")
            return
        dest = await self._pdf_path(ctx.guild, display_name)
        await att.save(dest)
        async with self.config.guild(ctx.guild).pdfs() as pdfs:
            pdfs[display_name] = att.filename
        await ctx.send(f"📄 Stored **{display_name}**.")

    @commands.command(name="listpdfs")
    async def list_pdfs(self, ctx):
        pdfs = await self.config.guild(ctx.guild).pdfs()
        await ctx.send("No PDFs uploaded." if not pdfs else box("\n".join(f"- {n}" for n in pdfs)))

    @commands.command()
    @sc_check()
    async def onboard(self, ctx, member: discord.Member):
        staff_ids = await self._staff_role_ids(ctx.guild)
        first_role = discord.utils.get(ctx.guild.roles, id=staff_ids[0]) if staff_ids else None
        if first_role:
            await member.add_roles(first_role, reason="Support onboarding")

        files = [
            discord.File(await self._pdf_path(ctx.guild, d), filename=f"{d}.pdf")
            for d in (await self.config.guild(ctx.guild).pdfs())
            if (await self._pdf_path(ctx.guild, d)).exists()
        ]
        try:
            await member.send("👋 Welcome to the Support Team! Please read the attached guides.", files=files)
        except discord.Forbidden:
            await ctx.send("Couldn’t DM the user (DMs closed).")
        else:
            await ctx.send(f"✅ Onboarded {member.mention}")

    # ─────────────────────────────
    # ░ Points & awards
    # ─────────────────────────────
    @commands.command()
    async def points(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        await ctx.send(f"{member.mention} has **{await self._points(member)}** points.")

    @commands.command()
    @sc_check()
    async def addpoints(self, ctx, member: discord.Member, pts: int):
        await self._change_points(member, pts)
        await ctx.send(f"Added {pts} points → {await self._points(member)} total.")

    @commands.command()
    @sc_check()
    async def removepoints(self, ctx, member: discord.Member, pts: int):
        await self._change_points(member, -pts)
        await ctx.send(f"Removed {pts} points → {await self._points(member)} total.")

    @commands.command()
    @sc_check()
    async def award(self, ctx, member: discord.Member, reason: str.lower):
        if reason not in AWARD_REASONS:
            await ctx.send(f"Invalid reason. Use one of: {', '.join(AWARD_REASONS)}")
            return
        delta, desc = AWARD_REASONS[reason]
        await self._change_points(member, delta)
        verb = "awarded" if delta > 0 else "deducted"
        await ctx.send(f"{verb.title()} **{abs(delta)}** points {desc} → total {await self._points(member)}.")

    @commands.command()
    async def awardreasons(self, ctx):
        await ctx.send(box("\n".join(f"- {r}: {p:+}" for r, (p, _) in AWARD_REASONS.items())))

    @commands.command()
    @sc_check()
    async def leaderboard(self, ctx, top: int = 10):
        mdata = await self.config.all_members(ctx.guild)
        top = sorted(mdata.items(), key=lambda kv: kv[1]["points"], reverse=True)[: max(1, top)]
        lines = [
            f"{i:>2}. {ctx.guild.get_member(uid).display_name:<25} {d['points']} pts"
            for i, (uid, d) in enumerate(top, 1)
            if ctx.guild.get_member(uid)
        ]
        await ctx.send(box("\n".join(lines)))

    # ─────────────────────────────
    # ░ Weekly check-in workflow
    # ─────────────────────────────
    @commands.command()
    @sc_check()
    async def opencheckins(self, ctx):
        g = self.config.guild(ctx.guild)
        await g.checkins_open.set(True)
        await g.submitted_this_week.set([])
        await g.excused_this_week.set([])
        ping = " ".join(f"<@&{rid}>" for rid in await self._staff_role_ids(ctx.guild)) or "@here"
        await (await self._get_chan(ctx.guild, "checkins") or ctx.channel).send(
            f"✅ Check-ins are now **open**.\n{ping}"
        )

    @commands.command()
    @staff_check()
    async def checkin(self, ctx):
        gconf = self.config.guild(ctx.guild)
        if not await gconf.checkins_open():
            await ctx.send("Check-ins are closed.")
            return
        if str(ctx.author.id) in await gconf.submitted_this_week():
            await ctx.send("You already submitted this week.")
            return

        # create priv channel
        cat = discord.utils.get(ctx.guild.categories, id=await gconf.category_id())
        ch = await ctx.guild.create_text_channel(
            f"checkin-{ctx.author.name}".lower().replace(" ", "-"),
            overwrites={
                ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                ctx.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                discord.utils.get(ctx.guild.roles, id=await self._sc_role_id(ctx.guild)):
                    discord.PermissionOverwrite(view_channel=True),
            },
            category=cat,
        )

        q_text = [
            "1. How many hours are you available to support this week?",
            "2. Do you have any specific goals for this week?",
            "3. Did you encounter any issues with users or the mod?",
            "4. Do you have any suggestions to improve the support system?",
            "5. How are you feeling about your workload? (1–5)",
            "6. Would you like to discuss anything privately with a team lead? (yes / no)",
        ]
        await ch.send(f"{ctx.author.mention}, please answer each question:")
        answers = []
        def chk(m): return m.author == ctx.author and m.channel == ch
        yes_words = {"yes", "y", "yep", "yeah", "sure", "please", "would like", "i do"}

        try:
            for q in q_text[:-1]:
                await ch.send(q)
                m = await self.bot.wait_for("message", check=chk, timeout=300)
                answers.append(m.content)

            # question 6 – force yes/no
            while True:
                await ch.send(q_text[-1])
                m6 = await self.bot.wait_for("message", check=chk, timeout=300)
                ans6 = m6.content.lower().strip()
                if ans6 in yes_words or ans6 in {"no", "n"}:
                    answers.append(m6.content)
                    break
                await ch.send("Please answer **yes** or **no**.")
        except asyncio.TimeoutError:
            await ch.send("⏰ Time-out. Run `!checkin` again later.")
            return

        # store & flag submitted
        summary = "\n".join(f"**{q[3:]}** {a}" for q, a in zip(q_text, answers))
        async with self.config.member(ctx.author).checkins() as arr:
            arr.append({"timestamp": datetime.datetime.utcnow().isoformat(), "message": summary})
        async with gconf.submitted_this_week() as s:
            s.append(str(ctx.author.id))

        # log thread
        log_ch = await self._get_chan(ctx.guild, "checkin_log")
        thread = None
        if log_ch:
            thread = discord.utils.get(log_ch.threads, name=ch.name) or await log_ch.create_thread(
                name=ch.name, type=discord.ChannelType.private_thread
            )
            await thread.send(f"📅 **Weekly check-in from {ctx.author.mention}**\n\n{summary}")

        # ping SC if they said yes
        if answers[-1].lower().strip() in yes_words and thread:
            sc_role = discord.utils.get(ctx.guild.roles, id=await self._sc_role_id(ctx.guild))
            await thread.send(f"{sc_role.mention if sc_role else '@here'} – {ctx.author.mention} would like a private chat with a team lead.")

        await self._change_points(ctx.author, 3)
        await ch.send("✅ Check-in recorded (+3 pts). Closing in 10 s.")
        await asyncio.sleep(10)
        await ch.delete()

    @commands.command()
    @sc_check()
    async def accept(self, ctx):
        if not ctx.channel.name.startswith("checkin-"):
            await ctx.send("Run this in a check-in channel.")
            return
        await ctx.send("✅ Closing channel…")
        await asyncio.sleep(1)
        await ctx.channel.delete()

    @commands.command()
    @sc_check()
    async def excuse(self, ctx, member: discord.Member):
        async with self.config.guild(ctx.guild).excused_this_week() as ex:
            if str(member.id) in ex:
                await ctx.send("They are already excused.")
                return
            ex.append(str(member.id))
        await ctx.send(f"{member.mention} excused for this week.")

    @commands.command()
    @sc_check()
    async def closecheckins(self, ctx):
        g = self.config.guild(ctx.guild)
        await g.checkins_open.set(False)
        submitted = await g.submitted_this_week()
        excused = await g.excused_this_week()
        staff_ids = await self._staff_role_ids(ctx.guild)
        missed = [
            m for m in ctx.guild.members
            if not m.bot and any(r.id in staff_ids for r in m.roles) and str(m.id) not in submitted + excused
        ]
        for m in missed:
            await self._change_points(m, -5)
            try:
                await m.send("You missed this week’s check-in. **-5 pts**.")
            except discord.Forbidden:
                pass
        await ctx.send(f"Check-ins closed. Penalised {len(missed)} members.")

    # ─────────────────────────────
    # ░ Summaries & inactivity
        # ─────────────────────────────
    @commands.command()
    @sc_check()
    async def summary(self, ctx):
        now = datetime.datetime.utcnow()
        week_ago = now - datetime.timedelta(days=7)
        pdata = await self.config.all_members(ctx.guild)

        earnings = defaultdict(int)
        for uid, d in pdata.items():
            for log in d["points_log"]:
                if datetime.datetime.fromisoformat(log["timestamp"]) > week_ago:
                    earnings[uid] += log["amount"]

        top = sorted(earnings.items(), key=lambda kv: kv[1], reverse=True)[:3]
        embed = discord.Embed(
            title="📊 Weekly Summary",
            description=f"{week_ago.date()} — {now.date()}",
            color=0x00FFCC,
        )
        embed.add_field(
            name="Top earners",
            value="\n".join(f"{ctx.guild.get_member(int(uid)).mention}: +{pts} pts" for uid, pts in top) or "None",
            inline=False,
        )

        # ✅ FIXED: call `await` first, then use result in list comp
        staff_ids = await self._staff_role_ids(ctx.guild)
        total_staff = len([m for m in ctx.guild.members if any(r.id in staff_ids for r in m.roles)])

        submitted = len(await self.config.guild(ctx.guild).submitted_this_week())
        embed.add_field(name="Check-ins submitted", value=f"{submitted}/{total_staff}", inline=False)
        await ctx.send(embed=embed)


    @commands.command()
    @sc_check()
    async def inactive(self, ctx):
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        staff_ids = await self._staff_role_ids(ctx.guild)
        inactive = []
        for m in ctx.guild.members:
            if m.bot or not any(r.id in staff_ids for r in m.roles):
                continue
            last_ts = None
            for c in reversed(await self.config.member(m).checkins()):
                last_ts = datetime.datetime.fromisoformat(c["timestamp"])
                break
            if not last_ts or last_ts < cutoff:
                inactive.append((m, last_ts))
        if not inactive:
            await ctx.send("No inactive support members.")
            return
        embed = discord.Embed(title="🛑 Inactive Support Members (7 days+)", color=0xE74C3C)
        embed.description = "\n".join(
            f"{m.mention} – {last.date() if last else 'Never'}" for m, last in inactive
        )
        await ctx.send(embed=embed)

    # ─────────────────────────────
    # ░ Promotions / demotions
    # ─────────────────────────────
    @commands.command()
    @sc_check()
    async def promote(self, ctx, member: discord.Member):
        roles = [discord.utils.get(ctx.guild.roles, id=r) for r in await self._staff_role_ids(ctx.guild)]
        roles = [r for r in roles if r]
        idx = next((i for i, r in enumerate(roles) if r in member.roles), -1)
        if idx == -1 or idx == len(roles) - 1:
            await ctx.send("Cannot promote further.")
            return
        await member.remove_roles(roles[idx], reason="Promotion")
        await member.add_roles(roles[idx + 1], reason="Promotion")
        log = await self._get_chan(ctx.guild, "promotion_log")
        if log:
            await log.send(f"📈 {member.mention} promoted to **{roles[idx + 1].name}** by {ctx.author.mention}")
        await ctx.send(f"✅ Promoted {member.display_name}.")

    @commands.command()
    @sc_check()
    async def demote(self, ctx, member: discord.Member):
        roles = [discord.utils.get(ctx.guild.roles, id=r) for r in await self._staff_role_ids(ctx.guild)]
        roles = [r for r in roles if r]
        idx = next((i for i, r in enumerate(roles) if r in member.roles), -1)
        if idx <= 0:
            await ctx.send("Cannot demote further.")
            return
        await member.remove_roles(roles[idx], reason="Demotion")
        await member.add_roles(roles[idx - 1], reason="Demotion")
        log = await self._get_chan(ctx.guild, "promotion_log")
        if log:
            await log.send(f"📉 {member.mention} demoted to **{roles[idx - 1].name}** by {ctx.author.mention}")
        await ctx.send(f"⬇️ Demoted {member.display_name}.")

