from __future__ import annotations

import discord
from redbot.core import commands, Config, checks  
from typing import Optional

ACCENT = 0xE74C3C


class WelcomeEmbed(commands.Cog):
    """Greets new members with a fancy embed you can edit from Discord."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # guild-scoped settings
        self.config = Config.get_conf(self, identifier=56789012, force_registration=True)
        self.config.register_guild(
            channel_id=None,          # int | None
            title="Halt!",
            body="By order of His Imperial Majesty, {member}, …",
            thumbnail_url=None,       # str | None
            image_url=None,           # str | None
            enabled=False,
        )

    # ╭─────────────────────────── COMMANDS ───────────────────────────╮
    @redcommands.group(name="welcome", aliases=["wel"], invoke_without_command=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def welcome(self, ctx: commands.Context):
        """Configure the join-welcome embed."""
        await ctx.send_help()

    # set channel
    @welcome.command(name="channel")
    async def welcome_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel where the embed should be posted."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"✅ Welcome channel set to {channel.mention}")

    # title
    @welcome.command(name="title")
    async def welcome_title(self, ctx: commands.Context, *, text: str):
        """Set the embed **title**."""
        await self.config.guild(ctx.guild).title.set(text)
        await ctx.tick()

    # body (supports {member})
    @welcome.command(name="body")
    async def welcome_body(self, ctx: commands.Context, *, text: str):
        """Set the **body**. Use `{member}` where the joiner should be mentioned."""
        await self.config.guild(ctx.guild).body.set(text)
        await ctx.tick()

    # thumbnail – url **or** attachment
    @welcome.command(name="thumb", aliases=["thumbnail"])
    async def welcome_thumb(self, ctx: commands.Context, url_or_none: Optional[str] = None):
        """
        Set / clear the **thumbnail** (small square).  
        • Provide a URL, **or**  
        • Attach an image & run the command with no URL, **or**  
        • Type `clear` to remove it.
        """
        if url_or_none and url_or_none.lower() == "clear":
            await self.config.guild(ctx.guild).thumbnail_url.set(None)
            return await ctx.send("🗑️ Thumbnail cleared.")

        if ctx.message.attachments and not url_or_none:
            url_or_none = ctx.message.attachments[0].url

        if not url_or_none:
            return await ctx.send("❌ Give me a URL or attach an image.")

        await self.config.guild(ctx.guild).thumbnail_url.set(url_or_none)
        await ctx.tick()

    # main image – same logic
    @welcome.command(name="image", aliases=["picture", "img"])
    async def welcome_image(self, ctx: commands.Context, url_or_none: Optional[str] = None):
        """
        Set / clear the **main image**.  
        Same rules as `thumb`.
        """
        if url_or_none and url_or_none.lower() == "clear":
            await self.config.guild(ctx.guild).image_url.set(None)
            return await ctx.send("🗑️ Image cleared.")

        if ctx.message.attachments and not url_or_none:
            url_or_none = ctx.message.attachments[0].url

        if not url_or_none:
            return await ctx.send("❌ Give me a URL or attach an image.")

        await self.config.guild(ctx.guild).image_url.set(url_or_none)
        await ctx.tick()

    # enable / disable
    @welcome.command(name="toggle")
    async def welcome_toggle(self, ctx: commands.Context):
        """Enable/disable the welcome embed without losing your settings."""
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)
        await ctx.send(f"{'✅ Enabled' if not current else '🛑 Disabled'}.")

    # preview
    @welcome.command(name="preview")
    async def welcome_preview(self, ctx: commands.Context, target: Optional[discord.Member]):
        """Send a preview in the current channel."""
        emb = await self._build_embed(ctx.guild, target or ctx.author)
        await ctx.send(embed=emb)

    # ╭───────────────────────── LISTENER ─────────────────────────────╮
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        gconf = self.config.guild(member.guild)
        if not await gconf.enabled():
            return
        chan_id = await gconf.channel_id()
        channel = member.guild.get_channel(chan_id)
        if not channel:
            return

        embed = await self._build_embed(member.guild, member)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass  # missing perms – silently ignore

    # ╭───────────────── INTERNAL EMBED BUILDER ──────────────────────╮
    async def _build_embed(self, guild: discord.Guild, member: discord.Member) -> discord.Embed:
        gconf = self.config.guild(guild)
        title = await gconf.title()
        body = (await gconf.body()).replace("{member}", member.mention)

        emb = discord.Embed(title=title, description=body, colour=ACCENT)

        if url := await gconf.thumbnail_url():
            emb.set_thumbnail(url=url)
        if url := await gconf.image_url():
            emb.set_image(url=url)

        return emb
