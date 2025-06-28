from __future__ import annotations

import discord
from redbot.core import commands, Config, checks
from typing import Optional

ACCENT = 0xE74C3C            # default embed colour


class WelcomeEmbed(commands.Cog):
    """
    Sends a fancy embed when someone joins **and**
    lets admins tweak the message / images with simple commands.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=99887766, force_registration=True
        )
        # guild-level defaults
        self.config.register_guild(
            channel_id=None,          # where to post
            title="Halt!",
            body=(
                "By order of His Imperial Majesty, Emperor Noxix I Targaryen, "
                "{mention}, you are ordered to read #📜│MUST-READ-FAQ│ and "
                "#📕│MUST-READ-RULES│…"
            ),
            thumb_url=None,           # small square
            banner_url=None           # large banner
        )

        bot.add_listener(self._on_member_join, "on_member_join")

    # ────────────────────────────────────────────────────────
    #                      COMMANDS
    # ────────────────────────────────────────────────────────
    @commands.group(
        name="welcome",
        aliases=["wel"],
        invoke_without_command=True
    )
    @checks.admin_or_permissions(manage_guild=True)
    async def welcome(self, ctx: commands.Context):
        """Configure the welcome embed."""
        await ctx.send_help()

    # set channel
    @welcome.command(name="channel")
    async def welcome_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ):
        """Choose the channel the welcome embed is posted to."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.tick()

    # set images
    @welcome.command(name="images")
    async def welcome_images(
        self,
        ctx: commands.Context,
        thumb: Optional[str] = None,
        banner: Optional[str] = None,
    ):
        """
        Set thumbnail and banner image URLs.
        • Pass `null` to clear an image.
        """
        if thumb and thumb.lower() == "null":
            thumb = None
        if banner and banner.lower() == "null":
            banner = None

        await self.config.guild(ctx.guild).thumb_url.set(thumb)
        await self.config.guild(ctx.guild).banner_url.set(banner)
        await ctx.tick()

    # set text
    @welcome.command(name="text")
    async def welcome_text(
        self,
        ctx: commands.Context,
        title: str,
        *,
        body: str
    ):
        """
        Update **title** and **body**.  
        Use **`{mention}`** in the body – it’ll be replaced with the new member’s ping.
        """
        await self.config.guild(ctx.guild).title.set(title)
        await self.config.guild(ctx.guild).body.set(body)
        await ctx.tick()

    # preview
    @welcome.command(name="preview")
    async def welcome_preview(self, ctx: commands.Context):
        """Send the current embed preview (to this channel)."""
        embed = await self._build_embed(ctx.guild, ctx.author)
        await ctx.send(embed=embed)

    # ────────────────────────────────────────────────────────
    #                  LISTENER / SENDER
    # ────────────────────────────────────────────────────────
    async def _on_member_join(self, member: discord.Member):
        cfg = self.config.guild(member.guild)
        channel_id = await cfg.channel_id()

        if not channel_id:
            return                                     # not configured

        channel = member.guild.get_channel(channel_id)
        if not channel:
            return

        embed = await self._build_embed(member.guild, member)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            # missing perms – silently ignore
            pass

    # ────────────────────────────────────────────────────────
    #                     HELPERS
    # ────────────────────────────────────────────────────────
    async def _build_embed(
        self,
        guild: discord.Guild,
        member: discord.abc.User,
    ) -> discord.Embed:
        cfg = self.config.guild(guild)
        title  = await cfg.title()
        body   = (await cfg.body()).replace("{mention}", member.mention)
        thumb  = await cfg.thumb_url()
        banner = await cfg.banner_url()

        embed = discord.Embed(
            title=title,
            description=body,
            colour=ACCENT
        )
        if thumb:
            embed.set_thumbnail(url=thumb)
        if banner:
            embed.set_image(url=banner)
        return embed
