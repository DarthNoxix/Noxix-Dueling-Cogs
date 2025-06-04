# forumduplicator.py
# MIT License
"""
ForumDuplicator – duplicate a Discord forum channel (ChannelType.forum) with all
its threads and messages.

Command
-------
[p]duplicateforum <source_forum_channel> [new_name]

• Creates a new forum channel in the same category (or with the supplied name)
• Copies topic, slow-mode, NSFW, auto-archive, tags, etc.
• Re-creates every thread with its first message.
• Re-posts each subsequent message (oldest-first) with original timestamp
  quoted and attachments ≤ 8 MiB re-uploaded.
• Prepends the author name on every repost to preserve context.
"""

import asyncio
import io
from typing import List, Optional

import discord
from redbot.core import checks, commands

__all__ = ("ForumDuplicator",)


class ForumDuplicator(commands.Cog):
    """Duplicate forum channels, their threads and messages."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ───────── helpers ─────────
    async def _create_dest_forum(
        self, guild: discord.Guild, source: discord.ForumChannel, new_name: str
    ) -> discord.ForumChannel:
        """Create a forum channel mirroring *source* on any d-py fork/version."""
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

        # official discord.py ≥ 2.2
        if hasattr(guild, "create_forum_channel"):
            return await guild.create_forum_channel(
                **base,
                default_auto_archive_duration=source.default_auto_archive_duration,
                default_thread_slowmode_delay=source.default_thread_slowmode_delay,
                available_tags=tags_payload,
            )

        # pycord / nextcord legacy
        if hasattr(guild, "create_forum"):
            return await guild.create_forum(**base, available_tags=tags_payload)

        # very old libraries (< 2.0)
        base["type"] = discord.ChannelType.forum
        return await guild.create_text_channel(**base)

    async def _match_tags(
        self, dest_forum: discord.ForumChannel, src_tags: List[discord.ForumTag]
    ) -> List[discord.ForumTag]:
        names = {t.name for t in src_tags}
        return [t for t in dest_forum.available_tags if t.name in names]

    async def _copy_attachments(self, atts: List[discord.Attachment]) -> List[discord.File]:
        files: List[discord.File] = []
        for a in atts:
            if a.size > 8 * 1024 * 1024:
                continue
            data = await a.read(use_cached=True)
            buf = io.BytesIO(data)
            buf.seek(0)
            files.append(discord.File(buf, filename=a.filename))
        return files

    async def _copy_messages(
        self,
        src_thread: discord.Thread,
        dst_thread: discord.Thread,
        after_first: bool = False,
    ):
        first = True
        async for msg in src_thread.history(oldest_first=True, limit=None):
            if first:
                first = False
                if after_first:
                    continue
            prefix = (
                f"**{msg.author.display_name}** • "
                f"<t:{int(msg.created_at.timestamp())}:f>\n"
            )
            files = await self._copy_attachments(msg.attachments)
            await dst_thread.send(
                content=prefix + (msg.content or "[no text]"),
                files=files or None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await asyncio.sleep(0.2)

    async def _get_first_message(self, thread: discord.Thread):
        async for m in thread.history(oldest_first=True, limit=1):
            return m
        return None

    # ───────── command ─────────
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
        """Duplicate a forum channel including threads and messages."""
        await ctx.typing()

        if not isinstance(source_forum, discord.ForumChannel):
            await ctx.send("❌ Source must be a **forum** channel.")
            return

        new_name = new_name or f"{source_forum.name}-copy"
        dest_forum = await self._create_dest_forum(ctx.guild, source_forum, new_name)
        await ctx.send(f"📑 Created {dest_forum.mention} – cloning threads…")

        total = len(source_forum.threads)
        done = 0

        for thread in source_forum.threads:
            first_msg = await self._get_first_message(thread)
            if first_msg:
                first_content = (
                    f"**{first_msg.author.display_name}** • "
                    f"<t:{int(first_msg.created_at.timestamp())}:f>\n"
                    f"{first_msg.content or '[no text]'}"
                )
                first_files = await self._copy_attachments(first_msg.attachments)
            else:
                first_content = "(thread created empty)"
                first_files = None

            tags = await self._match_tags(dest_forum, thread.applied_tags)

            dest_thread = await dest_forum.create_thread(
                name=thread.name,
                content=first_content,
                applied_tags=tags,
                slowmode_delay=thread.slowmode_delay,
                files=first_files,
                reason=f"Duplicated from {thread.name}",
            )

            await self._copy_messages(thread, dest_thread, after_first=True)

            done += 1
            await ctx.send(
                f"  ✔️ Copied **{thread.name}** ({done}/{total})",
                delete_after=5,
            )

        await ctx.send(
            f"✅ Finished! Forum cloned to {dest_forum.mention} with all threads/messages."
        )

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


       
