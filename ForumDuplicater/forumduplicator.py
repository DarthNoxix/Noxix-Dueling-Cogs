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
        source_forum_id: str,
        dest_forum_id: str,
    ):
        """Enable **one‑way** edit propagation from *source_forum* → *dest_forum* (by ID)."""
        try:
            source_forum = self.bot.get_channel(int(source_forum_id))
            dest_forum = self.bot.get_channel(int(dest_forum_id))
            if not isinstance(source_forum, discord.ForumChannel) or not isinstance(dest_forum, discord.ForumChannel):
                await ctx.send("❌ One or both IDs do not refer to valid forum channels.")
                return
        except Exception:
            await ctx.send("❌ Could not resolve one or both forum channel IDs.")
            return

        link = self._links.get(source_forum.id)

        if not link or link["dest_forum_id"] != dest_forum.id:
            await ctx.send("❌ No duplication link found. Please run `duplicateforumto` first.")
            return

        await ctx.send(
            f"🔗 Edit‑sync **enabled** – any message edits in {source_forum.mention} "
            f"will now update its mirror in {dest_forum.mention}."
        )

    ############################################################
    # Command – force sync                                     #
    ############################################################
    @commands.command(name="forcesyncforums", aliases=["manualsyncforums"])
    @commands.is_owner()
    async def force_sync_forums(
        self,
        ctx: commands.Context,
        source_forum_id: str,
        dest_forum_id: str,
    ):
        """Manually register a sync link using raw channel IDs (across servers)."""
        try:
            source_forum = self.bot.get_channel(int(source_forum_id)) or await self.bot.fetch_channel(int(source_forum_id))
            dest_forum = self.bot.get_channel(int(dest_forum_id)) or await self.bot.fetch_channel(int(dest_forum_id))
        except discord.NotFound:
            await ctx.send("❌ One or both channels could not be found.")
            return
        except discord.Forbidden:
            await ctx.send("❌ Missing permissions to fetch one or both channels.")
            return
        except Exception as e:
            await ctx.send(f"❌ Error fetching channels: {e}")
            return

        if not isinstance(source_forum, discord.ForumChannel) or not isinstance(dest_forum, discord.ForumChannel):
            await ctx.send("❌ One or both IDs do not refer to valid **forum** channels.")
            return

        thread_map = {}
        msg_map = {}

        for src_thread in source_forum.threads:
            dst_thread = discord.utils.get(dest_forum.threads, name=src_thread.name)
            if dst_thread:
                thread_map[src_thread.id] = dst_thread.id

                src_msgs = [msg async for msg in src_thread.history(oldest_first=True, limit=None)]
                dst_msgs = [msg async for msg in dst_thread.history(oldest_first=True, limit=None)]

                for src_msg, dst_msg in zip(src_msgs, dst_msgs):
                    msg_map[src_msg.id] = dst_msg.id

        self._links[source_forum.id] = {
            "dest_forum_id": dest_forum.id,
            "thread_map": thread_map,
            "msg_map": msg_map,
        }

        await ctx.send(
            f"✅ Manual sync link established between {source_forum.mention} and {dest_forum.mention}.\n"
            "Now edits will sync properly."
        )

    ############################################################
    # Command – full sync (retro + live)                       #
    ############################################################
    @commands.command(name="fullsyncforums")
    @commands.is_owner()
    async def full_sync_forums(
        self,
        ctx: commands.Context,
        source_forum_id: str,
        dest_forum_id: str,
    ):
        """Mirror **everything**: missing threads, new messages, edits, deletes."""
        # ── resolve forums ─────────────────────────────────────
        try:
            source_forum = self.bot.get_channel(int(source_forum_id)) or await self.bot.fetch_channel(int(source_forum_id))
            dest_forum   = self.bot.get_channel(int(dest_forum_id))   or await self.bot.fetch_channel(int(dest_forum_id))
        except Exception as e:
            await ctx.send(f"❌ Channel fetch error: {e}")
            return

        if not isinstance(source_forum, discord.ForumChannel) or not isinstance(dest_forum, discord.ForumChannel):
            await ctx.send("❌ Invalid forum channel(s).")
            return

        await ctx.send("🔄 Scanning threads… this might take a moment.")
        thread_map: Dict[int, int] = {}
        msg_map:    Dict[int, int] = {}

        # ── walk every thread in the source forum ─────────────
        for src_thread in source_forum.threads:
            # try to find a matching thread by name in dest
            dst_thread = discord.utils.get(dest_forum.threads, name=src_thread.name)

            # If not present → create + copy
            if dst_thread is None:
                first = await self._get_first_message(src_thread)
                if first:
                    first_text  = first.content or "-"
                    first_files = await self._copy_attachments(first.attachments)
                else:
                    first_text, first_files = "-", []

                tags = await self._match_tags(dest_forum, src_thread.applied_tags)
                tmp = await dest_forum.create_thread(
                    name=src_thread.name,
                    content=first_text,
                    applied_tags=tags,
                    slowmode_delay=src_thread.slowmode_delay,
                    reason="Retro-sync thread",
                    files=first_files,
                )
                dst_thread = tmp.thread if not hasattr(tmp, "send") and hasattr(tmp, "thread") else tmp

                # copy remaining messages
                await self._copy_messages(src_thread, dst_thread, msg_map, after_first=True)

                # map first message if we had one
                if first:
                    sent_first = [m async for m in dst_thread.history(oldest_first=True, limit=1)]
                    if sent_first:
                        msg_map[first.id] = sent_first[0].id

            else:
                # thread already exists; map existing messages
                src_msgs = [m async for m in src_thread.history(oldest_first=True, limit=None)]
                dst_msgs = [m async for m in dst_thread.history(oldest_first=True, limit=None)]
                for s, d in zip(src_msgs, dst_msgs):
                    msg_map[s.id] = d.id

            thread_map[src_thread.id] = dst_thread.id

        # ── persist link (enable live sync) ───────────────────
        self._links[source_forum.id] = {
            "dest_forum_id": dest_forum.id,
            "thread_map"   : thread_map,
            "msg_map"      : msg_map,
            "full_sync"    : True,
        }

        await ctx.send(
            f"✅ Retro-sync complete. **Live full sync** is now active between "
            f"{source_forum.mention} and {dest_forum.mention}."
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
    # Listener – propagate new messages                        #
    ############################################################
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or not isinstance(message.channel, discord.Thread):
            return
        parent_id = message.channel.parent_id
        link = self._links.get(parent_id)
        if not link or not link.get("full_sync"):
            return

        dest_thread_id = link["thread_map"].get(message.channel.id)
        if not dest_thread_id:
            return

        dest_thread = self.bot.get_channel(dest_thread_id)
        if not dest_thread:
            return

        files = [discord.File(io.BytesIO(await a.read()), filename=a.filename)
                for a in message.attachments if a.size <= 8 * 1024 * 1024]
        mirror = await dest_thread.send(content=message.content or "-", files=files)
        link["msg_map"][message.id] = mirror.id

    ############################################################
    # Listener – propagate deletions                           #
    ############################################################
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        parent_id = self.bot.get_channel(payload.channel_id).parent_id
        link = self._links.get(parent_id)
        if not link or not link.get("full_sync"):
            return

        dest_msg_id = link["msg_map"].get(payload.message_id)
        if not dest_msg_id:
            return

        dest_thread_id = link["thread_map"].get(payload.channel_id)
        if not dest_thread_id:
            return

        dest_thread = self.bot.get_channel(dest_thread_id)
        try:
            dest_msg = await dest_thread.fetch_message(dest_msg_id)
            await dest_msg.delete()
        except Exception:
            pass

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


    ############################################################
    # Listener – propagate brand-new threads                   #
    ############################################################
    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        """Mirror new threads opened in a linked (full-sync) source forum."""
        if not isinstance(thread.parent, discord.ForumChannel):
            return

        link = self._links.get(thread.parent.id)
        if not link or not link.get("full_sync"):
            return

        dest_forum = self.bot.get_channel(link["dest_forum_id"])
        if not isinstance(dest_forum, discord.ForumChannel):
            return

        first = await self._get_first_message(thread)
        first_text  = first.content or "-" if first else "-"
        first_files = await self._copy_attachments(first.attachments) if first else []

        tags = await self._match_tags(dest_forum, thread.applied_tags)
        tmp = await dest_forum.create_thread(
            name=thread.name,
            content=first_text,
            applied_tags=tags,
            slowmode_delay=thread.slowmode_delay,
            reason="Live mirror of new thread",
            files=first_files,
        )
        dest_thread = tmp.thread if not hasattr(tmp, "send") and hasattr(tmp, "thread") else tmp

        # map new thread + its first message
        link["thread_map"][thread.id] = dest_thread.id
        if first:
            sent = [m async for m in dest_thread.history(oldest_first=True, limit=1)]
            if sent:
                link["msg_map"][first.id] = sent[0].id
