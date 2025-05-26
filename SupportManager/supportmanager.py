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
from redbot.core import data_manager 
from redbot.core import commands, Config, checks
from redbot.core.utils.chat_formatting import box

__all__ = ("SupportManager",)


# ─────────────────────────────
# ░  Award-reason table
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
# ░  Helper decorators (role-gated)
# ─────────────────────────────
def sc_check():
    """Check if author has the configured Small-Council role (or fallback name)."""

    async def predicate(ctx):
        cfg = ctx.cog.config.guild(ctx.guild)
        sc_id = await cfg.sc_role_id()
        if sc_id:
            return any(r.id == sc_id for r in ctx.author.roles)
        return any(r.name == "Small Council" for r in ctx.author.roles)

    return commands.check(predicate)


def staff_check():
    """Check if author has *any* configured support-staff role."""

    async def predicate(ctx):
        cfg = ctx.cog.config.guild(ctx.guild)
        custom_ids = await cfg.staff_role_ids()
        if custom_ids:
            return any(r.id in custom_ids for r in ctx.author.roles)
        return any(r.name in {"Cupbearer", "Unlanded Knight", "Goldcloak", "Imperial Guard"}
                   for r in ctx.author.roles)

    return commands.check(predicate)


# ─────────────────────────────
# ░  Cog
# ─────────────────────────────
class SupportManager(commands.Cog):
    """ADOD Support-team manager (check-ins, points, PDFs, ranks)."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xAD0D515, force_registration=True)

        # Per-guild config
        self.config.register_guild(
            sc_role_id=None,          # int | None
            staff_role_ids=[],        # list[int]
            checkins_open=False,
            submitted_this_week=[],
            excused_this_week=[],
            pdfs={},                  # display -> stored filename
            category_id=None,
            channels={                # optional hard IDs
                "checkins": None,
                "weekly_summary": None,
                "checkin_log": None,
                "promotion_log": None,
            },
        )

        # Per-member
        self.config.register_member(
            points=0,
            points_log=[],            # list[{amount,timestamp}]
            checkins=[],              # list[{timestamp,message}]
        )

    # ===============  Path helpers  ===============
    from redbot.core import data_manager  # (already in your imports—keep only one copy)

    @property
    def _data_path(self) -> Path:
        """
        Per-cog data directory guaranteed by Red.
        Example: …/data/SupportManager/
        """
        return data_manager.cog_data_path(self)

    async def _pdf_path(self, guild: discord.Guild, display: str) -> Path:
        gfolder = self._data_path / str(guild.id)
        gfolder.mkdir(parents=True, exist_ok=True)
        return gfolder / f"{display}.pdf"


    # ===============  Point helpers  ===============
    async def _change_points(self, member: discord.Member, delta: int):
        async with self.config.member(member).points() as pts:
            pts += delta
        async with self.config.member(member).points_log() as log:
            log.append({"amount": delta, "timestamp": datetime.datetime.utcnow().isoformat()})

    async def _points(self, member: discord.Member) -> int:
        return await self.config.member(member).points()

    # ===============  Channel helper  ===============
    async def _get_chan(self, guild: discord.Guild, key: str):
        cid = (await self.config.guild(guild).channels()).get(key)
        return guild.get_channel(cid) if cid else None

    # ===============  Quick access to role IDs/names  ===============
    async def _staff_role_ids(self, guild):
        ids = await self.config.guild(guild).staff_role_ids()
        if ids:
            return ids
        # fallback to default names
        return [r.id for r in guild.roles if r.name in
                {"Cupbearer", "Unlanded Knight", "Goldcloak", "Imperial Guard"}]

    async def _sc_role_id(self, guild):
        rid = await self.config.guild(guild).sc_role_id()
        if rid:
            return rid
        role = discord.utils.get(guild.roles, name="Small Council")
        return role.id if role else None

    # ─────────────────────────────
    # ░  Owner-level config commands
    # ─────────────────────────────
    @commands.group(name="supportset", invoke_without_command=True)
    @commands.guild_only()
    @checks.is_owner()
    async def supportset(self, ctx):
        """Owner-only: configure channels, category *and* roles."""
        await ctx.send_help()

    # --- channel / category ---
    @supportset.command()
    async def channel(self, ctx, slot: str.lower, channel: discord.TextChannel):
        """Map a slot (`checkins`, `weekly_summary`, `checkin_log`, `promotion_log`)."""
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

    # --- role config ---
    @supportset.group(name="roles", invoke_without_command=True)
    async def roles(self, ctx):
        """Configure Small-Council & support-staff roles."""
        await ctx.send_help()

    @roles.command(name="sc")
    async def roles_sc(self, ctx, role: discord.Role):
        """`supportset roles sc @Role` – sets the Small Council role."""
        await self.config.guild(ctx.guild).sc_role_id.set(role.id)
        await ctx.send(f"✅ Small-Council role set to {role.name}")

    @roles.command(name="addstaff")
    async def roles_addstaff(self, ctx, role: discord.Role):
        """Add a role to the support-staff list."""
        async with self.config.guild(ctx.guild).staff_role_ids() as lst:
            if role.id in lst:
                await ctx.send("Already in the list.")
                return
            lst.append(role.id)
        await ctx.send(f"✅ Added **{role.name}** to staff roles.")

    @roles.command(name="removestaff")
    async def roles_removestaff(self, ctx, role: discord.Role):
        """Remove a role from the support-staff list."""
        async with self.config.guild(ctx.guild).staff_role_ids() as lst:
            if role.id not in lst:
                await ctx.send("That role isn’t in the list.")
                return
            lst.remove(role.id)
        await ctx.send(f"✅ Removed **{role.name}** from staff roles.")

    @roles.command(name="list")
    async def roles_list(self, ctx):
        gconf = self.config.guild(ctx.guild)
        sc = await gconf.sc_role_id()
        staff = await gconf.staff_role_ids()
        sc_disp = f"<@&{sc}>" if sc else "“Small Council” (by name)"
        staff_disp = ", ".join(f"<@&{rid}>" for rid in staff) or "Default hard-coded names"
        await ctx.send(f"**Small Council:** {sc_disp}\n**Support roles:** {staff_disp}")

    # ─────────────────────────────
    # ░  PDF onboarding
    # ─────────────────────────────
    @commands.command(name="uploadpdf")
    @commands.guild_only()
    @sc_check()
    async def upload_pdf(self, ctx, *, display_name: str):
        """Attach **one** PDF and give it a display name."""
        if not ctx.message.attachments:
            await ctx.send("Attach a PDF with the command.")
            return
        att = ctx.message.attachments[0]
        if not att.filename.lower().endswith(".pdf"):
            await ctx.send("That doesn’t look like a PDF.")
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

    # ---------- onboard ----------
    @commands.command()
    @commands.guild_only()
    @sc_check()
    async def onboard(self, ctx, member: discord.Member):
        """Assign the *first* staff role & DM onboarding PDFs."""
        staff_ids = await self._staff_role_ids(ctx.guild)
        roles_to_add = [discord.utils.get(ctx.guild.roles, id=staff_ids[0])] if staff_ids else []
        staff_role = roles_to_add[0] if roles_to_add else None

        if staff_role:
            await member.add_roles(staff_role, reason="Support onboarding")

        pdfs_cfg = await self.config.guild(ctx.guild).pdfs()
        files = []
        for disp in pdfs_cfg:
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
            await ctx.send(f"✅ Onboarded {member.mention}")

    # ─────────────────────────────
    # ░  Points & awards
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

    # ---------- leaderboard ----------
    @commands.command()
    @sc_check()
    async def leaderboard(self, ctx, top: int = 10):
        members = await self.config.all_members(ctx.guild)
        sorted_m = sorted(members.items(), key=lambda kv: kv[1]["points"], reverse=True)[:max(1, top)]
        lines = [f"{i:>2}. {ctx.guild.get_member(uid).display_name:<25} {d['points']} pts"
                 for i, (uid, d) in enumerate(sorted_m, 1) if ctx.guild.get_member(uid)]
        await ctx.send(box("\n".join(lines)))

    # ─────────────────────────────
    # ░  Weekly check-in workflow
    # ─────────────────────────────
    @commands.command()
    @sc_check()
    async def opencheckins(self, ctx):
        g = self.config.guild(ctx.guild)
        await g.checkins_open.set(True)
        await g.submitted_this_week.set([])
        await g.excused_this_week.set([])
        staff_ids = await self._staff_role_ids(ctx.guild)
        ping = " ".join(f"<@&{rid}>" for rid in staff_ids) if staff_ids else "@here"
        chk_chan = await self._get_chan(ctx.guild, "checkins") or ctx.channel
        await chk_chan.send(f"✅ Check-ins are now **open**.\n{ping}")

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

        cat_id = await gconf.category_id()
        category = discord.utils.get(ctx.guild.categories, id=cat_id) if cat_id else None
        ch = await ctx.guild.create_text_channel(
            f"checkin-{ctx.author.name}".lower().replace(" ", "-"),
            overwrites={
                ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                ctx.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                discord.utils.get(ctx.guild.roles, id=await self._sc_role_id(ctx.guild)):
                    discord.PermissionOverwrite(view_channel=True),
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

        await ch.send(f"{ctx.author.mention}, please answer each question:")
        answers = []
        def chk(m): return m.author == ctx.author and m.channel == ch
        try:
            for q in questions:
                await ch.send(q)
                m = await self.bot.wait_for("message", check=chk, timeout=300)
                answers.append(m.content)
        except asyncio.TimeoutError:
            await ch.send("⏰ Time-out. Run `!checkin` again later.")
            return

        summary = "\n".join(f"**{q[3:]}** {a}" for q, a in zip(questions, answers))
        async with self.config.member(ctx.author).checkins() as arr:
            arr.append({"timestamp": datetime.datetime.utcnow().isoformat(), "message": summary})
        async with gconf.submitted_this_week() as s:
            s.append(str(ctx.author.id))

        # log thread
        log_channel = await self._get_chan(ctx.guild, "checkin_log")
        if log_channel:
            thread = discord.utils.get(log_channel.threads, name=ch.name) \
                     or await log_channel.create_thread(name=ch.name, type=discord.ChannelType.private_thread)
            await thread.send(f"📅 **Weekly check-in from {ctx.author.mention}**\n\n{summary}")

        await self._change_points(ctx.author, 3)
        await ch.send("✅ Check-in recorded (+3 pts). Closing in 10 s.")
        await asyncio.sleep(10)
        await ch.delete()

    @commands.command()
    @sc_check()
    async def accept(self, ctx):
        """Close the current check-in channel immediately."""
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
                await ctx.send(f"{member.display_name} is already excused.")
                return
            ex.append(str(member.id))
        await ctx.send(f"{member.mention} excused from penalties this week.")

    @commands.command()
    @sc_check()
    async def closecheckins(self, ctx):
        gconf = self.config.guild(ctx.guild)
        await gconf.checkins_open.set(False)
        submitted, excused = await asyncio.gather(
            gconf.submitted_this_week(), gconf.excused_this_week()
        )
        missed = []
        staff_ids = await self._staff_role_ids(ctx.guild)
        for m in ctx.guild.members:
            if m.bot or not any(r.id in staff_ids for r in m.roles):
                continue
            if str(m.id) in submitted or str(m.id) in excused:
                continue
            missed.append(m)
            await self._change_points(m, -5)
            try:
                await m.send("You missed this week’s check-in. -5 points.")
            except discord.Forbidden:
                pass
        await ctx.send(f"Check-ins closed. Penalised {len(missed)} members.")

    # ─────────────────────────────
    # ░  Summaries & inactivity
    # ─────────────────────────────
    @commands.command()
    @sc_check()
    async def summary(self, ctx):
        now = datetime.datetime.utcnow()
        week_ago = now - datetime.timedelta(days=7)
        members = await self.config.all_members(ctx.guild)
        point_earnings = defaultdict(int)

        for uid, data in members.items():
            for log in data["points_log"]:
                if datetime.datetime.fromisoformat(log["timestamp"]) > week_ago:
                    point_earnings[uid] += log["amount"]

        top = sorted(point_earnings.items(), key=lambda kv: kv[1], reverse=True)[:3]
        top_lines = [f"{ctx.guild.get_member(int(uid)).mention}: +{pts} pts"
                     for uid, pts in top if ctx.guild.get_member(int(uid))]

        submitted = len(await self.config.guild(ctx.guild).submitted_this_week())
        staff_count = len([m for m in ctx.guild.members
                           if any(r.id in await self._staff_role_ids(ctx.guild) for r in m.roles)])

        embed = discord.Embed(
            title="📊 Weekly Summary",
            description=f"{week_ago.date()} — {now.date()}",
            color=0x00ffcc,
        )
        embed.add_field(name="Check-ins submitted",
                        value=f"{submitted}/{staff_count}", inline=False)
        embed.add_field(name="Top earners",
                        value="\n".join(top_lines) or "None", inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    @sc_check()
    async def inactive(self, ctx):
        threshold = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        inactive = []
        for m in ctx.guild.members:
            if m.bot or not any(r.id in await self._staff_role_ids(ctx.guild) for r in m.roles):
                continue
            ck = await self.config.member(m).checkins()
            last = max((datetime.datetime.fromisoformat(c["timestamp"]) for c in ck),
                       default=None)
            if not last or last < threshold:
                inactive.append((m, last))
        if not inactive:
            await ctx.send("No inactive support members.")
            return
        embed = discord.Embed(title="🛑 Inactive Support Members (7 days+)", color=0xe74c3c)
        embed.description = "\n".join(f"{m.mention} – {last.date() if last else 'Never'}"
                                      for m, last in inactive)
        await ctx.send(embed=embed)

    # ─────────────────────────────
    # ░  Promotions / demotions
    # ─────────────────────────────
    PROMO_THRESHOLDS = {75: 1, 200: 2, 500: 3}  # index into staff_role_ids list

    @commands.command()
    @sc_check()
    async def promote(self, ctx, member: discord.Member):
        roles = [discord.utils.get(ctx.guild.roles, id=r) for r in await self._staff_role_ids(ctx.guild)]
        roles = [r for r in roles if r]
        if not roles or roles[-1] in member.roles:
            await ctx.send("Cannot promote further.")
            return
        current_idx = next((i for i, r in enumerate(roles) if r in member.roles), -1)
        if current_idx == -1:
            await ctx.send("Member has no staff rank.")
            return
        await member.remove_roles(roles[current_idx], reason="Promotion")
        await member.add_roles(roles[current_idx + 1], reason="Promotion")
        log = await self._get_chan(ctx.guild, "promotion_log")
        if log:
            await log.send(f"📈 {member.mention} promoted to `{roles[current_idx + 1].name}` by {ctx.author.mention}")
        await ctx.send(f"✅ Promoted {member.display_name}.")

    @commands.command()
    @sc_check()
    async def demote(self, ctx, member: discord.Member):
        roles = [discord.utils.get(ctx.guild.roles, id=r) for r in await self._staff_role_ids(ctx.guild)]
        roles = [r for r in roles if r]
        current_idx = next((i for i, r in enumerate(roles) if r in member.roles), -1)
        if current_idx <= 0:
            await ctx.send("Cannot demote further.")
            return
        await member.remove_roles(roles[current_idx], reason="Demotion")
        await member.add_roles(roles[current_idx - 1], reason="Demotion")
        log = await self._get_chan(ctx.guild, "promotion_log")
        if log:
            await log.send(f"📉 {member.mention} demoted to `{roles[current_idx - 1].name}` by {ctx.author.mention}")
        await ctx.send(f"⬇️ Demoted {member.display_name}.")

    # ─────────────────────────────
    # ░  Dummy listener (placeholder)
    # ─────────────────────────────
    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        pass
