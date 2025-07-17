# openwebuichat.py  —  LLM-driven relevance (no local heuristics)
# MIT License

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
FALLBACK = "I do not know that information, please ask a member of the team."


class OpenWebUIChat(commands.Cog):
    """LLM wiki-bot: memories passed every call; model decides relevance."""

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
            memories=[],
        )

    # ─── lifecycle ──────────────────────────────────────────────
    async def cog_load(self):
        self._worker = asyncio.create_task(self._worker_loop())
        log.info("OpenWebUIChat worker started.")

    async def cog_unload(self):
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    # ─── Open-WebUI REST ────────────────────────────────────────
    async def _settings(self):
        return await asyncio.gather(
            self.config.api_base(), self.config.api_key(), self.config.model()
        )

    async def _api(self, messages: list) -> str:
        base, key, model = await self._settings()
        if not base or not key:
            raise RuntimeError("OpenWebUI URL/key not configured.")
        hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{base.rstrip('/')}/chat/completions",
                             headers=hdr, json={"model": model, "messages": messages})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    # ─── helper ─────────────────────────────────────────────────
    async def _send_split(self, dest, text: str):
        for p in [text[i:i + MAX_DISCORD] for i in range(0, len(text), MAX_DISCORD)] or [""]:
            await dest.send(p or "-")

    @staticmethod
    def _clean_deepseek(t: str) -> str:
        return re.sub(r"<think>.*?</think>", "", t, flags=re.I | re.S).strip()

    # ─── worker loop ────────────────────────────────────────────
    async def _worker_loop(self):
        while True:
            ctx, prompt = await self._queue.get()
            try:
                await self._answer(ctx, prompt)
            except Exception:
                log.exception("Failed to process prompt.")
            finally:
                self._queue.task_done()

    async def _answer(self, ctx: commands.Context, prompt: str):
        await ctx.typing()

        mems = await self.config.memories()
        system = (
            "You are an in-server knowledge bot.\n"
            "If a memory below directly answers the user's question, use it.\n"
            "If none apply, respond **exactly** with:\n"
            f"\"{FALLBACK}\"\n\n"
            "Memories:\n" + "\n".join(f"- {m}" for m in mems)
        )

        reply = await self._api([
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ])

        if (await self.config.model()).lower().startswith("deepseek"):
            reply = self._clean_deepseek(reply)

        await self._send_split(ctx, reply)

    # ─── public command ─────────────────────────────────────────
    @commands.hybrid_command(name="llmchat", with_app_command=True)
    async def llmchat(self, ctx, *, message: str):
        """Ask the wiki-bot a question."""
        if ctx.interaction:
            await ctx.interaction.response.defer()
        await self._queue.put((ctx, message))

    # ─── owner settings ─────────────────────────────────────────
    @commands.group()
    @commands.is_owner()
    async def setopenwebui(self, ctx):
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
        await ctx.send(f"✅ Start-prompt channel set to: {channel.mention}")

    # ─── memory vault ───────────────────────────────────────────
    @commands.group()
    @commands.is_owner()
    async def memory(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @memory.command()
    async def add(self, ctx, *, text: str):
        mems = await self.config.memories()
        mems.append(text)
        await self.config.memories.set(mems)
        await ctx.send("✅ Memory added.")

    @memory.command()
    async def list(self, ctx):
        mems = await self.config.memories()
        if not mems:
            await ctx.send("*No memories stored.*")
        else:
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
