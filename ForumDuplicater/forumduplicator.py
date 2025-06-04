
"""
ForumDuplicator – duplicate a Discord forum channel (ChannelType.forum) with all
its threads and messages.

Command
-------
[p]duplicateforum <source_forum_channel> [new_name]

• Creates a new forum channel in the same category (or with the supplied name).
• Copies topic, default settings, slow-mode, NSFW flag, and tags.
• Re-creates each thread with its name, tags and first message.
• Re-posts every subsequent message (oldest-first) with original timestamp
  quoted and attachments re-uploaded (≤ 8 MiB per file – Discord limit).
• Prepends the author name on every repost to preserve context.

Limitations
-----------
• Messages appear as the bot (Discord doesn’t let us spoof authors).
• Embeds are not replicated (most embeds are generated automatically anyway).
• Very large attachments (> 8 MiB) are skipped to respect default limits.
"""

import asyncio
import io
from datetime import datetime, timezone
from typing import Dict, List, Optional

import discord
from redbot.core import checks, commands

__all__ = ("ForumDuplicator",)


class ForumDuplicator(commands.Cog):
    """Duplicate complete forum channels, threads and messages."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────
    async def _create_dest_forum(
        self, guild: discord.Guild, source: discord.ForumChannel, new_name: str
    ) -> discord.ForumChannel:
        """Create a new forum channel mirroring `source`."""
        # Re-build permission overwrites
        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            target: overwrite for target, overwrite in source.overwrites.items()
        }

        # Clone tags → list[dict] accepted by create_forum_channel
        tags_payload: List[dict] = [
            {"name": t.name, "emoji": t.emoji} for t in source.available_tags
        ]

        dest = await guild.create_forum_channel(
            name=new_name,
            topic=source.topic,
            category=source.category,
            overwrites=overwrites,
            slowmode_delay=source.slowmode_delay,
            nsfw=source.nsfw,
            default_auto_archive_duration=source.default_auto_archive_duration,
            default_thread_slowmode_delay=source.default_thread_slowmode_delay,
            available_tags=tags_payload,
            reason=f"Duplicated from {source.name} by ForumDuplicator",
        )
        return dest

    async def _match_tags(
        self, dest_forum: discord.ForumChannel, source_tags: List[discord.ForumTag]
    ) -> List[discord.ForumTag]:
        """Return tags from dest forum matching names of source_tags."""
        tag_names = {t.name for t in source_tags}
        return [t for t in dest_forum.available_tags if t.name in tag_names]

    async def _copy_attachments(self, attachments: List[discord.Attachment]) -> List[discord.File]:
        """Download attachments (≤ 8 MiB) and return a list of discord.File objects."""
        files: List[discord.File] = []
        for att in attachments:
            if att.size > 8 * 1024 * 1024:
                continue  # skip large files
            data = await att.read(use_cached=True)
            fp = io.BytesIO(data)
            fp.seek(0)
            files.append(discord.File(fp, filename=att.filename))
        return files

    async def _copy_messages(
        self,
        source_thread: discord.Thread,
        dest_thread: discord.Thread,
        after_first: bool = False,
    ) -> None:
        """Copy messages from source to dest (oldest→newest)."""
        history = source_thread.history(oldest_first=True, limit=None)
        first = True
        async for msg in history:
            if first:
                first = False
                if after_first:
                    # already handled by thread creation
                    continue
            prefix = f"**{msg.author.display_name}** • <t:{int(msg.created_at.replace(tzinfo=timezone.utc).timestamp())}:f>\n"
            files = await self._copy_attachments(msg.attachments)
            # Discord auto-removes forbidden @-mentions on bots; keep content safe
            await dest_thread.send(
                content=prefix + (msg.content or "[no text]"),
                files=files or None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            # be gentle with rate-limits
            await asyncio.sleep(0.2)

    # ──────────────────────────────────────────────────────────────
    # Main command
    # ──────────────────────────────────────────────────────────────
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
        """
        Duplicate a forum channel with all threads and messages.

        Usage: `[p]duplicateforum <source_forum_channel> [new_name]`
        """
        await ctx.typing()
        guild = ctx.guild

        if not isinstance(source_forum, discord.ForumChannel):
            await ctx.send("❌ The source channel must be a **forum** channel.")
            return

        new_name = new_name or f"{source_forum.name}-copy"

        # ───── Create destination forum ─────
        dest_forum = await self._create_dest_forum(guild, source_forum, new_name)
        await ctx.send(f"📑 Created new forum **{dest_forum.mention}** – copying threads…")

        # ───── Iterate through threads ─────
        total_threads = len(source_forum.threads)
        done_threads = 0

        for thread in source_forum.threads:
            # Build first-message content & attachments
            first_msg = await thread.history(oldest_first=True, limit=1).flatten()
            first_msg = first_msg[0] if first_msg else None
            first_content = (
                f"**{first_msg.author.display_name}** • "
                f"<t:{int(first_msg.created_at.replace(tzinfo=timezone.utc).timestamp())}:f>\n"
                f"{first_msg.content}"
            )
            first_files = await self._copy_attachments(first_msg.attachments) if first_msg else None

            applied_tags = await self._match_tags(dest_forum, thread.applied_tags)

            # Create the new thread
            dest_thread = await dest_forum.create_thread(
                name=thread.name,
                content=first_content,
                applied_tags=applied_tags,
                slowmode_delay=thread.slowmode_delay,
                reason=f"Duplicated from {thread.name}",
                files=first_files,
            )

            # Copy remaining messages
            await self._copy_messages(
                source_thread=thread, dest_thread=dest_thread, after_first=True
            )

            done_threads += 1
            await ctx.send(
                f"  ✔️ Copied **{thread.name}** ({done_threads}/{total_threads})",
                delete_after=5,
            )

        await ctx.send(
            f"✅ **Done!** Cloned forum channel to {dest_forum.mention} with all threads/messages."
        )

    @commands.is_owner()
    @commands.command()
    async def guildattrs(self, ctx):
        """DMs you all public attrs on ctx.guild."""
        attrs = "\n".join(sorted(a for a in dir(ctx.guild) if not a.startswith("_")))
        for chunk in discord.utils.as_chunks(attrs.splitlines(), 1900):
            await ctx.author.send("```\n" + "\n".join(chunk) + "\n```")
       
