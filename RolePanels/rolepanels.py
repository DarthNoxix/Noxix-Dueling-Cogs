# rolepanels.py
# MIT-like – do whatever, just keep this header ;-)

from __future__ import annotations

import asyncio, time, random, discord
from typing import Optional, List, Dict

from redbot.core import commands, Config, checks

ACCENT = 0xE74C3C           # default embed colour
DROPDOWN_LIMIT = 25         # Discord hard-limit per select
BUTTON_ROW_LIMIT = 5        # #buttons shown in one row


# ╭──────────────────────────────────────────────────────────────╮
# │                       MAIN COG                               │
# ╰──────────────────────────────────────────────────────────────╯
class RolePanels(commands.Cog):
    """Self-assign roles with dropdowns **or** buttons."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=12345678, force_registration=True)
        self.config.register_guild(panels={})           # {panel_name: panel_data}
        bot.add_listener(self.on_interaction, "on_interaction")

    # ── helpers ──────────────────────────────────────────────
    async def get_panel(self, guild: discord.Guild, name: str) -> Optional[dict]:
        return (await self.config.guild(guild).panels()).get(name)

    async def save_panel(self, guild: discord.Guild, name: str, data: dict):
        cfg = self.config.guild(guild)
        panels = await cfg.panels()
        panels[name] = data
        await cfg.panels.set(panels)

    # append ONE entry (handles style automatically)
    async def add_entry(self, guild: discord.Guild, panel: str, entry: dict):
        data = await self.get_panel(guild, panel)
        if not data:
            raise KeyError("panel not found")

        style = data["style"]
        if style == "buttons":
            # one entry == one button row
            data["rows"].append(entry)
        else:  # dropdown
            selects = [r for r in data["rows"] if r["kind"] == "select"]
            target: Optional[dict] = None
            if selects and len(selects[-1]["options"]) < DROPDOWN_LIMIT:
                target = selects[-1]
            if target is None:
                # start a new select menu container
                target = dict(
                    kind="select",
                    placeholder="Choose…",
                    custom_id=f"sel_{int(time.time())}_{random.randint(1000,9999)}",
                    options=[]
                )
                data["rows"].append(target)
            target["options"].append(entry)

        await self.save_panel(guild, panel, data)
        return data           # return new state

    # append MANY lines at once
    async def add_many(self, guild: discord.Guild, panel: str, entries: List[dict]):
        for e in entries:
            await self.add_entry(guild, panel, e)

    # rebuild the live message (if published)
    async def refresh_live(self, guild: discord.Guild, panel: str, data: dict):
        if not (mid := data.get("message_id")) or not (cid := data.get("channel_id")):
            return
        channel = guild.get_channel(cid)
        if not channel:
            return
        try:
            msg = await channel.fetch_message(mid)
            await msg.edit(view=self._build_view(guild, panel, data))
        except Exception:
            pass

    # ╭─────────────────────────────────────────────────────────╮
    # │                  COMMAND GROUP (PREFIX)                 │
    # ╰─────────────────────────────────────────────────────────╯
    @commands.group(name="panel", invoke_without_command=True)
    @checks.admin_or_permissions(manage_roles=True)
    async def panel(self, ctx):
        """Create & manage role panels."""
        await ctx.send_help()

    # ── panel new  <name> [style] <title> [colour] ───────────
    @panel.command(name="new")
    async def panel_new(
        self,
        ctx,
        name: str,
        style_or_title: str,
        *extra: str,
    ):
        """
        Start a **draft** panel.

        • `style` = **buttons** *(default)* or **dropdown*.*  
        • Example: `[p]panel new houseroles dropdown "House Roles" #FF9900`
        """
        style = "buttons"
        colour = discord.Colour(ACCENT)
        title_chunks: List[str] = list(extra)

        # detect if first arg is a style keyword
        if style_or_title.lower() in ("buttons", "dropdown"):
            style = style_or_title.lower()
            if not extra:
                return await ctx.send("❌ You still need to supply a title.")
            title = extra[0]
            title_chunks = extra[1:]
        else:
            title = style_or_title

        # last chunk maybe colour hex
        if title_chunks:
            maybe_colour = title_chunks[-1]
            if maybe_colour.startswith("#") and len(maybe_colour) in (4, 7):
                try:
                    colour = discord.Colour(int(maybe_colour.lstrip("#"), 16))
                    title_chunks.pop()
                except ValueError:
                    pass
            if title_chunks:
                title = " ".join([title] + title_chunks)

        if await self.get_panel(ctx.guild, name):
            return await ctx.send("❌ A panel with that name already exists.")

        data = dict(
            title=title,
            colour=colour.value,
            style=style,
            rows=[],                 # buttons or select containers
            message_id=None,
            channel_id=None,
        )
        await self.save_panel(ctx.guild, name, data)
        await ctx.send(f"✅ Draft **{name}** (*{style}*) created – now add items!")

    # ── panel add  <panel>  <emoji>|<label>|<@role> ──────────
    @panel.command(name="add")
    async def panel_add(self, ctx, panel: str, *, line: str):
        """
        Add **one** role line.  
        Format: `emoji | label | role`  (role = mention or ID)
        """
        parsed = await self._parse_line(ctx, line)
        if not parsed:
            return await ctx.send("❌ Format: `emoji | label | role`.")

        try:
            data = await self.add_entry(ctx.guild, panel, parsed)
        except KeyError:
            return await ctx.send("❌ No such panel.")

        await self.refresh_live(ctx.guild, panel, data)
        await ctx.tick()

    # ── panel extend (wizard) ────────────────────────────────
    @panel.command(name="extend", aliases=["x"])
    async def panel_extend(self, ctx, panel: str):
        """Interactive **DM wizard** – paste many lines, finish with **done**."""
        data = await self.get_panel(ctx.guild, panel)
        if not data:
            return await ctx.send("❌ No panel with that name.")

        # DM handshake
        try:
            dm = await ctx.author.create_dm()
        except discord.Forbidden:
            return await ctx.send("❌ I can’t DM you – enable DMs.")

        await dm.send(
            f"🪄 **Extending panel `{panel}`** *(style = {data['style']})*.\n"
            "Paste `emoji | label | role` – one per message.  Type **done** when finished."
        )

        new_entries: List[dict] = []
        def check(m): return m.author == ctx.author and m.channel == dm

        while True:
            try:
                msg = await self.bot.wait_for("message", check=check, timeout=600)
            except asyncio.TimeoutError:
                await dm.send("⏰ Timeout – wizard cancelled.")
                return

            content = msg.content.strip()
            if content.lower() == "done":
                break

            parsed = await self._parse_line(ctx, content)
            if not parsed:
                await dm.send("⚠️  Wrong format.")
                continue
            new_entries.append(parsed)
            await dm.send(f"✅ queued **{parsed['label']}**")

        if not new_entries:
            return await dm.send("Nothing added – wizard closed.")

        await self.add_many(ctx.guild, panel, new_entries)
        await self.refresh_live(ctx.guild, panel, await self.get_panel(ctx.guild, panel))
        await dm.send("🎉 Panel updated!")
        await ctx.tick()

    # ── panel publish  <panel>  #channel ─────────────────────
    @panel.command(name="publish")
    async def panel_publish(self, ctx, panel: str, channel: discord.TextChannel):
        """Send the panel embed + components."""
        data = await self.get_panel(ctx.guild, panel)
        if not data:
            return await ctx.send("❌ No such panel.")

        embed = discord.Embed(title=data["title"], colour=data["colour"])
        view = self._build_view(ctx.guild, panel, data)

        msg = await channel.send(embed=embed, view=view)
        data.update(message_id=msg.id, channel_id=channel.id)
        await self.save_panel(ctx.guild, panel, data)
        await ctx.send(f"🚀 Published in {channel.mention}.")

    # ── panel list ───────────────────────────────────────────
    @panel.command(name="list")
    async def panel_list(self, ctx):
        """Show every stored panel name."""
        keys = (await self.config.guild(ctx.guild).panels()).keys()
        if not keys:
            return await ctx.send("ℹ️ No panels yet.")
        await ctx.send("📋 **Panels:** " + ", ".join(f"`{k}`" for k in keys))

    # ╭──────────────────────── private helpers ───────────────╮
    async def _parse_line(self, ctx, line: str) -> Optional[dict]:
        """Parse 'emoji | label | role' into dict or None."""
        try:
            emoji, label, role_raw = [p.strip() for p in line.split("|", 2)]
            role_id = int(role_raw.strip("<@&>"))
            role = ctx.guild.get_role(role_id)
            if not role:
                return None
            return dict(kind="option", label=label, role=role_id, emoji=emoji or None)
        except Exception:
            return None

    # ╭──────────────────────── view builder ──────────────────╮
    def _build_view(self, guild: discord.Guild, pname: str, data: dict):
        view = discord.ui.View(timeout=None)
        style = data["style"]

        if style == "buttons":
            # chunk 5-per-row (discord will take care visually)
            for row in data["rows"]:
                view.add_item(RoleButton(label=row["label"],
                                         role=row["role"],
                                         emoji=row.get("emoji"),
                                         style=discord.ButtonStyle.secondary))
        else:  # dropdown
            for sel in data["rows"]:
                options = [
                    discord.SelectOption(label=o["label"],
                                         value=str(o["role"]),
                                         emoji=o.get("emoji"))
                    for o in sel["options"]
                ]
                view.add_item(RoleSelect(
                    placeholder=sel.get("placeholder", "Choose…"),
                    custom_id=sel["custom_id"],
                    options=options
                ))
        return view

    # ╭──────────────── listener (no logic) ───────────────────╮
    async def on_interaction(self, inter: discord.Interaction):
        # components handle themselves
        return


# ╭──────────────── COMPONENT CLASSES ─────────────────────────╮
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
                msg = f"❌ Removed **{role.name}**"
            else:
                await inter.user.add_roles(role, reason="RolePanels button")
                msg = f"✅ Assigned **{role.name}**"
            await inter.response.send_message(msg, ephemeral=True, delete_after=5)
        except discord.Forbidden:
            await inter.response.send_message("Missing permissions.", ephemeral=True)


class RoleSelect(discord.ui.Select):
    def __init__(self, **kwargs):
        super().__init__(min_values=0, max_values=1, **kwargs)

    async def callback(self, inter: discord.Interaction):
        chosen = int(self.values[0]) if self.values else None
        role_ids = [int(o.value) for o in self.options]

        to_remove = [inter.guild.get_role(r) for r in role_ids if r != chosen]
        to_add = inter.guild.get_role(chosen) if chosen else None

        try:
            await inter.user.remove_roles(*filter(None, to_remove), reason="RolePanels dropdown")
            if to_add:
                await inter.user.add_roles(to_add, reason="RolePanels dropdown")
            await inter.response.send_message("✅ Updated!", ephemeral=True, delete_after=5)
        except discord.Forbidden:
            await inter.response.send_message("Missing permissions.", ephemeral=True)
