import asyncio
import contextlib
import logging
import re
from typing import Optional, Tuple

import httpx
import discord
from redbot.core import commands, Config

log = logging.getLogger("red.OpenWebUIChat")

MAX_DISCORD = 1990


class OpenWebUIChat(commands.Cog):
    """
    Chat slash / prefix command that relays the user’s prompt to an
    Open-WebUI `/chat/completions` endpoint and returns the assistant reply.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._queue: "asyncio.Queue[Tuple[commands.Context, str]]" = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

        self.config = Config.get_conf(self, identifier=0xA71BDDDC0)
        self.config.register_global(
            api_base="",
            api_key="",
            model="mistral",
            channel_id=0,
            start_prompt="",
        )

    # ╭─────────── lifecycle ─────────────────────────────────────╮
    async def cog_load(self) -> None:
        self._worker_task = asyncio.create_task(self._worker())
        log.info("OpenWebUIChat worker started.")

    async def cog_unload(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Send the system prompt once, if configured."""
        channel_id = await self.config.channel_id()
        if not channel_id:
            return
        chan = self.bot.get_channel(channel_id)
        if not chan:
            return

        sys_prompt = await self.config.start_prompt()
        if not sys_prompt:
            return

        try:
            reply = await self._openwebui_request(sys_prompt, role="system")
            await chan.send(reply)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to post start prompt: %s", exc)

    # ╭─────────── REST helper ───────────────────────────────────╮
    async def _openwebui_request(self, prompt: str, *, role: str = "user") -> str:
        base = (await self.config.api_base()).rstrip("/")
        key = await self.config.api_key()
        model = await self.config.model()

        if not base or not key:
            raise RuntimeError("API URL / key not configured. Use `$setopenwebui`.")

        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": [{"role": role, "content": prompt}]}

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{base}/chat/completions", headers=headers, json=payload)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    # ╭─────────── DeepSeek cleaner ──────────────────────────────╮
    @staticmethod
    def _clean_deepseek(text: str) -> str:
        """Strip <think>…</think> blocks and tidy whitespace."""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        return text.strip()

    # ╭─────────── Discord split helper ─────────────────────────╮
    async def _send_split(self, dest, text: str) -> None:  # noqa: ANN001
        """Send text in ≤2000-char chunks; send '-' if a chunk would be empty."""
        chunks = [text[i : i + MAX_DISCORD] for i in range(0, len(text), MAX_DISCORD)] or [""]
        for c in chunks:
            await dest.send(c or "-")

    # ╭─────────── background worker ────────────────────────────╮
    async def _worker(self) -> None:
        while True:
            ctx, prompt = await self._queue.get()
            try:
                await self._process(ctx, prompt)
            except Exception:  # noqa: BLE001
                log.exception("Failed to process prompt.")
            finally:
                self._queue.task_done()

    async def _process(self, ctx: commands.Context, prompt: str) -> None:
        await ctx.typing()
        model = await self.config.model()
        reply = await self._openwebui_request(prompt)

        # ── DeepSeek formatting fix ────────────────────────────
        if model.lower().startswith("deepseek"):
            reply = self._clean_deepseek(reply)

        # ── Respond (split if needed) ──────────────────────────
        await self._send_split(ctx, reply)

    # ╭─────────── public commands ──────────────────────────────╮
    @commands.hybrid_command(name="llmchat", with_app_command=True)
    async def llmchat(self, ctx: commands.Context, *, message: str) -> None:
        """Chat with your local LLM via Open-WebUI."""
        # Defer if this came from a slash interaction
        if ctx.interaction:
            await ctx.interaction.response.defer()
        await self._queue.put((ctx, message))

    @commands.hybrid_command()
    async def reset(self, ctx: commands.Context) -> None:
        """Placeholder: clear conversation (no history stored yet)."""
        await ctx.send("✅ Conversation cleared.")

    # ╭─────────── owner-only config cmds ───────────────────────╮
    @commands.group()
    @commands.is_owner()
    async def setopenwebui(self, ctx: commands.Context) -> None:
        """Configure Open-WebUI connection."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @setopenwebui.command()
    async def url(self, ctx: commands.Context, url: str) -> None:
        await self.config.api_base.set(url)
        await ctx.send(f"✅ URL set to: {url}")

    @setopenwebui.command()
    async def key(self, ctx: commands.Context, key: str) -> None:
        await self.config.api_key.set(key)
        await ctx.send("✅ API key set.")

    @setopenwebui.command()
    async def model(self, ctx: commands.Context, model: str) -> None:
        await self.config.model.set(model)
        await ctx.send(f"✅ Model set to: {model}")

    @setopenwebui.command()
    async def channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        await self.config.channel_id.set(channel.id)
        await ctx.send(f"✅ Channel set to: {channel.mention}")

    @setopenwebui.command()
    async def prompt(self, ctx: commands.Context, *, prompt: str) -> None:
        await self.config.start_prompt.set(prompt)
        await ctx.send("✅ System prompt set.")
