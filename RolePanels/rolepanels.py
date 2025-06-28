# rolepanels.py
# MIT-like – do whatever, just keep this header ;-)

from __future__ import annotations

import asyncio
import discord
import json
import io
from typing import Dict, List, Optional

from redbot.core import commands, Config, checks
from redbot.core.utils.chat_formatting import escape

ACCENT = 0xE74C3C        # default embed colour


# ╭──────────────────────────────────────────────────────────────╮
# │                     MAIN COG                                │
# ╰──────────────────────────────────────────────────────────────╯
class RolePanels(commands.Cog):
    """Self-assign roles with sexy dropdowns & buttons."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=12345678, force_registration=True)
        self.config.register_guild(panels={})        # {panel_name: {data}}
        bot.add_listener(self.on_interaction, "on_interaction")

    # ── helpers ──────────────────────────────────────────────
    async def get_panel(self, guild: discord.Guild, name: str) -> Optional[dict]:
        """Fetch a panel dict or None."""
        return (await self.config.guild(guild).panels()).get(name)

    async def save_panel(self, guild: discord.Guild, name: str, data: dict):
        cfg = self.config.guild(guild)
        panels = await cfg.panels()
        panels[name] = data
        await cfg.panels.set(panels)

    async def add_item_to_panel(self, guild: discord.Guild, panel: str, entry: dict):
        """Append a single row/option and auto-save."""
        data = await self.get_panel(guild, panel)
        if not data:
            raise KeyError("panel not found")
        data["rows"].append(entry)
        await self.save_panel(guild, panel, data)

    async def add_many_to_panel(self, guild: discord.Guild, panel: str, new_entries: list[dict]):
        """Bulk append several entries then rebuild the live message (if any)."""
        data = await self.get_panel(guild, panel)
        if not data:
            raise KeyError("panel not found")
        data["rows"].extend(new_entries)
        await self.save_panel(guild, panel, data)

        # if already published → edit the message
        if data.get("message_id") and data.get("channel_id"):
            channel = guild.get_channel(data["channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(data["message_id"])
                    view = self._build_view(guild, panel, data)
                    await msg.edit(view=view)
                except Exception:
                    pass   # swallow – nothing fatal

    # ╭─────────────────────────────────────────────────────────╮
    # │                     COMMAND GROUP                       │
    # ╰─────────────────────────────────────────────────────────╯
    @commands.group(name="panel", invoke_without_command=True)
    @checks.admin_or_permissions(manage_roles=True)
    async def panel(self, ctx):
        """Create & manage role panels."""
        await ctx.send_help()

    # ── panel new ────────────────────────────────────────────
    @panel.command(name="new")
    async def panel_new(self, ctx, name: str, *, title: str, colour: discord.Colour | None = None):
        """Start a new **draft** panel."""
        data = dict(
            title=title,
            colour=(colour or discord.Colour(ACCENT)).value,
            rows=[],                # list of button/select dicts
            message_id=None,
            channel_id=None,
        )
        await self.save_panel(ctx.guild, name, data)
        await ctx.send(f"✅ Draft **{name}** created – now add items!")

    # ── panel add  <panel>  <emoji>|<label>|<@role> ──────────
    @panel.command(name="add")
    async def panel_add(
        self,
        ctx,
        panel: str,
        *,
        line: str
    ):
        """
       Quick one-liner add.

        Format:  `<emoji> | <label> | <role>`  
        Example: `🐺 | House Stark | @Stark`
        """
        try:
            emoji, label, role_raw = [p.strip() for p in line.split("|", maxsplit=2)]
            role_id = int(role_raw.strip("<@&>"))
            role = ctx.guild.get_role(role_id)
            if not role:
                raise ValueError
        except Exception:
            return await ctx.send("❌ Format: `emoji | label | role` (mention or ID).")

        entry = dict(kind="button", label=label, role=role_id, emoji=emoji)
        try:
            await self.add_item_to_panel(ctx.guild, panel, entry)
        except KeyError:
            return await ctx.send("❌ No such draft panel.")

        await ctx.tick()

    # ── panel extend  (wizard) ───────────────────────────────
    @panel.command(name="extend", aliases=["x"])
    @commands.mod_or_permissions(manage_roles=True)
    async def panel_extend(self, ctx, panel: str):
        """DM wizard – paste many `emoji | label | role` lines, then **done**."""
        data = await self.get_panel(ctx.guild, panel)
        if not data:
            return await ctx.send("❌ No panel named that.")

        # open DM
        try:
            dm = await ctx.author.create_dm()
        except discord.Forbidden:
            return await ctx.send("❌ I can’t DM you – enable DMs.")

        await dm.send(
            f"🪄 **Extending panel `{panel}`.**\n"
            "Paste `emoji | label | role` (one per message).  Type **done** to finish."
        )

        new_entries: list[dict] = []

        def check(m): return m.author == ctx.author and m.channel == dm

        while True:
            try:
                msg = await self.bot.wait_for("message", check=check, timeout=600)
            except asyncio.TimeoutError:
                await dm.send("⏰ Timeout – wizard cancelled.")
                return

            line = msg.content.strip()
            if line.lower() == "done":
                break

            try:
                emoji, label, role_raw = [p.strip() for p in line.split("|", maxsplit=2)]
                role_id = int(role_raw.strip("<@&>"))
                role = ctx.guild.get_role(role_id)
                if not role:
                    raise ValueError
            except Exception:
                await dm.send("⚠️  Wrong format. Try again.")
                continue

            new_entries.append(dict(kind="button", label=label, role=role_id, emoji=emoji))
            await dm.send(f"✅ Queued **{label}**.")

        if not new_entries:
            return await dm.send("Nothing added – wizard closed.")

        await self.add_many_to_panel(ctx.guild, panel, new_entries)
        await dm.send("🎉 Panel updated!")
        await ctx.tick()

    # ── panel publish  <panel>  #channel ─────────────────────
    @panel.command(name="publish")
    async def panel_publish(self, ctx, panel: str, channel: discord.TextChannel):
        """Send the panel embed + components to a channel."""
        data = await self.get_panel(ctx.guild, panel)
        if not data:
            return await ctx.send("❌ No such panel.")

        embed = discord.Embed(title=data["title"], colour=data["colour"])
        view = self._build_view(ctx.guild, panel, data)

        msg = await channel.send(embed=embed, view=view)
        data.update(message_id=msg.id, channel_id=channel.id)
        await self.save_panel(ctx.guild, panel, data)
        await ctx.send(f"🚀 Published in {channel.mention}.")

    # ── panel shows ──────────────────────────────────────────
    @panel.command(name="list")
    async def panel_list(self, ctx):
        """List all stored panel drafts & live messages."""
        panels = (await self.config.guild(ctx.guild).panels()).keys()
        if not panels:
            return await ctx.send("ℹ️ No panels yet.")
        await ctx.send("📋 **Panels:**\n" + ", ".join(f"`{p}`" for p in panels))

    # ╭─────────────────────────────────────────────────────────╮
    # │                VIEW / COMPONENT BUILDER                │
    # ╰─────────────────────────────────────────────────────────╯
    def _build_view(self, guild: discord.Guild, panel_name: str, data: dict):
        view = discord.ui.View(timeout=None)
        for row in data["rows"]:
            style = discord.ButtonStyle.secondary
            if row["kind"] == "button":
                style = discord.ButtonStyle.success if row.get("emoji") else discord.ButtonStyle.secondary
                view.add_item(
                    RoleButton(
                        label=row["label"],
                        role=row["role"],
                        emoji=row.get("emoji"),
                        style=style,
                    )
                )
            else:  # select not used in quick-add but kept for completeness
                opts = [
                    discord.SelectOption(label=o["label"], value=str(o["role"]), emoji=o.get("emoji"))
                    for o in row["options"]
                ]
                view.add_item(
                    RoleSelect(
                        placeholder=row.get("placeholder", "Choose…"),
                        custom_id=row.get("custom_id", f"sel_{panel_name}"),
                        options=opts,
                    )
                )
        return view

    # ╭─────────────────────────────────────────────────────────╮
    # │                     LISTENER                            │
    # ╰─────────────────────────────────────────────────────────╯
    async def on_interaction(self, inter: discord.Interaction):
        if inter.type is discord.InteractionType.component:
            # components handle themselves – nothing here
            return


# ╭──────────────────────────────────────────────────────────────╮
# │               COMPONENT CLASSES                             │
# ╰──────────────────────────────────────────────────────────────╯
class RoleButton(discord.ui.Button):
    def __init__(self, **kwargs):
        self.role_id = kwargs.pop("role")
        super().__init__(custom_id=f"btn_{self.role_id}", **kwargs)

    async def callback(self, inter: discord.Interaction):
        role = inter.guild.get_role(self.role_id)
        if not role:
            return await inter.response.send_message("Role vanished.", ephemeral=True)

        try:
            if role in inter.user.roles:
                await inter.user.remove_roles(role, reason="RolePanels button")
                txt = f"❌ Removed **{role.name}**"
            else:
                await inter.user.add_roles(role, reason="RolePanels button")
                txt = f"✅ Assigned **{role.name}**"
            await inter.response.send_message(txt, ephemeral=True)
        except discord.Forbidden:
            await inter.response.send_message("I’m missing permissions.", ephemeral=True)


class RoleSelect(discord.ui.Select):
    def __init__(self, **kwargs):
        super().__init__(min_values=0, max_values=1, **kwargs)

    async def callback(self, inter: discord.Interaction):
        role_id = int(self.values[0]) if self.values else None
        role_ids = [int(o.value) for o in self.options]

        # remove any held role that belongs to this menu
        to_remove = [inter.guild.get_role(r) for r in role_ids if r != role_id]
        to_add = inter.guild.get_role(role_id) if role_id else None

        try:
            await inter.user.remove_roles(*filter(None, to_remove), reason="RolePanels select")
            if to_add:
                await inter.user.add_roles(to_add, reason="RolePanels select")
            await inter.response.send_message("✅ Updated!", ephemeral=True, delete_after=5)
        except discord.Forbidden:
            await inter.response.send_message("Missing permissions.", ephemeral=True)
