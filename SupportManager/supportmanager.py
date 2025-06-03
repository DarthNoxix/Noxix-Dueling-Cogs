"""
Full-fat ADOD SupportManager cog
• Per-guild PDF onboarding
• Weekly check-ins, points & promotions
• Owner-configurable channels, category & ALL role names/IDs
• Activity graphs, goal-tracking, delta reports, inactivity tools, support-channel registry
"""

# ─────────────────────────────
#  Imports
# ─────────────────────────────
import asyncio
import datetime
import io
from collections import defaultdict, Counter
from pathlib import Path
from typing import List, Optional

import discord
import matplotlib.pyplot as plt
from redbot.core import checks, commands, Config, data_manager
from redbot.core.utils.chat_formatting import box
from typing import Tuple
from redbot.core import commands  # already there


__all__ = ("SupportManager",)

PENGUIN_DISPLAY = "King Penguin"          # “Penguin perms for everything”

# ─────────────────────────────
#  Award-reason table
# ─────────────────────────────
AWARD_REASONS = {
    # positives
    "help":        (5,  "for effectively helping a member"),
    "followup":    (2,  "for answering multiple follow-up questions in a thread"),
    "link":        (3,  "for providing a helpful link or resource"),
    "checkin":     (3,  "for completing a weekly check-in"),
    "bugreport":   (5,  "for reporting a bug from user feedback"),
    "above":       (15, "for going above and beyond"),
    "reminder":    (2,  "for reminding others to be respectful or helpful"),
    "correction":  (2,  "for spotting and correcting incorrect info"),
    "escalation":  (3,  "for escalating an issue to team leads appropriately"),
    # penalties
    "missedcheckin": (-5,  "for missing weekly check-ins without notice"),
    "ignoredping":   (-3,  "for ignoring direct support pings without reason"),
    "wronginfo":     (-1,  "for giving incorrect information (unintentional)"),
    "rude":         (-15,  "for being rude / unprofessional"),
}

SUPPORT_ROLE_NAMES = {"Cupbearer", "Unlanded Knight", "Goldcloak", "Kingsguard", "Imperial Guard"}

# ─────────────────────────────
#  Helper decorators
# ─────────────────────────────
def _is_penguin(ctx):
    return ctx.author.display_name == PENGUIN_DISPLAY

def sc_check():
    async def predicate(ctx):
        if _is_penguin(ctx):
            return True
        sc_id = await ctx.cog.config.guild(ctx.guild).sc_role_id()
        return any(r.id == sc_id for r in ctx.author.roles) if sc_id else any(
            r.name == "Small Council" for r in ctx.author.roles
        )

    return commands.check(predicate)


def staff_check():
    async def predicate(ctx):
        if _is_penguin(ctx):
            return True
        ids = await ctx.cog._staff_role_ids(ctx.guild)
        return any(r.id in ids for r in ctx.author.roles)

    return commands.check(predicate)


#  Alias for the new text commands you provided
def is_small_council():
    return sc_check()

# ─────────────────────────────
#  Activity-graph helpers
# ─────────────────────────────
async def _member_daily_points(cog: "SupportManager",
                               member: discord.Member,
                               days: int = 7) -> List[int]:
    """Return list[days] of points earned each UTC day (today-N … today)."""
    today = datetime.datetime.utcnow().date()
    log = await cog.config.member(member).points_log()
    per_day = Counter()
    for entry in log:
        ts = datetime.datetime.fromisoformat(entry["timestamp"]).date()
        if (today - ts).days < days:
            per_day[ts] += entry["amount"]
    return [per_day[today - datetime.timedelta(days=i)] for i in reversed(range(days))]

async def _member_daily_messages(guild: discord.Guild,
                                 support_channels: List[int],
                                 member: discord.Member,
                                 days: int = 7) -> Tuple[List[int], List[int]]:
    """Return (support_msgs, other_msgs) lists over last N days."""
    today = datetime.datetime.utcnow()
    since = today - datetime.timedelta(days=days)
    support_counts = Counter()
    other_counts = Counter()

    async def scan_channel(ch: discord.TextChannel, support: bool):
        try:
            async for msg in ch.history(limit=None, after=since):
                if msg.author.id == member.id:
                    key_date = msg.created_at.date()
                    if support:
                        support_counts[key_date] += 1
                    else:
                        other_counts[key_date] += 1
        except (discord.Forbidden, discord.HTTPException):
            pass

    # iterate channels
    for ch in guild.text_channels:
        await scan_channel(ch, ch.id in support_channels)

    days_range = [today.date() - datetime.timedelta(days=i) for i in reversed(range(days))]
    sup = [support_counts[d] for d in days_range]
    oth = [other_counts[d] for d in days_range]
    return sup, oth

async def generate_activity_graph(ctx: commands.Context, member: discord.Member) -> discord.File:
    """Create a matplotlib PNG graph in-memory for the given member."""
    conf = ctx.cog
    support_channels = await conf.config.guild(ctx.guild).support_channels()
    support_channels = support_channels or []

    days = 7
    dates = [ (datetime.datetime.utcnow().date() - datetime.timedelta(days=i)).strftime("%d %b")
              for i in reversed(range(days))]

    pts = await _member_daily_points(conf, member, days)
    sup_msgs, oth_msgs = await _member_daily_messages(ctx.guild, support_channels, member, days)

    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot(dates, pts, marker="o", label="Points earned")
    ax.bar(dates, sup_msgs, alpha=0.5, label="Msgs in support-channels")
    ax.bar(dates, oth_msgs, bottom=sup_msgs, alpha=0.3, label="Msgs elsewhere")
    ax.set_title(f"Activity (last 7 days) — {member.display_name}")
    ax.set_ylabel("Count")
    ax.legend(loc="upper left")
    fig.tight_layout()

    bio = io.BytesIO()
    fig.savefig(bio, format="png")
    bio.seek(0)
    plt.close(fig)
    return discord.File(bio, filename="activity.png")

# ─────────────────────────────
#  UI: activity-graph selector
# ─────────────────────────────
class ActivityGraphView(discord.ui.View):
    """A 120-s view with one <select> that swaps the graph."""
    def __init__(self, ctx: commands.Context, members: List[discord.Member]):
        super().__init__(timeout=120)
        self.add_item(ActivitySelect(ctx, members))     # add the dropdown


class ActivitySelect(discord.ui.Select):
    def __init__(self, ctx: commands.Context, members: List[discord.Member]):
        self.ctx = ctx
        opts = [discord.SelectOption(label=m.display_name, value=str(m.id)) for m in members]
        super().__init__(placeholder="Select member…", min_values=1, max_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        # 0. claim the interaction token immediately
        await interaction.response.defer()

        uid = int(self.values[0])
        member = self.ctx.guild.get_member(uid)
        if not member:
            await interaction.edit_original_response(
                content="Member left the server 🤷",
                view=self.view
            )
            return

        # 1. heavy work – render the graph
        file = await generate_activity_graph(self.ctx, member)

        # 2. edit the original message, keeping the same dropdown active
        await interaction.edit_original_response(
            content=f"📊 Activity graph for {member.mention} (past 7 days)",
            attachments=[file],
            view=self.view,          # self.view is already set by Discord
        )


# ─────────────────────────────
#  Cog
# ─────────────────────────────
class SupportManager(commands.Cog):
    """ADOD Support-team manager (check-ins, points, PDFs, ranks, goals, graphs)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xAD0D515, force_registration=True)

        # per-guild defaults
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
            support_channels=[],      # ⭠ NEW
            role_dates={},            # ⭠ NEW
            goals={},                 # ⭠ NEW
        )

        # per-member defaults
        self.config.register_member(points=0, points_log=[], checkins=[])

    # =============== PATH HELPERS ===============
    @property
    def _data_path(self) -> Path:
        return data_manager.cog_data_path(self)

    async def _pdf_path(self, guild: discord.Guild, display: str) -> Path:
        gfolder = self._data_path / str(guild.id)
        gfolder.mkdir(parents=True, exist_ok=True)
        return gfolder / f"{display}.pdf"

    # =============== POINT HELPERS ===============
    async def _change_points(self, member: discord.Member, delta: int):
        cur = await self.config.member(member).points()
        await self.config.member(member).points.set(cur + delta)

        async with self.config.member(member).points_log() as log:
            log.append(
                {"amount": delta, "timestamp": datetime.datetime.utcnow().isoformat()}
            )

    async def _points(self, member: discord.Member) -> int:
        return await self.config.member(member).points()

    # =============== CHANNEL / ROLE HELPERS ===============
    async def _get_chan(self, guild: discord.Guild, key: str) -> Optional[discord.TextChannel]:
        cid = (await self.config.guild(guild).channels()).get(key)
        return guild.get_channel(cid) if cid else None

    async def _staff_role_ids(self, guild: discord.Guild):
        ids = await self.config.guild(guild).staff_role_ids()
        if ids:
            return ids
        return [r.id for r in guild.roles if r.name in SUPPORT_ROLE_NAMES]

    async def _sc_role_id(self, guild: discord.Guild):
        rid = await self.config.guild(guild).sc_role_id()
        if rid:
            return rid
        role = discord.utils.get(guild.roles, name="Small Council")
        return role.id if role else None

    # ─────────────────────────────
    #  Small-Council utility commands (new)
    # ─────────────────────────────
    @commands.hybrid_command()
    @is_small_council()
    async def activitygraphsetup(self, ctx: commands.Context):
        """Create an interactive 7-day activity graph for Support staff."""
        role_names = SUPPORT_ROLE_NAMES
        members = [m for m in ctx.guild.members if any(r.name in role_names for r in m.roles)]

        if not members:
            await ctx.send("⚠ No eligible members found with the specified roles.")
            return

        default_member = members[0]
        file = await generate_activity_graph(ctx, default_member)
        view = ActivityGraphView(ctx, members)
        await ctx.send(
            content=f"📊 Activity graph for {default_member.mention} (past 7 days)",
            file=file,
            view=view
        )

    # ---- Support-channel registry --------------------------
    @commands.hybrid_command()
    @is_small_council()
    async def addsupportchannel(self, ctx, channel: discord.TextChannel):
        """Add a text channel to the support-channel set."""
        async with self.config.guild(ctx.guild).support_channels() as sc:
            if channel.id in sc:
                await ctx.send(f"⚠️ {channel.mention} is already a support channel.")
                return
            sc.append(channel.id)
        await ctx.send(f"✅ Added {channel.mention} as a support channel.")

    @commands.hybrid_command()
    @is_small_council()
    async def removesupportchannel(self, ctx, channel: discord.TextChannel):
        """Remove a channel from the support-channel set."""
        async with self.config.guild(ctx.guild).support_channels() as sc:
            if channel.id not in sc:
                await ctx.send(f"⚠️ {channel.mention} is not listed as a support channel.")
                return
            sc.remove(channel.id)
        await ctx.send(f"🗑️ Removed {channel.mention} from support channels.")

    @commands.hybrid_command()
    @is_small_council()
    async def listsupportchannels(self, ctx):
        ids = await self.config.guild(ctx.guild).support_channels()
        if not ids:
            await ctx.send("📭 No support channels are currently set.")
            return
        channels = [ctx.guild.get_channel(cid) for cid in ids]
        lines = [f"• {ch.mention}" for ch in channels if ch]
        await ctx.send("📋 **Current Support Channels:**\n" + "\n".join(lines))

    # ---- Cup-team overview --------------------------------
    @commands.hybrid_command()
    @is_small_council()
    async def cupteam(self, ctx):
        """List support members sorted by time in current rank."""
        now = datetime.datetime.utcnow()
        support_roles = SUPPORT_ROLE_NAMES
        role_dates = await self.config.guild(ctx.guild).role_dates()
        member_info = []

        for member in ctx.guild.members:
            current_role = next((r.name for r in member.roles if r.name in support_roles), None)
            if not current_role:
                continue

            uid = str(member.id)
            rd = role_dates.get(uid)
            if rd and rd.get("role") == current_role:
                ts = datetime.datetime.fromisoformat(rd["timestamp"])
            else:
                # start tracking from now
                ts = now
                role_dates[uid] = {"role": current_role, "timestamp": now.isoformat()}

            delta = now - ts
            days = delta.days
            weeks, rem = divmod(days, 7)
            member_info.append((member.display_name, current_role, days, f"{weeks}w {rem}d"))

        # persist any new timestamps
        await self.config.guild(ctx.guild).role_dates.set(role_dates)

        if not member_info:
            await ctx.send("No support staff currently assigned roles.")
            return

        member_info.sort(key=lambda x: x[2], reverse=True)
        lines = [f"• {name} — **{role}** — {dur}" for name, role, _, dur in member_info]

        embed = discord.Embed(
            title="🛡️ Cupbearer Team Overview",
            description="\n".join(lines),
            color=0x3498db
        )
        await ctx.send(embed=embed)

    # ---- Inactivity report -------------------------------
    @commands.hybrid_command()
    @is_small_council()
    async def noactivity(self, ctx):
        """List support staff who haven’t spoken in any support channel in 7 days."""
        now = datetime.datetime.utcnow()
        cutoff = now - datetime.timedelta(days=7)
        sc_ids = await self.config.guild(ctx.guild).support_channels()
        if not sc_ids:
            await ctx.send("🔧 No support channels configured.")
            return

        last_seen = {}

        for channel_id in sc_ids:
            channel = ctx.guild.get_channel(channel_id)
            if not channel:
                continue
            try:
                async for msg in channel.history(limit=None, after=cutoff):
                    if any(r.name in SUPPORT_ROLE_NAMES for r in msg.author.roles):
                        uid = str(msg.author.id)
                        last_seen[uid] = max(last_seen.get(uid, cutoff), msg.created_at)
            except Exception:
                continue

        inactive = [
            m.display_name for m in ctx.guild.members
            if any(r.name in SUPPORT_ROLE_NAMES for r in m.roles)
            and str(m.id) not in last_seen
        ]

        if not inactive:
            await ctx.send("✅ All support staff have been active in the last 7 days.")
        else:
            await ctx.send(
                "❌ The following support members have been inactive for 7+ days:\n"
                + "\n".join(f"• {n}" for n in inactive)
            )

        # ---- Weekly delta report -----------------------------
    @commands.hybrid_command()
    @is_small_council()
    async def delta(self, ctx):
        """Compare check-ins & points this week vs last."""
        today = datetime.datetime.utcnow().date()
        this_week_start = today - datetime.timedelta(days=today.weekday())          # Monday
        last_week_start = this_week_start - datetime.timedelta(weeks=1)
        last_week_end   = this_week_start - datetime.timedelta(seconds=1)

        # --- Tally check-ins ---
        this_check = 0
        last_check = 0
        for member in ctx.guild.members:
            for entry in await self.config.member(member).checkins():
                d = datetime.datetime.fromisoformat(entry["timestamp"]).date()
                if d >= this_week_start:
                    this_check += 1
                elif last_week_start <= d <= last_week_end:
                    last_check += 1

        # --- Tally points ---
        this_pts = 0
        last_pts = 0
        for member in ctx.guild.members:
            for log in await self.config.member(member).points_log():
                d = datetime.datetime.fromisoformat(log["timestamp"]).date()
                if d >= this_week_start:
                    this_pts += log["amount"]
                elif last_week_start <= d <= last_week_end:
                    last_pts += log["amount"]

        # --- Embed ---
        def arrow(n): return "🔺" if n > 0 else ("🔻" if n < 0 else "⏸️")
        embed = discord.Embed(title="📊 Weekly Delta Report", color=0x00bfff)
        embed.add_field(name="Check-ins",
            value=f"{this_check} ({arrow(this_check-last_check)} {this_check-last_check:+})", inline=True)
        embed.add_field(name="Points Earned",
            value=f"{this_pts} ({arrow(this_pts-last_pts)} {this_pts-last_pts:+})", inline=True)

        await ctx.send(embed=embed)


    # ---- Goal system -------------------------------------
    @commands.hybrid_command()
    @is_small_council()
    async def assigngoal(self, ctx, member: discord.Member, *, goal: str):
        """Assign a weekly goal to a staff member."""
        gid = str(member.id)
        async with self.config.guild(ctx.guild).goals() as goals:
            goals[gid] = {
                "goal": goal,
                "assigned_by": ctx.author.display_name,
                "timestamp": datetime.datetime.utcnow().isoformat()
            }
        await ctx.send(f"🎯 Goal assigned to {member.display_name}: \"{goal}\"")
        try:
            await member.send(f"🎯 You’ve been assigned a new goal: \"{goal}\"")
        except discord.Forbidden:
            await ctx.send("⚠️ Couldn’t DM the user.")

    @commands.hybrid_command()
    @is_small_council()
    async def goalsummary(self, ctx):
        goals = await self.config.guild(ctx.guild).goals()
        if not goals:
            await ctx.send("📭 No goals are currently assigned.")
            return

        lines = []
        for uid, info in goals.items():
            member = ctx.guild.get_member(int(uid))
            if not member:
                continue
            ts = datetime.datetime.fromisoformat(info["timestamp"]).strftime("%Y-%m-%d")
            lines.append(
                f"• {member.mention} — _Assigned by **{info['assigned_by']}** on **{ts}**_\n"
                f"  ➤ \"{info['goal']}\""
            )

        embed = discord.Embed(
            title="🎯 Current Support Staff Goals",
            description="\n\n".join(lines),
            color=0x2ecc71
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command()
    @is_small_council()
    async def removegoal(self, ctx, member: discord.Member):
        gid = str(member.id)
        async with self.config.guild(ctx.guild).goals() as goals:
            if gid not in goals:
                await ctx.send(f"⚠️ {member.display_name} has no assigned goal to remove.")
                return
            del goals[gid]
        await ctx.send(f"🗑️ Removed goal for {member.display_name}.")

    @commands.hybrid_command()
    @is_small_council()
    async def completegoal(self, ctx, member: discord.Member):
        gid = str(member.id)
        async with self.config.guild(ctx.guild).goals() as goals:
            if gid not in goals:
                await ctx.send(f"⚠️ {member.display_name} has no assigned goal to complete.")
                return
            goal_text = goals[gid]["goal"]
            del goals[gid]
        await ctx.send(
            f"✅ Goal completed for {member.mention}!\n"
            f"🎯 \"{goal_text}\" is now marked as **complete**."
        )

    # ---- Ping team for missing check-ins ------------------
    @commands.hybrid_command()
    @is_small_council()
    async def pingteam(self, ctx):
        """Ping staff who haven’t checked in this week."""
        week_start = datetime.datetime.utcnow() - datetime.timedelta(days=datetime.datetime.utcnow().weekday())
        unsubmitted = []

        for member in ctx.guild.members:
            if not any(r.name in SUPPORT_ROLE_NAMES for r in member.roles):
                continue
            # has check-in this week?
            recent = False
            for check in (await self.config.member(member).checkins()):
                ts = datetime.datetime.fromisoformat(check["timestamp"])
                if ts.date() >= week_start.date():
                    recent = True
                    break
            if not recent:
                unsubmitted.append(member.mention)

        if not unsubmitted:
            await ctx.send("✅ All support staff have submitted their check-in this week.")
        else:
            await ctx.send(f"⏰ These members still need to check in:\n{', '.join(unsubmitted)}")

    # ─────────────────────────────
    #  Existing owner-level setup (supportset …)  ← unchanged
    # ─────────────────────────────
    # ... (UNCHANGED CODE BETWEEN THIS AND AWARD SECTION OMITTED FOR BREVITY) ...

    # ─────────────────────────────
    #  Points & awards  (updated for multi-reason)
    # ─────────────────────────────
    @commands.hybrid_command()
    async def awardreasons(self, ctx):
        await ctx.send(box("\n".join(f"- {r}: {p:+}" for r, (p, _) in AWARD_REASONS.items())))

    @commands.hybrid_command()
    @sc_check()
    async def award(self, ctx, member: discord.Member, *reasons: str):
        """!award @user reason [reason …]  – award/deduct for multiple reasons."""
        if not reasons:
            await ctx.send("Specify at least one reason (see !awardreasons).")
            return
        total_delta = 0
        invalid = []
        details = []
        for r in reasons:
            if r not in AWARD_REASONS:
                invalid.append(r)
                continue
            delta, desc = AWARD_REASONS[r]
            total_delta += delta
            details.append(f"{delta:+} ({r})")
        if invalid:
            await ctx.send(f"Invalid reasons: {', '.join(invalid)}")
            return
        await self._change_points(member, total_delta)
        await ctx.send(
            f"🏅 **{member.display_name}** {('gains' if total_delta>0 else 'loses')} "
            f"**{abs(total_delta)}** pts → total **{await self._points(member)}** "
            f"({' ,'.join(details)})"
        )

    # ─────────────────────────────
    #  Weekly check-in workflow (Q5 text tweak)
    # ─────────────────────────────
    #  -- (existing opencheckins, checkin etc. kept exactly as before,
    #      but question 5 changed) --

    # Replace originals for brevity only: the check-in command below
    # is identical to your current one except Q5 wording updated
    @commands.hybrid_command()
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
            "5. How are you feeling about your workload? (1 All Good – 5 Overwhelmed)",
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

        summary = "\n".join(f"**{q[3:]}** {a}" for q, a in zip(q_text, answers))
        async with self.config.member(ctx.author).checkins() as arr:
            arr.append({"timestamp": datetime.datetime.utcnow().isoformat(), "message": summary})
        async with gconf.submitted_this_week() as s:
            s.append(str(ctx.author.id))

        log_ch = await self._get_chan(ctx.guild, "checkin_log")
        thread = None
        if log_ch:
            thread = discord.utils.get(log_ch.threads, name=ch.name) \
                     or await log_ch.create_thread(name=ch.name, type=discord.ChannelType.private_thread)
            await thread.send(f"📅 **Weekly check-in from {ctx.author.mention}**\n\n{summary}")

        if answers[-1].lower().strip() in yes_words and thread:
            sc_role = discord.utils.get(ctx.guild.roles, id=await self._sc_role_id(ctx.guild))
            await thread.send(f"{sc_role.mention if sc_role else '@here'} – {ctx.author.mention} "
                              f"would like a private chat with a team lead.")

        await self._change_points(ctx.author, 3)
        await ch.send("✅ Check-in recorded (+3 pts). Closing in 10 s.")
        await asyncio.sleep(10)
        await ch.delete()

    # ─────────────────────────────
    # ░ Owner-level setup (supportset …)
    # ─────────────────────────────
    @commands.hybrid_group(name="supportset", invoke_without_command=True)
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
    @commands.hybrid_command(name="uploadpdf")
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

    @commands.hybrid_command(name="listpdfs")
    async def list_pdfs(self, ctx):
        pdfs = await self.config.guild(ctx.guild).pdfs()
        await ctx.send("No PDFs uploaded." if not pdfs else box("\n".join(f"- {n}" for n in pdfs)))

    @commands.hybrid_command()
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
    @commands.hybrid_command()
    async def points(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        await ctx.send(f"{member.mention} has **{await self._points(member)}** points.")

    @commands.hybrid_command()
    @sc_check()
    async def addpoints(self, ctx, member: discord.Member, pts: int):
        await self._change_points(member, pts)
        await ctx.send(f"Added {pts} points → {await self._points(member)} total.")

    @commands.hybrid_command()
    @sc_check()
    async def removepoints(self, ctx, member: discord.Member, pts: int):
        await self._change_points(member, -pts)
        await ctx.send(f"Removed {pts} points → {await self._points(member)} total.")

    @commands.hybrid_command(with_app_command=False)
    @sc_check()
    async def award(self, ctx, member: discord.Member, reason: str.lower):
        if reason not in AWARD_REASONS:
            await ctx.send(f"Invalid reason. Use one of: {', '.join(AWARD_REASONS)}")
            return
        delta, desc = AWARD_REASONS[reason]
        await self._change_points(member, delta)
        verb = "awarded" if delta > 0 else "deducted"
        await ctx.send(f"{verb.title()} **{abs(delta)}** points {desc} → total {await self._points(member)}.")

    @commands.hybrid_command()
    async def awardreasons(self, ctx):
        await ctx.send(box("\n".join(f"- {r}: {p:+}" for r, (p, _) in AWARD_REASONS.items())))

    @commands.hybrid_command()
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
    @commands.hybrid_command()
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

    @commands.hybrid_command()
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

    @commands.hybrid_command()
    @sc_check()
    async def accept(self, ctx):
        if not ctx.channel.name.startswith("checkin-"):
            await ctx.send("Run this in a check-in channel.")
            return
        await ctx.send("✅ Closing channel…")
        await asyncio.sleep(1)
        await ctx.channel.delete()

    @commands.hybrid_command()
    @sc_check()
    async def excuse(self, ctx, member: discord.Member):
        async with self.config.guild(ctx.guild).excused_this_week() as ex:
            if str(member.id) in ex:
                await ctx.send("They are already excused.")
                return
            ex.append(str(member.id))
        await ctx.send(f"{member.mention} excused for this week.")

    @commands.hybrid_command()
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
    @commands.hybrid_command()
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


    @commands.hybrid_command()
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
    @commands.hybrid_command()
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

    @commands.hybrid_command()
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

