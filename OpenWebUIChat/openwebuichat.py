import asyncio
import contextlib
import logging
import re
from typing import List, Optional, Tuple

import discord
import httpx
from redbot.core import Config, commands

log = logging.getLogger("red.OpenWebUIChat")

MAX_DISCORD = 1990


class OpenWebUIChat(commands.Cog):
    """Chat with a local LLM exposed by Open-WebUI."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._queue: "asyncio.Queue[Tuple[commands.Context, str]]" = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

        # ── Red Config schema ────────────────────────────────────
        self.config = Config.get_conf(self, identifier=0xA71BDDDC0)
        self.config.register_global(
            api_base="",
            api_key="",
            model="mistral",
            channel_id=0,        # optional “startup prompt” channel
            start_prompt="",

            chat_channels=[],    # list[int] – auto-chat channels
            max_history=10,
            memories=[],
        )
        # NEW: initialize custom group for per-channel history
        self.config.init_custom("HIST", 1)

    # ╭─────────── lifecycle ─────────────────────────────────────╮
    async def cog_load(self):
        self._worker = asyncio.create_task(self._worker_loop())
        log.info("OpenWebUIChat worker started.")

    async def cog_unload(self):
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    @commands.Cog.listener()
    async def on_ready(self):
        """Send system prompt once (if configured)."""
        channel_id = await self.config.channel_id()
        if not channel_id:
            return
        chan = self.bot.get_channel(channel_id)
        if not chan:
            return

        prompt = await self.config.start_prompt()
        if not prompt:
            return
        try:
            reply = await self._api_request([{"role": "system", "content": prompt}])
            await self._send_split(chan, reply)
        except Exception as exc:                         # noqa: BLE001
            log.warning("Failed to post start prompt: %s", exc)

    # ╭─────────── helpers: REST + model list ────────────────────╮
    async def _fetch_settings(self):
        return await asyncio.gather(
            self.config.api_base(),
            self.config.api_key(),
            self.config.model(),
        )

    async def _api_request(self, messages: list):
        base, key, model = await self._fetch_settings()
        if not base or not key:
            raise RuntimeError("API URL / key not configured ($setopenwebui).")

        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages}

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{base.rstrip('/')}/chat/completions",
                                  headers=headers, json=payload)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    async def _fetch_models(self) -> List[str]:
        base, key, _ = await self._fetch_settings()
        if not base or not key:
            return []
        headers = {"Authorization": f"Bearer {key}"}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{base.rstrip('/')}/models", headers=headers)
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]

    # ╭─────────── history + memory helpers ──────────────────────╮
    async def _get_history(self, chan_id: int) -> list:
        return await self.config.custom("HIST", chan_id).get_raw(default=[])

    async def _push_history(self, chan_id: int, role: str, content: str):
        key = self.config.custom("HIST", chan_id)
        hist = await key.get_raw(default=[])
        hist.append({"role": role, "content": content})
        hist = hist[-await self.config.max_history():]
        await key.set(hist)

    @staticmethod
    def _clean_deepseek(txt: str) -> str:
        return re.sub(r"<think>.*?</think>", "", txt, flags=re.DOTALL | re.I).strip()

    async def _inject_memories(self, prompt: str, msgs: list) -> list:
        mems = await self.config.memories()
        relevant = [m for m in mems if any(w.lower() in prompt.lower() for w in m.split())]
        if relevant:
            mem_block = "Here are some facts you should use if relevant:\n" + \
                        "\n".join(f"- {m}" for m in relevant)
            msgs.insert(0, {"role": "system", "content": mem_block})
        return msgs

    # ╭─────────── Discord splitting ─────────────────────────────╮
    async def _send_split(self, dest, text: str):   # noqa: ANN001
        chunks = [text[i:i + MAX_DISCORD] for i in range(0, len(text), MAX_DISCORD)] or [""]
        for c in chunks:
            await dest.send(c or "-")

    # ╭─────────── background worker loop ────────────────────────╮
    async def _worker_loop(self):
        while True:
            ctx, prompt = await self._queue.get()
            try:
                await self._handle_prompt(ctx, prompt)
            except Exception:                                # noqa: BLE001
                log.exception("Failed to process prompt.")
            finally:
                self._queue.task_done()

    async def _handle_prompt(self, ctx: commands.Context, prompt: str):
        await ctx.typing()

        msgs = await self._get_history(ctx.channel.id)
        msgs.append({"role": "user", "content": prompt})
        msgs = await self._inject_memories(prompt, msgs)

        reply = await self._api_request(msgs)

        model = await self.config.model()
        if model.lower().startswith("deepseek"):
            reply = self._clean_deepseek(reply)

        await self._send_split(ctx, reply)
        await self._push_history(ctx.channel.id, "user", prompt)
        await self._push_history(ctx.channel.id, "assistant", reply)

    # ╭─────────── event: continuous-chat channels ───────────────╮
    @commands.Cog.listener("on_message")
    async def _auto_chat(self, msg: discord.Message):
        if msg.author.bot:
            return
        chans = await self.config.chat_channels()
        if msg.channel.id not in chans:
            return
        ctx = await self.bot.get_context(msg)
        if ctx.valid:
            await self._queue.put((ctx, msg.content))

    # ╭─────────── public chat + reset cmds ──────────────────────╮
    @commands.hybrid_command(name="llmchat", with_app_command=True)
    async def llmchat(self, ctx: commands.Context, *, message: str):
        """Send a prompt to the LLM."""
        if ctx.interaction:
            await ctx.interaction.response.defer()
        await self._queue.put((ctx, message))

    @commands.hybrid_command()
    async def reset(self, ctx: commands.Context):
        """Clear stored history for this channel."""
        await self.config.custom("HIST", ctx.channel.id).clear()
        await ctx.send("✅ Conversation history cleared for this channel.")

    # ╭─────────── owner-only config commands ────────────────────╮
    @commands.group()
    @commands.is_owner()
    async def setopenwebui(self, ctx: commands.Context):
        """Configure Open-WebUI connection."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @setopenwebui.command()
    async def url(self, ctx, url: str):
        await self.config.api_base.set(url)
        await ctx.send(f"✅ URL set to: {url}")

    @setopenwebui.command()
    async def key(self, ctx, key: str):
        await self.config.api_key.set(key)
        await ctx.send("✅ API key set.")

    @setopenwebui.command()
    async def model(self, ctx, model: str):
        await self.config.model.set(model)
        await ctx.send(f"✅ Model set to: {model}")

    @setopenwebui.command()
    async def channel(self, ctx, channel: discord.TextChannel):
        await self.config.channel_id.set(channel.id)
        await ctx.send(f"✅ Start-prompt channel set to: {channel.mention}")

    # continuous-chat channels
    @setopenwebui.command(name="addchat")
    async def add_chat(self, ctx, channel: discord.TextChannel):
        chans = await self.config.chat_channels()
        if channel.id not in chans:
            chans.append(channel.id)
            await self.config.chat_channels.set(chans)
        await ctx.send(f"✅ {channel.mention} added to auto-chat channels.")

    @setopenwebui.command(name="rmchat")
    async def rm_chat(self, ctx, channel: discord.TextChannel):
        chans = await self.config.chat_channels()
        if channel.id in chans:
            chans.remove(channel.id)
            await self.config.chat_channels.set(chans)
        await ctx.send(f"❌ {channel.mention} removed from auto-chat channels.")

    # ╭─────────── memory vault commands ─────────────────────────╮
    @commands.group()
    @commands.is_owner()
    async def memory(self, ctx):
        """Manage the bot’s memory vault."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @memory.command(name="add")
    async def mem_add(self, ctx, *, text: str):
        mems = await self.config.memories()
        mems.append(text)
        await self.config.memories.set(mems)
        await ctx.send("✅ Memory added.")

    @memory.command(name="list")
    async def mem_list(self, ctx):
        mems = await self.config.memories()
        if not mems:
            await ctx.send("*No memories stored.*")
            return
        listing = "\n".join(f"{i+1}. {m}" for i, m in enumerate(mems))
        await self._send_split(ctx, listing)

    @memory.command(name="del")
    async def mem_del(self, ctx, index: int):
        mems = await self.config.memories()
        if 1 <= index <= len(mems):
            removed = mems.pop(index - 1)
            await self.config.memories.set(mems)
            await ctx.send(f"❌ Removed: {removed}")
        else:
            await ctx.send("Index out of range.")
