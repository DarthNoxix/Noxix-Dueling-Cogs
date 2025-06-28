from __future__ import annotations

import io
import re
from typing import Optional, Tuple, List

import discord
from redbot.core import commands, checks

ATTACHMENT_LIMIT = 8 * 1024 * 1024          # 8 MiB safety cap


class EmbedCopier(commands.Cog):
    """Copy any message’s embeds & attachments to another channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ──────────────────────────────────────────────────────────
    # Helper – resolve a "<channel_id>/<message_id>" or link
    # ──────────────────────────────────────────────────────────
    async def _resolve_message(
        self, ctx: commands.Context, ref: str, fallback_channel: discord.TextChannel
    ) -> Optional[discord.Message]:
        """
        Accepts:
          • a full https://discord.com/... message link
          • "channel_id-message_id"
          • plain message_id (assumes current channel)
        """
        link_regex = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
        m = link_regex.search(ref)
        if m:
            _, chan_id, msg_id = map(int, m.groups())
        else:
            if "-" in ref:
                chan_id, msg_id = map(int, ref.split("-", 1))
            else:
                chan_id, msg_id = fallback_channel.id, int(ref)

        channel = self.bot.get_channel(chan_id) or await self.bot.fetch_channel(chan_id)
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            return None
        try:
            return await channel.fetch_message(msg_id)
        except discord.NotFound:
            return None

    # ──────────────────────────────────────────────────────────
    #  Command
    # ──────────────────────────────────────────────────────────
    @commands.command(name="copyembed", aliases=["cloneembed", "embedcopy"])
    @checks.admin_or_permissions(manage_messages=True)
    async def copy_embed(
        self,
        ctx: commands.Context,
        message_ref: str,
        destination: Optional[discord.TextChannel] = None,
        *,
        flags: str = ""
    ):
        """
        Copy every embed (and attachments) from another message.

        `[p]copyembed <source-link|id>  [#destination]  [--no-content]`
        """
        dest = destination or ctx.channel
        if not dest.permissions_for(ctx.me).send_messages:
            return await ctx.send("❌ I can’t send messages in that channel.")

        source_msg = await self._resolve_message(ctx, message_ref, ctx.channel)
        if not source_msg:
            return await ctx.send("❌ Couldn’t find that message.")

        # flags
        keep_content = "--no-content" not in flags

        # rebuild embeds list (immutables → deep copy is cheap)
        embeds: List[discord.Embed] = [embed for embed in source_msg.embeds]

        # attachments up to 8 MiB
        files: List[discord.File] = []
        for a in source_msg.attachments:
            if a.size <= ATTACHMENT_LIMIT:
                buf = io.BytesIO(await a.read())
                files.append(discord.File(buf, filename=a.filename))

        await dest.send(
            content=source_msg.content if keep_content else None,
            embeds=embeds or None,
            files=files or None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await ctx.tick()
