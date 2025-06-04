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

        if hasattr(guild, "create_forum_channel"):  # discord.py ≥ 2.2
            return await guild.create_forum_channel(
                **base,
                default_auto_archive_duration=source.default_auto_archive_duration,
                default_thread_slowmode_delay=source.default_thread_slowmode_delay,
                available_tags=tags_payload,
            )
        if hasattr(guild, "create_forum"):  # Pycord / Nextcord legacy
            return await guild.create_forum(**base, available_tags=tags_payload)

        base["type"] = discord.ChannelType.forum  # very old libraries
        return await guild.create_text_channel(**base)

    async def _match_tags(
        self, dest_forum: discord.ForumChannel, source_tags: List[discord.ForumTag]
    ) -> List[discord.ForumTag]:
        names = {t.name for t in source_tags}
        return [t for t in dest_forum.available_tags if t.name in names]

    async def _copy_attachments(self, atts: List[discord.Attachment]) -> List[discord.File]:
        files = []
        for a in atts:
            if a.size > 8 * 1024 * 1024:
                continue
            files.append(discord.File(io.BytesIO(await a.read(use_cached=True)), filename=a.filename))
        return files

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
            prefix = (
                f"**{m.author.display_name}** • "
                f"<t:{int(m.created_at.replace(tzinfo=timezone.utc).timestamp())}:f>\n"
            )
            files = await self._copy_attachments(m.attachments)
            await dst_thread.send(
                content=prefix + (m.content or "[no text]"),
                files=files or None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await asyncio.sleep(0.2)

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
                first_content = (
                    f"**{first.author.display_name}** • "
                    f"<t:{int(first.created_at.timestamp())}:f>\n"
                    f"{first.content or '[no text]'}"
                )
                first_files = await self._copy_attachments(first.attachments)
            else:
                first_content, first_files = "(thread created empty)", None

            tags = await self._match_tags(dest_forum, th.applied_tags)
            tmp = await dest_forum.create_thread(
                name=th.name,
                content=first_content,
                applied_tags=tags,
                slowmode_delay=th.slowmode_delay,
                reason=f"Duplicated from {th.name}",
                files=first_files,
            )

            # Pycord/Nextcord returns ThreadWithMessage; unwrap.
            dest_thread = tmp.thread if not hasattr(tmp, "send") and hasattr(tmp, "thread") else tmp

            await self._copy_messages(th, dest_thread, after_first=True)

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


       
