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
import discord
from redbot.core import commands, Config
from redbot.core.utils import AsyncIter
from redbot.core.utils.chat_formatting import pagify


class DummyCtx:
    def __init__(self, guild):
        self.guild = guild

    async def send(self, *args, **kwargs):
        pass

    async def tick(self):
        pass

    async def embed_color(self):
        return discord.Color.blue()

    @property
    def typing(self):
        class Dummy:
            async def __aenter__(self): return self
            async def __aexit__(self, exc_type, exc_val, exc_tb): pass
        return Dummy()


class RoleSync(commands.Cog):
    """
    Cross-Server Role Sync

    Sync roles from one server to another on join and on schedule.
    """

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=14000605, force_registration=True)
        default_guild = {
            "roles": {},
        }
        self.config.register_guild(**default_guild)
        self.bg_task = self.bot.loop.create_task(self._scheduled_sync_loop())

    def cog_unload(self):
        self.bg_task.cancel()

    @commands.Cog.listener("on_member_join")
    async def _member_join(self, member: discord.Member):
        if (
            member.bot
            or await self.bot.cog_disabled_in_guild(self, member.guild)
            or not member.guild.me.guild_permissions.manage_roles
        ):
            return

        settings = await self.config.guild(member.guild).roles()
        for pair in settings.values():
            other_server = self.bot.get_guild(pair["other_server"])
            if not other_server:
                continue

            to_check = other_server.get_role(pair["to_check"])
            other_member = other_server.get_member(member.id)
            if not to_check or not other_member or to_check not in other_member.roles:
                continue

            to_add = member.guild.get_role(pair["to_add"])
            if not to_add or to_add >= member.guild.me.top_role:
                continue

            if to_add not in member.roles:
                await member.add_roles(to_add, reason=f"RoleSync: user has {to_check.name} in {other_server.name}")

    @commands.Cog.listener("on_member_update")
    async def _member_update(self, before: discord.Member, after: discord.Member):
        if before.bot:
            return

        for guild in self.bot.guilds:
            if await self.bot.cog_disabled_in_guild(self, guild):
                continue

            settings = await self.config.guild(guild).roles()
            for pair in settings.values():
                if pair["other_server"] != before.guild.id:
                    continue

                to_check = before.guild.get_role(pair["to_check"])
                to_add = guild.get_role(pair["to_add"])
                if not to_check or not to_add:
                    continue

                target_member = guild.get_member(before.id)
                if not target_member or to_add >= guild.me.top_role:
                    continue

                if to_check in before.roles and to_check not in after.roles:
                    if to_add in target_member.roles:
                        await target_member.remove_roles(to_add, reason=f"RoleSync: lost {to_check.name} in {before.guild.name}")
                elif to_check not in before.roles and to_check in after.roles:
                    if to_add not in target_member.roles:
                        await target_member.add_roles(to_add, reason=f"RoleSync: gained {to_check.name} in {before.guild.name}")

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
            except Exception as e:
                print(f"[RoleSync] Scheduled sync error: {e}")
            await asyncio.sleep(3600)

    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    @commands.group(name="rolesync")
    async def _role_sync(self, ctx: commands.Context):
        """RoleSync Settings"""

    @_role_sync.command(name="add")
    async def _add(self, ctx: commands.Context, name: str, role_to_add: discord.Role, other_server: discord.Guild, role_to_check):
        if not (other_member := other_server.get_member(ctx.author.id)) or not other_member.guild_permissions.administrator:
            return await ctx.send("Sorry, you must be an Administrator in both servers.")

        if role_to_add >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("That role is above you in the role hierarchy!")
        elif role_to_add >= ctx.guild.me.top_role:
            return await ctx.send("That role is above me in the role hierarchy!")

        other_server_ctx = copy.copy(ctx)
        other_server_ctx.guild = other_server
        role_to_check = await commands.RoleConverter().convert(other_server_ctx, role_to_check)

        async with self.config.guild(ctx.guild).roles() as settings:
            if name in settings:
                return await ctx.send("There is already an existing role pair with that name!")

            settings[name] = {
                "to_add": role_to_add.id,
                "other_server": other_server.id,
                "to_check": role_to_check.id
            }

        await ctx.send(f"Set: if user has {role_to_check.name} in {other_server.name}, they will receive {role_to_add.mention}.")

    @_role_sync.command(name="remove")
    async def _remove(self, ctx: commands.Context, name: str):
        async with self.config.guild(ctx.guild).roles() as settings:
            if name not in settings:
                return await ctx.send("No role pair with that name found.")
            del settings[name]
        await ctx.tick()

    @_role_sync.command(name="forcesync")
    async def _force_sync(self, ctx: commands.Context, name: str, remove_role: bool = False):
        settings = await self.config.guild(ctx.guild).roles()
        if name not in settings:
            return await ctx.send("No role pair with that name found.")
        await ctx.send("Forced sync started...")
        await self._sync_role(ctx, settings, name, remove_role)

    @_role_sync.command(name="forcesyncall")
    async def _force_sync_all(self, ctx: commands.Context, enter_true_to_confirm: bool, remove_role: bool = False):
        if not enter_true_to_confirm:
            return await ctx.send("Please enter `True` to confirm.")
        settings = await self.config.guild(ctx.guild).roles()
        await ctx.send("Forced sync for all roles started...")
        for name in settings:
            await self._sync_role(ctx, settings, name, remove_role)

    async def _sync_role(self, ctx: commands.Context, settings: dict, name: str, remove_role: bool):
        counter = 0
        to_sync = settings[name]
        other_server = self.bot.get_guild(to_sync["other_server"])
        if not other_server:
            return await ctx.send(f"Server ID {to_sync['other_server']} not found.")

        to_check = other_server.get_role(to_sync["to_check"])
        to_add = ctx.guild.get_role(to_sync["to_add"])
        if not to_check or not to_add:
            return await ctx.send("Missing role or role hierarchy issue.")
        if to_add >= ctx.guild.me.top_role:
            return await ctx.send(f"{to_add.mention} is above me in the role hierarchy!")

        async with ctx.typing():
            async for m in AsyncIter(ctx.guild.members, steps=500):
                counter += 1
                other_member = other_server.get_member(m.id)
                if other_member and to_check in other_member.roles:
                    if to_add not in m.roles:
                        await m.add_roles(to_add, reason=f"RoleSync: user has {to_check.name} in {other_server.name}")
                elif remove_role:
                    if to_add in m.roles:
                        await m.remove_roles(to_add, reason=f"RoleSync: user does not have {to_check.name} anymore")

        await ctx.send(f"Force synced `{name}` for {counter} members.")

    @_role_sync.command(name="view")
    async def _view(self, ctx: commands.Context):
        async with self.config.guild(ctx.guild).roles() as settings:
            role_sync_settings = ""
            for k, v in settings.items():
                to_add = ctx.guild.get_role(v["to_add"])
                other_server = self.bot.get_guild(v["other_server"])
                to_check = other_server.get_role(v["to_check"]) if other_server else None
                if to_add and other_server and to_check:
                    role_sync_settings += f"**{k}:** assign {to_add.mention} if user has {to_check.name} in {other_server.name}\n"
                else:
                    del settings[k]

        for page in pagify(role_sync_settings):
            await ctx.send(embed=discord.Embed(
                title="RoleSync Settings",
                description=page,
                color=await ctx.embed_color()
            ))
