from __future__ import annotations
import re
from typing import Literal, Optional

from redbot.core import commands
import discord
import asyncio
import time


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


async def _resolve_member(guild: discord.Guild, user_id: Optional[int] = None, user: Optional[str] = None) -> Optional[discord.Member]:
    # Prefer explicit ID if present
    if user_id is not None:
        m = guild.get_member(int(user_id))
        if m is None:
            try:
                return await guild.fetch_member(int(user_id))
            except discord.HTTPException:
                return None
        return m
    # Try to extract an ID from a mention or raw numbers inside `user`
    if user:
        m_id = None
        id_match = re.search(r"(\d{17,20})", user)
        if id_match:
            try:
                m_id = int(id_match.group(1))
            except ValueError:
                m_id = None
        if m_id:
            m = guild.get_member(m_id)
            if m is None:
                try:
                    return await guild.fetch_member(m_id)
                except discord.HTTPException:
                    pass
            else:
                return m
        # Fall back to name/display_name exact (case-insensitive)
        uname = user.strip().lower()
        for m in guild.members:
            if m.name.lower() == uname or (m.nick and m.nick.lower() == uname) or m.display_name.lower() == uname:
                return m
    return None


class AlicentModeration(commands.Cog):
    """Assistant-exposed moderation tools (roles & kicks) with strict permission checks."""

    def __init__(self, bot):
        self.bot = bot
        self._recent_handled: dict[int, float] = {}

    async def _suppress_assistant_reply(self, channel: discord.TextChannel):
        """Wait briefly for the Assistant's RP reply and delete it if it appears."""
        try:
            def check(m: discord.Message) -> bool:
                if m.channel.id != channel.id:
                    return False
                if not m.author.bot:
                    return False
                txt = (m.content or "").lower()
                return any(
                    key in txt for key in (
                        "i am not equipped with the authority",
                        "i am unable to carry out this action",
                        "i do not possess the authority",
                        "recommend consulting",
                    )
                )
            m = await self.bot.wait_for("message", check=check, timeout=3.0)
            try:
                await m.delete()
            except discord.HTTPException:
                pass
        except asyncio.TimeoutError:
            return

    # ── Natural language router (mention-based) ──────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        Lightweight NL handler so phrases like "@Alicent kick @User for <reason>" work.
        This only triggers when the bot is mentioned and reuses our strict checks.
        """
        # Ignore DMs and bot/webhook messages
        if not message.guild or message.author.bot:
            return

        guild: discord.Guild = message.guild
        me: Optional[discord.Member] = guild.me
        if me is None:
            return

        # Only act if the bot was mentioned
        if not any(m.id == me.id for m in message.mentions):
            return

        # Normalize content
        text = message.content
        low = text.lower()

        # Handle kicks: look for a command like "kick @user ..." when the bot is mentioned
        if "kick" in low:
            # Choose the first mentioned member that is not the bot itself
            target: Optional[discord.Member] = None
            for m in message.mentions:
                if m.id != me.id and isinstance(m, discord.Member):
                    target = m
                    break

            # Fallback: numeric ID in the text
            if target is None:
                id_match = re.search(r"\b(\d{17,20})\b", text)
                if id_match:
                    try:
                        target = await guild.fetch_member(int(id_match.group(1)))
                    except discord.HTTPException:
                        target = None

            if target is None:
                # Not enough info to resolve a target—politely ignore to avoid noise
                return

            # Try to parse a reason after cue words
            reason_match = re.search(r"\b(?:for|because|due to|reason[:\-]?)\s+(.*)$", text, flags=re.IGNORECASE)
            reason = reason_match.group(1).strip() if reason_match else None

            # Call the existing function with full safety checks
            result = await self.alicent_kick_member(
                user_id=target.id,
                reason=reason,
                guild=guild,
                author=message.author,
            )

            # Acknowledge and try to suppress the Assistant's RP reply instead of deleting the user's message
            try:
                try:
                    await message.add_reaction("✅")
                except discord.HTTPException:
                    pass
                asyncio.create_task(self._suppress_assistant_reply(message.channel))
            except Exception:
                pass

            # Send a concise reply with the outcome
            try:
                await message.channel.send(result.get("message", "Done."))
            except discord.HTTPException:
                pass

            return

        # Handle roles: patterns like "give @User @Role", "add role X to @User", "remove/take @Role from @User"
        # Only proceed if text includes keywords indicating role ops
        role_add_cues = ("give", "add", "+=")
        role_remove_cues = ("remove", "take", "-=")
        is_add = (any(cue in low for cue in role_add_cues) and ("role" in low)) or ("<@&" in text)
        is_remove = (any(cue in low for cue in role_remove_cues) and ("role" in low)) or (("without" in low) and ("role" in low))

        if is_add or is_remove:
            # Find target member: first mentioned member that isn't the bot
            target_member: Optional[discord.Member] = None
            for m in message.mentions:
                if m.id != me.id and isinstance(m, discord.Member):
                    target_member = m
                    break
            if target_member is None:
                # Try to resolve "to <name>" / "for <name>"
                name_match = re.search(r"\b(?:to|for)\s+([\w .'`-]{2,32})\b", text, flags=re.IGNORECASE)
                if name_match:
                    cand = name_match.group(1).strip()
                    for m in guild.members:
                        if m.display_name.lower() == cand.lower() or m.name.lower() == cand.lower() or (m.nick and m.nick.lower() == cand.lower()):
                            target_member = m
                            break
            if target_member is None:
                return

            # Find role: prefer role mentions
            target_role: Optional[discord.Role] = None
            if getattr(message, "role_mentions", None):
                if message.role_mentions:
                    target_role = message.role_mentions[0]
            if target_role is None:
                # Try to parse after "role" keyword
                # e.g., "give @User the Friend of The Crown role" or "add role Friend of The Crown to @User"
                role_name = None
                # Pattern: "the <name> role" or "role <name>"
                m1 = re.search(r"\bthe\s+(.+?)\s+role\b", text, flags=re.IGNORECASE)
                m2 = re.search(r"\brole\s+(.+?)\b(?:to|for|on|please|$)", text, flags=re.IGNORECASE)
                if m1:
                    role_name = m1.group(1).strip()
                elif m2:
                    role_name = m2.group(1).strip()
                if role_name:
                    # Trim trailing punctuation
                    role_name = role_name.strip(" .,:;!?")
                    # Resolve by exact (case-insensitive)
                    rnorm = _norm(role_name)
                    for r in guild.roles:
                        if _norm(r.name) == rnorm:
                            target_role = r
                            break
            if target_role is None:
                # Fallback: explicit role ID in text
                rid = re.search(r"<@&(?P<id>\d{17,20})>", text)
                if rid:
                    rid_int = int(rid.group("id"))
                    target_role = guild.get_role(rid_int)
            if target_role is None:
                # Not enough information
                return

            action = "add" if is_add and not is_remove else "remove"

            # Call the existing function with full checks
            result = await self.alicent_manage_role(
                action=action,
                user_id=target_member.id,
                role=str(target_role.id),  # pass ID; function resolves name/id
                guild=guild,
                author=message.author,
            )

            # Acknowledge and try to suppress the Assistant's RP reply instead of deleting the user's message
            try:
                try:
                    await message.add_reaction("✅")
                except discord.HTTPException:
                    pass
                asyncio.create_task(self._suppress_assistant_reply(message.channel))
            except Exception:
                pass

            # Send outcome
            try:
                await message.channel.send(result.get("message", "Done."))
            except discord.HTTPException:
                pass

            return

    # ── Assistant registration ───────────────────────────────────────────
    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog):
        """
        Register assistant-callable functions when your Assistant cog is loaded.
        """
        # Add / remove role
        await cog.register_function(
            cog_name="AlicentModeration",
            schema={
                "name": "alicent_manage_role",
                "description": (
                    "Add or remove a role from a Discord user, with strict checks on the requester, "
                    "the bot, and role hierarchies."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "remove"],
                            "description": "Whether to add or remove the role.",
                        },
                        "user_id": {
                            "type": "integer",
                            "description": "Target user's Discord ID.",
                        },
                        "user": {
                            "type": "string",
                            "description": "Target user as mention, raw ID, or name (fallback if user_id isn’t provided).",
                        },
                        "role": {
                            "type": "string",
                            "description": "Role name or numeric role ID to add/remove.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Optional audit-log reason.",
                        },
                    },
                    "required": ["action", "user_id", "role"],
                },
            },
        )

        # Kick member
        await cog.register_function(
            cog_name="AlicentModeration",
            schema={
                "name": "alicent_kick_member",
                "description": (
                    "Kick a Discord user, with strict checks on the requester, the bot, "
                    "and role hierarchies."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "integer",
                            "description": "Target user's Discord ID.",
                        },
                        "user": {
                            "type": "string",
                            "description": "Target user as mention, raw ID, or name (fallback if user_id isn’t provided).",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Optional audit-log reason.",
                        },
                    },
                    "required": ["user_id"],
                },
            },
        )

    # ── Assistant-callable functions ─────────────────────────────────────
    async def alicent_manage_role(
        self,
        action: Literal["add", "remove"],
        user_id: Optional[int] = None,
        role: str = "",
        reason: Optional[str] = None,
        *,
        user: Optional[str] = None,
        guild: Optional[discord.Guild] = None,
        author: Optional[discord.Member] = None,
        **__,
    ) -> dict:
        """
        Add/remove a role. The Assistant cog should pass `guild` and `author`
        (the requesting human) into this call, same as your BannerlordDocs pattern.
        Returns: {"ok": bool, "message": str}
        """
        # Resolve context
        if guild is None:
            return {"ok": False, "message": "No guild context provided."}
        me: discord.Member = guild.me  # the bot
        if me is None:
            return {"ok": False, "message": "Bot member not found in this guild."}
        if author is None or not isinstance(author, discord.Member):
            return {"ok": False, "message": "Requester context missing."}

        # Resolve target
        member = await _resolve_member(guild, user_id=user_id, user=user)
        if member is None:
            return {"ok": False, "message": "Target user not found (provide a mention, ID, or exact name)."}

        # Resolve role by ID or name (case/space-insensitive)
        target_role: Optional[discord.Role] = None
        role_str = role.strip()
        if role_str.isdigit():
            target_role = guild.get_role(int(role_str))
        if target_role is None:
            rnorm = _norm(role_str)
            for r in guild.roles:
                if _norm(r.name) == rnorm:
                    target_role = r
                    break
        if target_role is None:
            return {"ok": False, "message": f"Role not found: {role_str}"}

        # Permissions checks (requester)
        if action == "add":
            needed = "manage_roles"
            if not (author.guild_permissions.administrator or author.guild_permissions.manage_roles):
                return {"ok": False, "message": "You lack Manage Roles permission."}
        else:
            needed = "manage_roles"
            if not (author.guild_permissions.administrator or author.guild_permissions.manage_roles):
                return {"ok": False, "message": "You lack Manage Roles permission."}

        # Permissions checks (bot)
        if not (me.guild_permissions.administrator or me.guild_permissions.manage_roles):
            return {"ok": False, "message": "I lack Manage Roles permission."}

        # Role hierarchy checks:
        # 1) Role must be below bot's top role
        if target_role >= me.top_role:
            return {"ok": False, "message": "That role is above or equal to my highest role."}
        # 2) Requester must outrank the role unless they are admin
        if not author.guild_permissions.administrator and target_role >= author.top_role:
            return {"ok": False, "message": "That role is above or equal to your highest role."}
        # 3) Cannot modify guild owner via roles (Discord will error anyway)
        if member == guild.owner:
            return {"ok": False, "message": "Cannot modify the server owner."}

        # 4) Requester must outrank the target member unless admin
        if not author.guild_permissions.administrator and member.top_role >= author.top_role:
            return {"ok": False, "message": "Target member's top role is higher or equal to yours."}
        # 5) Bot must outrank the target member
        if member.top_role >= me.top_role:
            return {"ok": False, "message": "Target member's top role is higher or equal to mine."}

        # No-ops
        if action == "add" and target_role in member.roles:
            return {"ok": True, "message": f"{member} already has role '{target_role.name}'."}
        if action == "remove" and target_role not in member.roles:
            return {"ok": True, "message": f"{member} does not have role '{target_role.name}'."}

        # Perform action
        try:
            if action == "add":
                await member.add_roles(target_role, reason=reason or f"Requested by {author} via Assistant")
                return {"ok": True, "message": f"Added '{target_role.name}' to {member}."}
            else:
                await member.remove_roles(target_role, reason=reason or f"Requested by {author} via Assistant")
                return {"ok": True, "message": f"Removed '{target_role.name}' from {member}."}
        except discord.Forbidden:
            return {"ok": False, "message": "Discord forbids this action (check role positions & permissions)."}
        except discord.HTTPException:
            return {"ok": False, "message": "Discord API error while updating roles."}

    async def alicent_kick_member(
        self,
        user_id: Optional[int] = None,
        reason: Optional[str] = None,
        *,
        user: Optional[str] = None,
        guild: Optional[discord.Guild] = None,
        author: Optional[discord.Member] = None,
        **__,
    ) -> dict:
        """
        Kick a member with strict checks.
        Returns: {"ok": bool, "message": str}
        """
        if guild is None:
            return {"ok": False, "message": "No guild context provided."}
        me: discord.Member = guild.me
        if me is None:
            return {"ok": False, "message": "Bot member not found in this guild."}
        if author is None or not isinstance(author, discord.Member):
            return {"ok": False, "message": "Requester context missing."}

        # Resolve target
        member = await _resolve_member(guild, user_id=user_id, user=user)
        if member is None:
            return {"ok": False, "message": "Target user not found (provide a mention, ID, or exact name)."}

        # Requester perms
        if not (author.guild_permissions.administrator or author.guild_permissions.kick_members):
            return {"ok": False, "message": "You lack Kick Members permission."}
        # Bot perms
        if not (me.guild_permissions.administrator or me.guild_permissions.kick_members):
            return {"ok": False, "message": "I lack Kick Members permission."}

        # Safety & hierarchy
        if member == guild.owner:
            return {"ok": False, "message": "Cannot kick the server owner."}
        if member == author:
            return {"ok": False, "message": "You cannot kick yourself."}
        if member == me:
            return {"ok": False, "message": "I will not kick myself."}

        # Requester must outrank target unless admin
        if not author.guild_permissions.administrator and member.top_role >= author.top_role:
            return {"ok": False, "message": "Target member's top role is higher or equal to yours."}
        # Bot must outrank target
        if member.top_role >= me.top_role:
            return {"ok": False, "message": "Target member's top role is higher or equal to mine."}

        try:
            await member.kick(reason=reason or f"Requested by {author} via Assistant")
            return {"ok": True, "message": f"Kicked {member}."}
        except discord.Forbidden:
            return {"ok": False, "message": "Discord forbids this action (insufficient hierarchy/permissions)."}
        except discord.HTTPException:
            return {"ok": False, "message": "Discord API error while kicking the member."}