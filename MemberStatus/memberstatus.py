import discord
import csv
import io
from redbot.core import commands
from datetime import datetime

class MemberStatus(commands.Cog):
    """Counts members by role and status and exports member info to CSV."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.guild_only()
    async def rolestatus(self, ctx, role: discord.Role):
        """Check how many members have the given role and export their info to CSV."""
        members = role.members
        total = len(members)

        # Status counters
        status_counts = {
            "online": 0,
            "idle": 0,
            "dnd": 0,
            "offline": 0
        }

        for member in members:
            status = str(member.status)
            if status in status_counts:
                status_counts[status] += 1
            else:
                status_counts["offline"] += 1  # Just in case

        # CSV creation
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Member ID", "Username", "Display Name", "Status", 
            "Account Created", "Joined Server", "Roles"
        ])

        for member in members:
            roles = [r.name for r in member.roles if r != ctx.guild.default_role]
            writer.writerow([
                member.id,
                f"{member.name}#{member.discriminator}",
                member.display_name,
                str(member.status),
                member.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "N/A",
                ", ".join(roles)
            ])

        output.seek(0)
        csv_file = discord.File(fp=io.BytesIO(output.getvalue().encode()), filename=f"{role.name}_members.csv")

        # Embed summary
        embed = discord.Embed(
            title=f"Role Status Summary: {role.name}",
            color=role.color
        )
        embed.add_field(name="Total Members", value=total)
        embed.add_field(name="Online", value=status_counts["online"])
        embed.add_field(name="Idle", value=status_counts["idle"])
        embed.add_field(name="Do Not Disturb", value=status_counts["dnd"])
        embed.add_field(name="Offline/Invisible", value=status_counts["offline"])
        embed.set_footer(text=f"Exported by {ctx.author}", icon_url=ctx.author.display_avatar.url)

        await ctx.send(embed=embed, file=csv_file)
