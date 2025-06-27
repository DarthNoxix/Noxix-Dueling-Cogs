# forumduplicator.py
# MIT License
"""
ForumDuplicator – duplicate a forum channel (threads + messages) **across guilds**
and optionally keep the destination forum **live‑synced** with the source.

Commands
========
[p]duplicateforum <source_forum_channel> [new_name]
    – clone a forum **inside the same guild** (legacy command – unchanged).

[p]duplicateforumto <source_forum_channel> <dest_category> [new_name]
    – clone a forum into *another* guild (the guild that owns <dest_category>).

[p]syncforums <source_forum_channel> <dest_forum_channel>
    – set up continuous one‑way sync of **message edits** from *source* to *dest*.
      Use this **after** `duplicateforumto` if you need live updates.

Only message *edits* propagate – messages that were never edited remain untouched.
Edits to messages longer than 1 900 chars update only the **first** mirrored chunk.
"""

import asyncio
import io
from typing import Dict, List, Optional

import discord
from redbot.core import checks, commands

MAX_LEN = 2000       # Discord hard limit per message
SAFE_SLICE = 1900    # wiggle‑room for safety when splitting


class ForumDuplicator(commands.Cog):
    """Duplicate forum channels, including their threads and all messages.

    New features (2025‑06‑27):
    • cross‑guild duplication via `duplicateforumto`
    • live one‑way sync of **edits** via `syncforums`
    """

    ############################################################
    # Init / in‑memory link state                                    #
    ############################################################
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Maps a *source forum id* → LinkInfo
        #   LinkInfo = {
        #       "dest_forum_id": int,
        #       "thread_map"   : {src_thread_id: dest_thread_id},
        #       "msg_map"      : {src_msg_id   : dest_msg_id}
        #   }
        self._links: Dict[int, Dict] = {}

    ############################################################
    # Helpers                                                   #
    ############################################################
    async def _create_dest_forum(
        self,
        dest_guild: discord.Guild,
        source: discord.ForumChannel,
        dest_category: discord.CategoryChannel,
        new_name: str,
    ) -> discord.ForumChannel:
        """Create a *new* forum in **dest_guild** under *dest_category*,
        mirroring settings from *source*.
        """
        overwrites = dict(source.overwrites)
        base = dict(
            name=new_name,
            topic=source.topic,
            category=dest_category,
            overwrites=overwrites,
            slowmode_delay=source.slowmode_delay,
            nsfw=source.nsfw,
            reason=f"Duplicated from {source.name} by ForumDuplicator",
        )
        tags_payload = [{"name": t.name, "emoji": t.emoji} for t in source.available_tags]

        if hasattr(dest_guild, "create_forum_channel"):            # discord.py ≥ 2.2
            return await dest_guild.create_forum_channel(
                **base,
                default_auto_archive_duration=source.default_auto_archive_duration,
                default_thread_slowmode_delay=source.default_thread_slowmode_delay,
                available_tags=tags_payload,
            )
        if hasattr(dest_guild, "create_forum"):                    # Pycord / Nextcord
            return await dest_guild.create_forum(**base, available_tags=tags_payload)

        # Fallback – unsupported library: create as text channel of type = forum
        base["type"] = discord.ChannelType.forum
        return await dest_guild.create_text_channel(**base)

    async def _match_tags(
        self, dest_forum: discord.ForumChannel, source_tags: List[discord.ForumTag]
    ) -> List[discord.ForumTag]:
        names = {t.name for t in source_tags}
        return [t for t in dest_forum.available_tags if t.name in names]

    async def _copy_attachments(self, atts: List[discord.Attachment]) -> List[discord.File]:
        files: List[discord.File] = []
        for a in atts:
            if a.size > 8 * 1024 * 1024:       # skip > 8 MiB for safety
                continue
            fp = io.BytesIO(await a.read(use_cached=True))
            files.append(discord.File(fp, filename=a.filename))
        return files

    async def _send_long_message(
        self,
        thread: discord.Thread,
        text: str,
        files: Optional[List[discord.File]] = None,
    ) -> List[discord.Message]:
        """Split *text* into safe chunks, send them, and **return sent messages**."""
        chunks = [text[i : i + SAFE_SLICE] for i in range(0, len(text), SAFE_SLICE)] or [""]
        first = True
        sent: List[discord.Message] = []
        for chunk in chunks:
            if not chunk.strip() and not (files if first else None):
                chunk = "-"                  # prevent “empty message” error
            msg = await thread.send(
                content=chunk,
                files=files if first else None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            sent.append(msg)
            first = False
            await asyncio.sleep(0.2)
        return sent

    async def _copy_messages(
        self,
        src_thread: discord.Thread,
        dst_thread: discord.Thread,
        msg_map: Dict[int, int],
        *,
        after_first: bool = False,
    ):
        """Copy all messages from *src_thread* → *dst_thread* and fill *msg_map*."""
        first = True
        async for m in src_thread.history(oldest_first=True, limit=None):
            if first:
                first = False
                if after_first:
                    continue

            content = m.content or ""
            files   = await self._copy_attachments(m.attachments)
            sent    = await self._send_long_message(dst_thread, content, files)
            # Map **only the first** sent chunk back to the src msg id for edit sync
            if sent:
                msg_map[m.id] = sent[0].id

    async def _get_first_message(self, thread: discord.Thread):
        async for m in thread.history(oldest_first=True, limit=1):
            return m
        return None

    ############################################################
    # Commands – intra‑guild clone (legacy)                    #
    ############################################################
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
        """Clone an entire forum *within the same guild* (unchanged behaviour)."""
        await ctx.typing()

        if not isinstance(source_forum, discord.ForumChannel):
            await ctx.send("❌ The source channel must be a **forum** channel.")
            return

        dest_forum = await self._create_dest_forum(
            ctx.guild, source_forum, source_forum.category, new_name or f"{source_forum.name}-copy"
        )
        await ctx.send(f"📑 Created **{dest_forum.mention}** – copying threads…")

        total, done = len(source_forum.threads), 0
        for th in source_forum.threads:
            first = await self._get_first_message(th)

            if first:
                first_text  = first.content or "-"
                first_files = await self._copy_attachments(first.attachments)
            else:
                first_text, first_files = "-", []

            tags = await self._match_tags(dest_forum, th.applied_tags)
            tmp = await dest_forum.create_thread(
                name=th.name,
                content=first_text,
                applied_tags=tags,
                slowmode_delay=th.slowmode_delay,
                reason=f"Duplicated from {th.name}",
                files=first_files,
            )
            dst_thread = tmp.thread if not hasattr(tmp, "send") and hasattr(tmp, "thread") else tmp

            await self._copy_messages(th, dst_thread, msg_map={}, after_first=True)

            done += 1
            await ctx.send(f"  ✔️ Copied **{th.name}** ({done}/{total})", delete_after=5)

        await ctx.send(f"✅ **Done!** Forum duplicated → {dest_forum.mention}")

    ############################################################
    # Command – cross‑guild clone                              #
    ############################################################
    @commands.command(name="duplicateforumto", aliases=["cloneforumto", "copyforumto"])
    async def duplicate_forum_to(
        self,
        ctx: commands.Context,
        source_forum: discord.ForumChannel,
        dest_category_id: str,
        *,
        new_name: Optional[str] = None,
    ):
        """Clone *source_forum* into a category (using ID) in another server."""
        await ctx.typing()

        try:
            dest_category_obj = self.bot.get_channel(int(dest_category_id))
            if not isinstance(dest_category_obj, discord.CategoryChannel):
                await ctx.send("❌ That ID does not point to a category.")
                return
        except Exception:
            await ctx.send("❌ Invalid category ID.")
            return

        dest_forum = await self._create_dest_forum(
            dest_category_obj.guild,
            source_forum,
            dest_category_obj,
            new_name or source_forum.name,
        )

        await ctx.send(
            f"📑 Created forum **{dest_forum.name}** in {dest_category_obj.guild.name} – copying threads…"
        )



        # Prepare link‑state holders
        thread_map: Dict[int, int] = {}
        msg_map: Dict[int, int]    = {}

        total, done = len(source_forum.threads), 0
        for th in source_forum.threads:
            first = await self._get_first_message(th)

            if first:
                first_text  = first.content or "-"
                first_files = await self._copy_attachments(first.attachments)
            else:
                first_text, first_files = "-", []

            tags = await self._match_tags(dest_forum, th.applied_tags)
            tmp = await dest_forum.create_thread(
                name=th.name,
                content=first_text,
                applied_tags=tags,
                slowmode_delay=th.slowmode_delay,
                reason=f"Duplicated from {th.name}",
                files=first_files,
            )
            dst_thread: discord.Thread
            dst_thread = tmp.thread if not hasattr(tmp, "send") and hasattr(tmp, "thread") else tmp

            # Map thread ids
            thread_map[th.id] = dst_thread.id

            # Copy remaining messages
            await self._copy_messages(th, dst_thread, msg_map=msg_map, after_first=True)

            done += 1
            await ctx.send(f"  ✔️ Copied **{th.name}** ({done}/{total})", delete_after=5)

        # Persist link for potential sync
        self._links[source_forum.id] = {
            "dest_forum_id": dest_forum.id,
            "thread_map"   : thread_map,
            "msg_map"      : msg_map,
        }

        await ctx.send(
            f"✅ **Done!** Forum duplicated → {dest_forum.mention}\n"
            "Run `[p]syncforums {source_forum.mention} {dest_forum.mention}` if you need live edit sync."
        )

    ############################################################
    # Command – start edit‑sync                                #
    ############################################################
    @commands.guild_only()
    @checks.admin_or_permissions(manage_channels=True)
    @commands.command(name="syncforums", aliases=["linkforums"])
    async def sync_forums(
        self,
        ctx: commands.Context,
        source_forum: discord.ForumChannel,
        dest_forum:   discord.ForumChannel,
    ):
        """Enable **one‑way** edit propagation from *source_forum* → *dest_forum*."""
        link = self._links.get(source_forum.id)

        if not link or link["dest_forum_id"] != dest_forum.id:
            await ctx.send("❌ No duplication link found. Please run `duplicateforumto` first.")
            return

        await ctx.send(
            f"🔗 Edit‑sync **enabled** – any message edits in {source_forum.mention} "
            f"will now update its mirror in {dest_forum.mention}."
        )

    ############################################################
    # Listener – propagate edits                               #
    ############################################################
    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        """Mirror edits from linked *source* → *dest*."""
        if payload.guild_id is None:           # DM edit – ignore
            return

        # Determine source forum for this channel/thread
        src_channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(src_channel, discord.Thread):
            return                            # we only track threads
        src_forum_id = src_channel.parent_id
        link = self._links.get(src_forum_id)
        if not link:
            return                            # not a linked forum

        # Resolve dest thread & message
        dest_thread_id = link["thread_map"].get(src_channel.id)
        dest_msg_id    = link["msg_map"].get(payload.message_id)
        if not dest_thread_id or not dest_msg_id:
            return                            # we didn't mirror this message (e.g. too long etc.)

        dest_thread = self.bot.get_channel(dest_thread_id)
        if not dest_thread:
            try:
                dest_thread = await self.bot.fetch_channel(dest_thread_id)
            except discord.HTTPException:
                return

        try:
            dest_msg = await dest_thread.fetch_message(dest_msg_id)
        except discord.NotFound:
            return

        # Raw payload may or may not include content – fetch full after edit
        src_msg = await src_channel.fetch_message(payload.message_id)
        await dest_msg.edit(content=src_msg.content or "-", allowed_mentions=discord.AllowedMentions.none())

    ############################################################
    # Utility – inspect guild attrs (owner only)               #
    ############################################################
    @commands.is_owner()
    @commands.command()
    async def guildattrs(self, ctx):
        """DM every *public* attribute on ctx.guild (for debugging)."""
        attrs = sorted(a for a in dir(ctx.guild) if not a.startswith("_"))
        raw   = " ".join(attrs)
        chunks = [raw[i : i + SAFE_SLICE] for i in range(0, len(raw), SAFE_SLICE)]
        for chunk in chunks:
            await ctx.author.send(f"```py\n{chunk}\n```")
