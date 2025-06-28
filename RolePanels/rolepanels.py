from __future__ import annotations
import discord, asyncio, json, io, textwrap
from redbot.core import commands, Config, checks
from redbot.core.utils.chat_formatting import escape

ACCENT = 0xE74C3C          # default embed colour

class RolePanels(commands.Cog):
    """Self-assign roles with sexy dropdowns & buttons."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=12345678, force_registration=True)
        self.config.register_guild(panels={})        # {panel_name: {data}}
        bot.add_listener(self.on_interaction, "on_interaction")

    # ──────────────────────────────────────────────────────────
    # panels helper: fetch & save
    # ──────────────────────────────────────────────────────────
    async def _get(self, guild, name):           # returns dict or None
        return (await self.config.guild(guild).panels()).get(name)

    async def _save(self, guild, name, data):
        cfg = self.config.guild(guild)
        panels = await cfg.panels()
        panels[name] = data
        await cfg.panels.set(panels)

    # ──────────────────────────────────────────────────────────
    # command group
    # ──────────────────────────────────────────────────────────
    @commands.group(name="panel")
    @checks.admin_or_permissions(manage_guild=True)
    async def panel(self, ctx):
        """Create & manage role panels."""
        if not ctx.invoked_subcommand:
            await ctx.send_help()

    # panel new
    @panel.command(name="new")
    async def panel_new(self, ctx, name: str, *, title: str, colour: discord.Colour | None = None):
        """Start a new draft panel."""
        data = dict(title=title, colour=(colour or discord.Colour(ACCENT)).value,
                    rows=[], message_id=None, channel_id=None)
        await self._save(ctx.guild, name, data)
        await ctx.send(f"✅ Draft **{name}** created.")

    # panel add  <panel>  select|button  "label"  <role_id>  [emoji]
    @panel.command(name="add")
    async def panel_add(self, ctx, panel: str, kind: str,
                        label: str, role_id: int, emoji: commands.PartialEmojiConverter = None):
        """Add a select (dropdown) or button row."""
        data = await self._get(ctx.guild, panel)
        if not data:
            return await ctx.send("❌ No such draft panel.")

        entry = dict(kind=kind.lower(), label=label, role=role_id, emoji=str(emoji) if emoji else None)
        if kind.lower() not in ("button", "select"):
            return await ctx.send("Kind must be **button** or **select**.")

        # add row or create new select with first option
        if kind.lower() == "button":
            data["rows"].append(entry)
        else:
            data["rows"].append(dict(kind="select", placeholder=label,
                                     options=[entry], custom_id=f"sel_{discord.utils.time_snowflake()}"))
        await self._save(ctx.guild, panel, data)
        await ctx.send("➕ Added.")

    # panel choice  <panel>  <label>  <role_id>  [emoji]
    @panel.command(name="choice")
    async def panel_choice(self, ctx, panel: str, label: str,
                           role_id: int, emoji: commands.PartialEmojiConverter = None):
        """Add an option to the *last* select you created."""
        data = await self._get(ctx.guild, panel)
        if not data:
            return await ctx.send("❌ No such draft panel.")

        selects = [r for r in data["rows"] if r["kind"] == "select"]
        if not selects:
            return await ctx.send("You haven’t added a select yet.")

        selects[-1]["options"].append(
            dict(kind="select", label=label, role=role_id, emoji=str(emoji) if emoji else None)
        )
        await self._save(ctx.guild, panel, data)
        await ctx.send("➕ Choice added.")

    # panel publish  <panel>  #channel
    @panel.command(name="publish")
    async def panel_publish(self, ctx, panel: str, channel: discord.TextChannel):
        """Send the panel embed + components to the chosen channel."""
        data = await self._get(ctx.guild, panel)
        if not data:
            return await ctx.send("❌ No such panel.")

        embed = discord.Embed(title=data["title"], colour=data["colour"])
        view  = self._build_view(ctx.guild, panel, data)

        msg = await channel.send(embed=embed, view=view)
        data.update(message_id=msg.id, channel_id=channel.id)
        await self._save(ctx.guild, panel, data)
        await ctx.send(f"🚀 Published in {channel.mention}.")

    # ──────────────────────────────────────────────────────────
    # COMPONENT builder
    # ──────────────────────────────────────────────────────────
    def _build_view(self, guild: discord.Guild, panel_name: str, data: dict):
        view = discord.ui.View(timeout=None)
        for row in data["rows"]:
            if row["kind"] == "button":
                style = discord.ButtonStyle.success if row["emoji"] else discord.ButtonStyle.secondary
                view.add_item(RoleButton(label=row["label"], role=row["role"],
                                         emoji=row.get("emoji"), style=style))
            else:   # select
                opts = []
                for opt in row["options"]:
                    opts.append(discord.SelectOption(label=opt["label"], value=str(opt["role"]),
                                                     emoji=opt.get("emoji")))
                view.add_item(RoleSelect(placeholder=row["placeholder"],
                                         custom_id=row["custom_id"], options=opts))
        return view

    # ──────────────────────────────────────────────────────────
    # LISTENER
    # ──────────────────────────────────────────────────────────
    async def on_interaction(self, inter: discord.Interaction):
        if not inter.guild or not inter.data:
            return
        if inter.type is not discord.InteractionType.component:
            return

        # handled inside the component classes, nothing here


# ──────────────────────────────────────────────────────────
#  COMPONENT CLASSES
# ──────────────────────────────────────────────────────────
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
        # value will be role ID as str
        role_id = int(self.values[0]) if self.values else None
        roles = [int(o.value) for o in self.options]

        # remove any already-held role from this menu
        to_remove = [inter.guild.get_role(r) for r in roles if r != role_id]
        to_add    = inter.guild.get_role(role_id) if role_id else None

        try:
            await inter.user.remove_roles(*filter(None, to_remove), reason="RolePanels select")
            if to_add:
                await inter.user.add_roles(to_add, reason="RolePanels select")
            await inter.response.send_message("✅ Updated!", ephemeral=True, delete_after=5)
        except discord.Forbidden:
            await inter.response.send_message("Missing permissions.", ephemeral=True)
