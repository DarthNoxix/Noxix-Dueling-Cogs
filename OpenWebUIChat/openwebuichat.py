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
FALLBACK_MSG = "I do not know that information, please ask a member of the team."
MAX_MEMORIES_IN_PROMPT = 50  # safety cap


class OpenWebUIChat(commands.Cog):
    """Wiki-bot that only answers from its stored memories."""

    # ─────────── init / config ───────────
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._queue: "asyncio.Queue[Tuple[commands.Context, str]]" = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

        self.config = Config.get_conf(self, identifier=0xA71BDDDC0)
        self.config.register_global(
            api_base="",
            api_key="",
            model="mistral",
            channel_id=0,
            start_prompt="",
            memories=[],  # list[str]
        )

    # ─────────── lifecycle ───────────
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
        cid = await self.config.channel_id()
        if not cid:
            return
        chan = self.bot.get_channel(cid)
        sp = await self.config.start_prompt()
        if chan and sp:
            try:
                reply = await self._api_request([{"role": "system", "content": sp}])
                await self._send_split(chan, reply)
            except Exception as exc:  # noqa: BLE001
                log.warning("Start-prompt failed: %s", exc)

    # ─────────── Open-WebUI helper ───────────
    async def _fetch_settings(self):
        return await asyncio.gather(
            self.config.api_base(), self.config.api_key(), self.config.model()
        )

    async def _api_request(self, messages: list) -> str:
        base, key, model = await self._fetch_settings()
        if not base or not key:
            raise RuntimeError("OpenWebUI URL / key not configured.")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{base.rstrip('/')}/chat/completions",
                headers=headers,
                json={"model": model, "messages": messages},
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    # ─────────── small helpers ───────────
    @staticmethod
    def _clean_deepseek(txt: str) -> str:
        return re.sub(r"<think>.*?</think>", "", txt, flags=re.I | re.S).strip()

    async def _send_split(self, dest, text: str):
        parts = [text[i:i + MAX_DISCORD] for i in range(0, len(text), MAX_DISCORD)] or [""]
        for p in parts:
            await dest.send(p or "-")

    # ─────────── background worker ───────────
    async def _worker_loop(self):
        while True:
            ctx, question = await self._queue.get()
            try:
                await self._answer_question(ctx, question)
            except Exception:  # noqa: BLE001
                log.exception("Processing failed.")
            finally:
                self._queue.task_done()

    async def _answer_question(self, ctx: commands.Context, question: str):
        await ctx.typing()

        mems = (await self.config.memories())[:MAX_MEMORIES_IN_PROMPT]
        system_prompt = (
            "You are a strict knowledge bot. "
            "You may ONLY answer using the facts provided below. "
            "If the facts are insufficient, reply exactly with the single word NO_ANSWER.\n\n"
            "Facts:\n"
            + "\n".join(f"- {m}" for m in mems)
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ]

        reply = await self._api_request(messages)

        # Optionally strip DeepSeek thinking
        if (await self.config.model()).lower().startswith("deepseek"):
            reply = self._clean_deepseek(reply)

        if reply.strip().upper() == "NO_ANSWER":
            await ctx.send(FALLBACK_MSG)
        else:
            await self._send_split(ctx, reply)

    # ─────────── public command ───────────
    @commands.hybrid_command(name="llmchat", with_app_command=True)
    async def llmchat(self, ctx: commands.Context, *, message: str):
        """Ask the bot a question; it answers only from stored memories."""
        if ctx.interaction:
            await ctx.interaction.response.defer()
        await self._queue.put((ctx, message))

    # ─────────── owner config ───────────
    @commands.group()
    @commands.is_owner()
    async def setopenwebui(self, ctx):
        """Configure Open-WebUI."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @setopenwebui.command()
    async def url(self, ctx, url: str):
        await self.config.api_base.set(url)
        await ctx.send("✅ URL set.")

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
        await ctx.send(f"✅ Start-prompt channel set: {channel.mention}")

    # ─────────── memory vault ───────────
    @commands.group()
    @commands.is_owner()
    async def memory(self, ctx):
        """Add, list, or delete facts used by the bot."""
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
        await self._send_split(ctx, "\n".join(f"{i+1}. {m}" for i, m in enumerate(mems)))

    @memory.command(name="del")
    async def mem_del(self, ctx, index: int):
        mems = await self.config.memories()
        if 1 <= index <= len(mems):
            removed = mems.pop(index - 1)
            await self.config.memories.set(mems)
            await ctx.send(f"❌ Removed: {removed}")
        else:
            await ctx.send("Index out of range.")