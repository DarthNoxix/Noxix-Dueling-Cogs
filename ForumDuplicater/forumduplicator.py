# forumduplicator.py
# MIT License
"""
ForumDuplicator – duplicate a forum channel (threads + messages).

[p]duplicateforum <source_forum_channel> [new_name]
"""

import asyncio
import io
from datetime import timezone
from typing import List, Optional

import discord
from redbot.core import checks, commands


MAX_LEN = 2000       # Discord absolute limit
SAFE_SLICE = 1900    # wiggle-room for safety


class ForumDuplicator(commands.Cog):
    """Duplicate forum channels, their threads, and all thread messages."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────
    async def _create_dest_forum(
        self, guild: discord.Guild, source: discord.ForumChannel, new_name: str
    ) -> discord.ForumChannel:
        overwrites = dict(source.overwrites)
        base = dict(
            name=new_name,
            topic=source.topic,
            category=source.category,
            overwrites=overwrites,
            slowmode_delay=source.slowmode_delay,
            nsfw=source.nsfw,
            reason=f"Duplicated from {source.name} by ForumDuplicator",
        )
        tags_payload = [{"name": t.name, "emoji": t.emoji} for t in source.available_tags]

        if hasattr(guild, "create_forum_channel"):     # discord.py ≥ 2.2
            return await guild.create_forum_channel(
                **base,
                default_auto_archive_duration=source.default_auto_archive_duration,
                default_thread_slowmode_delay=source.default_thread_slowmode_delay,
                available_tags=tags_payload,
            )
        if hasattr(guild, "create_forum"):             # Pycord / Nextcord
            return await guild.create_forum(**base, available_tags=tags_payload)

        base["type"] = discord.ChannelType.forum       # fallback
        return await guild.create_text_channel(**base)

    async def _match_tags(
        self, dest_forum: discord.ForumChannel, source_tags: List[discord.ForumTag]
    ) -> List[discord.ForumTag]:
        names = {t.name for t in source_tags}
        return [t for t in dest_forum.available_tags if t.name in names]

    async def _copy_attachments(self, atts: List[discord.Attachment]) -> List[discord.File]:
        files = []
        for a in atts:
            if a.size > 8 * 1024 * 1024:       # skip > 8 MiB
                continue
            files.append(discord.File(io.BytesIO(await a.read(use_cached=True)), filename=a.filename))
        return files

    async def _send_long_message(
        self,
        thread: discord.Thread,
        text: str,
        files: Optional[List[discord.File]] = None,
    ):
        """Split *text* into safe chunks and send.  Attachments only on first chunk."""
        chunks = [text[i:i + SAFE_SLICE] for i in range(0, len(text), SAFE_SLICE)] or [""]
        first = True
        for chunk in chunks:
            if not chunk.strip() and not (files if first else None):
                chunk = "-"                       # prevent “empty message” error
            await thread.send(
                content=chunk,
                files=files if first else None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            first = False
            await asyncio.sleep(0.2)

    async def _copy_messages(
        self,
        src_thread: discord.Thread,
        dst_thread: discord.Thread,
        after_first: bool = False,
    ):
        first = True
        async for m in src_thread.history(oldest_first=True, limit=None):
            if first:
                first = False
                if after_first:
                    continue

            content = m.content or ""                   # no prefix / placeholder
            files = await self._copy_attachments(m.attachments)
            await self._send_long_message(dst_thread, content, files)

    async def _get_first_message(self, thread: discord.Thread):
        async for m in thread.history(oldest_first=True, limit=1):
            return m
        return None

    # ──────────────────────────────────────────────
    # Main command
    # ──────────────────────────────────────────────
    @commands.guild_only()
    @checks.admin_or_permissions(manage_channels=True)
    @commands.command(name="duplicateforum", aliases=["cloneforum", "copyforum"])
    async def duplicate_forum(
        self,
        ctx: commands.Context,
        source_forum: discord.ForumChannel,
        *,
        new_name: Optional[str] = None,
    ):
        """Clone an entire forum channel."""
        await ctx.typing()

        if not isinstance(source_forum, discord.ForumChannel):
            await ctx.send("❌ The source channel must be a **forum** channel.")
            return

        dest_forum = await self._create_dest_forum(
            ctx.guild, source_forum, new_name or f"{source_forum.name}-copy"
        )
        await ctx.send(f"📑 Created **{dest_forum.mention}** – copying threads…")

        total, done = len(source_forum.threads), 0
        for th in source_forum.threads:
            first = await self._get_first_message(th)

            if first:
                first_text  = first.content or "-"          # ensure non-empty
                first_files = await self._copy_attachments(first.attachments)
            else:
                first_text, first_files = "-", None

            tags = await self._match_tags(dest_forum, th.applied_tags)
            tmp = await dest_forum.create_thread(
                name=th.name,
                content=first_text,
                applied_tags=tags,
                slowmode_delay=th.slowmode_delay,
                reason=f"Duplicated from {th.name}",
                files=first_files,
            )

            # Pycord/Nextcord returns ThreadWithMessage; unwrap.
            dst_thread = tmp.thread if not hasattr(tmp, "send") and hasattr(tmp, "thread") else tmp

            await self._copy_messages(th, dst_thread, after_first=True)

            done += 1
            await ctx.send(f"  ✔️ Copied **{th.name}** ({done}/{total})", delete_after=5)

        await ctx.send(f"✅ **Done!** Forum duplicated → {dest_forum.mention}")

    # ──────────────────────────────────────────────
    # Utility: inspect guild attrs (owner only)
    # ──────────────────────────────────────────────
    @commands.is_owner()
    @commands.command()
    async def guildattrs(self, ctx):
        """
        DM every *public* attribute on ctx.guild.
        Handles Discord’s 2 000-char limit cleanly.
        """
        # Collect all public attrs
        attrs = sorted(a for a in dir(ctx.guild) if not a.startswith("_"))
        raw   = " ".join(attrs)                    # one long space-separated string

        # Split into ≤ 1 900-char slices (room for code-block markup)
        chunks = [raw[i : i + 1900] for i in range(0, len(raw), 1900)]

        for chunk in chunks:
            await ctx.author.send(f"```py\n{chunk}\n```")


       
