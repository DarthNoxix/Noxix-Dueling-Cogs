"""
MIT License

Copyright (c) 2021-present Obi-Wan3

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import asyncio
import copy
from typing import Dict, Any

import discord
from redbot.core import commands, Config
from redbot.core.utils import AsyncIter
from redbot.core.utils.chat_formatting import pagify


# ---------------------------------------------------------------------------#
# Helpers                                                                    #
# ---------------------------------------------------------------------------#
class DummyCtx:
    """Silent ctx replacement for scheduled sync."""

    def __init__(self, guild: discord.Guild):
        self.guild = guild

    async def send(self, *args, **kwargs):
        pass

    async def tick(self):
        pass

    async def embed_color(self):
        return discord.Color.blue()

    @property
    def typing(self):
        class _T:  # pylint: disable=too-few-public-methods
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

        return _T()


# ---------------------------------------------------------------------------#
# Cog                                                                        #
# ---------------------------------------------------------------------------#
class RoleSync(commands.Cog):
    """Cross-server role sync with hourly tasks, DM-on-role, and Assistant API"""

    __version__ = "2.0.0"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.config = Config.get_conf(self, identifier=14000605, force_registration=True)
        default_guild = {
            "roles": {},      # name -> {to_add, other_server, to_check}
            "dmroles": {}     # role_id -> message
        }
        self.config.register_guild(**default_guild)

        # start background hourly sync
        self.bg_task = self.bot.loop.create_task(self._scheduled_sync_loop())

    # -------------------- LIFECYCLE ----------------------------------------
    def cog_unload(self):
        self.bg_task.cancel()

    # -------------------- EVENT LISTENERS ----------------------------------
    @commands.Cog.listener("on_member_join")
    async def _member_join(self, member: discord.Member):
        """When a user joins, apply synced roles immediately."""
        if (
            member.bot
            or await self.bot.cog_disabled_in_guild(self, member.guild)
            or not member.guild.me.guild_permissions.manage_roles
        ):
            return

        await self._apply_synced_roles(member.guild, member)

    @commands.Cog.listener("on_member_update")
    async def _member_update(self, before: discord.Member, after: discord.Member):
        """Handle role changes (source-guild check) and DM-on-role for every guild."""
        if before.bot:
            return

        # 1) Source-guild monitoring – update target guilds
        for guild in self.bot.guilds:
            if await self.bot.cog_disabled_in_guild(self, guild):
                continue

            settings = await self.config.guild(guild).roles()
            for pair in settings.values():
                if pair["other_server"] != before.guild.id:
                    continue

                to_check = before.guild.get_role(pair["to_check"])
                to_add = guild.get_role(pair["to_add"])
                if not to_check or not to_add or to_add >= guild.me.top_role:
                    continue

                target_member = guild.get_member(before.id)
                if not target_member:
                    continue

                # lost role
                if to_check in before.roles and to_check not in after.roles:
                    if to_add in target_member.roles:
                        await target_member.remove_roles(
                            to_add,
                            reason=f"RoleSync: lost {to_check.name} in {before.guild.name}"
                        )
                # gained role
                elif to_check not in before.roles and to_check in after.roles:
                    if to_add not in target_member.roles:
                        await target_member.add_roles(
                            to_add,
                            reason=f"RoleSync: gained {to_check.name} in {before.guild.name}"
                        )

        # 2) DM-on-role for every guild the member belongs to
        for guild in before.mutual_guilds:
            dmroles: Dict[str, str] = await self.config.guild(guild).dmroles()
            if not dmroles:
                continue

            added_roles = [r for r in after.roles if r not in before.roles]
            for role in added_roles:
                if str(role.id) in dmroles:
                    try:
                        await after.send(dmroles[str(role.id)])
                    except discord.Forbidden:
                        pass  # user closed DMs

    # -------------------- BACKGROUND HOURLY SYNC ---------------------------
    async def _scheduled_sync_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                for guild in self.bot.guilds:
                    if await self.bot.cog_disabled_in_guild(self, guild):
                        continue
                    settings = await self.config.guild(guild).roles()
                    for name in settings:
                        await self._sync_role(DummyCtx(guild), settings, name, remove_role=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[RoleSync] Scheduled sync error: {exc}")
            await asyncio.sleep(3600)  # 1 hour

    # -------------------- CORE HELPERS -------------------------------------
    async def _apply_synced_roles(self, guild: discord.Guild, member: discord.Member):
        """Apply roles to `member` based on current config when they join."""
        settings = await self.config.guild(guild).roles()
        for pair in settings.values():
            other_server = self.bot.get_guild(pair["other_server"])
            if not other_server:
                continue
            to_check = other_server.get_role(pair["to_check"])
            other_member = other_server.get_member(member.id)
            if not to_check or not other_member or to_check not in other_member.roles:
                continue

            to_add = guild.get_role(pair["to_add"])
            if not to_add or to_add >= guild.me.top_role:
                continue

            if to_add not in member.roles:
                await member.add_roles(
                    to_add,
                    reason=f"RoleSync: user has {to_check.name} in {other_server.name}"
                )

    async def _sync_role(self, ctx: commands.Context, settings: dict, name: str, remove_role: bool):
        counter = 0
        to_sync = settings[name]

        src_guild = self.bot.get_guild(to_sync["other_server"])
        tgt_guild = ctx.guild
        if not src_guild:
            return await ctx.send(f"Source guild ID {to_sync['other_server']} not found.")

        src_role = src_guild.get_role(to_sync["to_check"])
        tgt_role = tgt_guild.get_role(to_sync["to_add"])
        if not src_role:
            return await ctx.send(f"Source role ID {to_sync['to_check']} not found.")
        if not tgt_role or tgt_role >= tgt_guild.me.top_role:
            return await ctx.send("Target role missing or above bot.")

        async with ctx.typing():
            async for m in AsyncIter(tgt_guild.members, steps=500):
                counter += 1
                src_member = src_guild.get_member(m.id)
                has_src = src_member and src_role in src_member.roles
                has_tgt = tgt_role in m.roles

                if has_src and not has_tgt:
                    await m.add_roles(tgt_role, reason="RoleSync hourly sync")
                elif not has_src and has_tgt and remove_role:
                    await m.remove_roles(tgt_role, reason="RoleSync hourly sync – role revoked")

        await ctx.send(f"Synced **{name}** for {counter} members.")

    # ----------------------------------------------------------------------#
    # COMMANDS                                                              #
    # ----------------------------------------------------------------------#
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    @commands.group(name="rolesync", invoke_without_command=True)
    async def _rolesync(self, ctx: commands.Context):
        """RoleSync root command."""
        await ctx.send_help()

    # ----------- pair management ------------------------------------------
    @_rolesync.command(name="add")
    async def rs_add(
        self,
        ctx: commands.Context,
        name: str,
        role_to_add: discord.Role,
        other_server: discord.Guild,
        role_to_check,
    ):
        """Add a new sync pair."""
        # admin in both guilds?
        if not (other_member := other_server.get_member(ctx.author.id)) or not other_member.guild_permissions.administrator:
            return await ctx.send("You must be an Administrator in both servers.")

        if role_to_add >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("That role is above your highest role.")
        if role_to_add >= ctx.guild.me.top_role:
            return await ctx.send("That role is above my highest role.")

        # resolve role_to_check
        other_ctx = copy.copy(ctx)
        other_ctx.guild = other_server
        role_to_check = await commands.RoleConverter().convert(other_ctx, role_to_check)

        async with self.config.guild(ctx.guild).roles() as cfg:
            if name in cfg:
                return await ctx.send("A rule with that name already exists.")
            cfg[name] = {
                "to_add": role_to_add.id,
                "other_server": other_server.id,
                "to_check": role_to_check.id,
            }

        await ctx.tick()
        await ctx.send(
            f"Users who have **{role_to_check.name}** in **{other_server.name}** "
            f"will receive **{role_to_add.name}** here."
        )

    @_rolesync.command(name="remove")
    async def rs_remove(self, ctx: commands.Context, name: str):
        """Remove a sync pair."""
        async with self.config.guild(ctx.guild).roles() as cfg:
            if name not in cfg:
                return await ctx.send("No rule with that name.")
            del cfg[name]
        await ctx.tick()

    @_rolesync.command(name="view")
    async def rs_view(self, ctx: commands.Context):
        """List all sync pairs."""
        cfg = await self.config.guild(ctx.guild).roles()
        lines = []
        for k, v in cfg.items():
            tgt = ctx.guild.get_role(v["to_add"])
            src_g = self.bot.get_guild(v["other_server"])
            src_r = src_g.get_role(v["to_check"]) if src_g else None
            if tgt and src_g and src_r:
                lines.append(f"**{k}** → give `{tgt.name}` if user has `{src_r.name}` in `{src_g.name}`")
        desc = "\n".join(lines) or "None configured."
        for page in pagify(desc):
            await ctx.send(page)

    # ----------- manual sync ---------------------------------------------
    @_rolesync.command(name="forcesync")
    async def rs_forcesync(self, ctx: commands.Context, name: str, remove_role: bool = False):
        """Run one rule immediately."""
        cfg = await self.config.guild(ctx.guild).roles()
        if name not in cfg:
            return await ctx.send("No rule by that name.")
        await self._sync_role(ctx, cfg, name, remove_role)

    @_rolesync.command(name="forcesyncall")
    async def rs_forcesyncall(self, ctx: commands.Context, remove_role: bool = False):
        """Run all rules immediately."""
        cfg = await self.config.guild(ctx.guild).roles()
        for name in cfg:
            await self._sync_role(ctx, cfg, name, remove_role)

    # ----------- DM-on-role ----------------------------------------------
    @_rolesync.command(name="setdmrole")
    async def rs_setdmrole(self, ctx: commands.Context, role: discord.Role, *, message: str):
        """
        Configure a role to trigger a DM with `message`.
        Example:
        `[p]rolesync setdmrole @Alpha "Here’s your invite: https://discord.gg/xyz"`
        """
        async with self.config.guild(ctx.guild).dmroles() as dm:
            dm[str(role.id)] = message
        await ctx.tick()

    @_rolesync.command(name="removedmrole")
    async def rs_removedmrole(self, ctx: commands.Context, role: discord.Role):
        """Delete a DM-on-role rule."""
        async with self.config.guild(ctx.guild).dmroles() as dm:
            dm.pop(str(role.id), None)
        await ctx.tick()

    @_rolesync.command(name="viewdmroles")
    async def rs_viewdmroles(self, ctx: commands.Context):
        """Show current DM-on-role mappings."""
        dm = await self.config.guild(ctx.guild).dmroles()
        if not dm:
            return await ctx.send("No DM-on-role rules set.")
        lines = [f"<@&{rid}> → {msg[:80]}…" if len(msg) > 80 else f"<@&{rid}> → {msg}" for rid, msg in dm.items()]
        for page in pagify("\n".join(lines)):
            await ctx.send(page)

    # ----------------------------------------------------------------------#
    # ASSISTANT FUNCTION REGISTRATION                                       #
    # ----------------------------------------------------------------------#
    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog: commands.Cog):  # noqa: N802  (Red's camelCase event)
        """Expose main actions to the Assistant cog."""
        async def reg(schema: Dict[str, Any]):
            await cog.register_function(cog_name="RoleSync", schema=schema)

        # add_rolesync_pair
        await reg({
            "name": "add_rolesync_pair",
            "description": "Create a role sync rule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role_to_add_id": {"type": "integer"},
                    "other_server_id": {"type": "integer"},
                    "role_to_check_id": {"type": "integer"}
                },
                "required": ["name", "role_to_add_id", "other_server_id", "role_to_check_id"]
            }
        })

        # remove_rolesync_pair
        await reg({
            "name": "remove_rolesync_pair",
            "description": "Delete a role sync rule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"}
                },
                "required": ["name"]
            }
        })

        # view_rolesync_settings
        await reg({
            "name": "view_rolesync_settings",
            "description": "List all role sync rules.",
            "parameters": {"type": "object", "properties": {}}
        })

        # force_sync_roles
        await reg({
            "name": "force_sync_roles",
            "description": "Run a single role sync rule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "remove_role": {"type": "boolean"}
                },
                "required": ["name"]
            }
        })

        # force_sync_all_roles
        await reg({
            "name": "force_sync_all_roles",
            "description": "Run all role sync rules.",
            "parameters": {
                "type": "object",
                "properties": {
                    "remove_role": {"type": "boolean"}
                }
            }
        })

        # set_dm_role
        await reg({
            "name": "set_dm_role",
            "description": "Configure a DM to send when a role is granted.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role_id": {"type": "integer"},
                    "message": {"type": "string"}
                },
                "required": ["role_id", "message"]
            }
        })

        # remove_dm_role
        await reg({
            "name": "remove_dm_role",
            "description": "Remove a DM-on-role rule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role_id": {"type": "integer"}
                },
                "required": ["role_id"]
            }
        })

    # ----------------- Assistant-exposed handlers --------------------------
    async def add_rolesync_pair(
        self,
        name: str,
        role_to_add_id: int,
        other_server_id: int,
        role_to_check_id: int,
        *,
        guild: discord.Guild
    ):
        role_to_add = guild.get_role(role_to_add_id)
        other_guild = self.bot.get_guild(other_server_id)
        if not role_to_add or not other_guild:
            return {"error": "Role or server not found."}
        role_to_check = other_guild.get_role(role_to_check_id)
        if not role_to_check:
            return {"error": "Source role not found."}
        async with self.config.guild(guild).roles() as cfg:
            cfg[name] = {
                "to_add": role_to_add.id,
                "other_server": other_guild.id,
                "to_check": role_to_check.id
            }
        return {"success": True}

    async def remove_rolesync_pair(self, name: str, *, guild: discord.Guild):
        async with self.config.guild(guild).roles() as cfg:
            cfg.pop(name, None)
        return {"success": True}

    async def view_rolesync_settings(self, *, guild: discord.Guild):
        return {"rules": await self.config.guild(guild).roles()}

    async def force_sync_roles(self, name: str, remove_role: bool = False, *, guild: discord.Guild):
        cfg = await self.config.guild(guild).roles()
        if name not in cfg:
            return {"error": "No such rule"}
        await self._sync_role(DummyCtx(guild), cfg, name, remove_role)
        return {"success": True}

    async def force_sync_all_roles(self, remove_role: bool = False, *, guild: discord.Guild):
        cfg = await self.config.guild(guild).roles()
        for name in cfg:
            await self._sync_role(DummyCtx(guild), cfg, name, remove_role)
        return {"success": True}

    async def set_dm_role(self, role_id: int, message: str, *, guild: discord.Guild):
        async with self.config.guild(guild).dmroles() as dm:
            dm[str(role_id)] = message
        return {"success": True}

    async def remove_dm_role(self, role_id: int, *, guild: discord.Guild):
        async with self.config.guild(guild).dmroles() as dm:
            dm.pop(str(role_id), None)
        return {"success": True}
