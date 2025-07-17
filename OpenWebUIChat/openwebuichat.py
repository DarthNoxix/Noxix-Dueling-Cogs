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


class OpenWebUIChat(commands.Cog):
    """Chat with a local LLM via Open-WebUI, using stored memories."""

    # ────────────────────────────────────────────
    # Init & config
    # ────────────────────────────────────────────
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._queue: "asyncio.Queue[Tuple[commands.Context, str]]" = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

        self.config = Config.get_conf(self, identifier=0xA71BDDDC0)
        self.config.register_global(
            api_base="",
            api_key="",
            model="mistral",
            channel_id=0,       # optional system-prompt channel
            start_prompt="",
            max_history=10,
            memories=[],        # list[str]
        )
        self.config.init_custom("HIST", 1)  # per-channel history (list)

    # ────────────────────────────────────────────
    # Lifecycle
    # ────────────────────────────────────────────
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
        chan_id = await self.config.channel_id()
        if not chan_id:
            return
        chan = self.bot.get_channel(chan_id)
        if not chan:
            return
        prompt = await self.config.start_prompt()
        if not prompt:
            return
        try:
            reply = await self._api_request([{"role": "system", "content": prompt}])
            await self._send_split(chan, reply)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to post start prompt: %s", exc)

    # ────────────────────────────────────────────
    # Open-WebUI REST helpers
    # ────────────────────────────────────────────
    async def _fetch_settings(self):
        return await asyncio.gather(
            self.config.api_base(),
            self.config.api_key(),
            self.config.model(),
        )

    async def _api_request(self, messages: list) -> str:
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

    # ────────────────────────────────────────────
    # History & memories
    # ────────────────────────────────────────────
    async def _get_history(self, cid: int) -> list:
        return await self.config.custom("HIST", cid).history.get_raw(default=[])

    async def _push_history(self, cid: int, role: str, content: str):
        key = self.config.custom("HIST", cid).history
        hist = await key.get_raw(default=[])
        hist.append({"role": role, "content": content})
        hist = hist[-await self.config.max_history():]
        await key.set_raw(hist)

    @staticmethod
    def _clean_deepseek(txt: str) -> str:
        return re.sub(r"<think>.*?</think>", "", txt, flags=re.I | re.S).strip()

    async def _relevant_memories(self, prompt: str) -> List[str]:
        mems = await self.config.memories()
        prompt_lower = prompt.lower()
        return [m for m in mems if any(word in prompt_lower for word in m.lower().split())]

    # ────────────────────────────────────────────
    # Discord helpers
    # ────────────────────────────────────────────
    async def _send_split(self, dest, text: str):
        parts = [text[i:i + MAX_DISCORD] for i in range(0, len(text), MAX_DISCORD)] or [""]
        for p in parts:
            await dest.send(p or "-")

    # ────────────────────────────────────────────
    # Background worker – processes /llmchat queue
    # ────────────────────────────────────────────
    async def _worker_loop(self):
        while True:
            ctx, prompt = await self._queue.get()
            try:
                await self._handle_prompt(ctx, prompt)
            except Exception:  # noqa: BLE001
                log.exception("Failed to process prompt.")
            finally:
                self._queue.task_done()

    async def _handle_prompt(self, ctx: commands.Context, prompt: str):
        await ctx.typing()

        memories = await self._relevant_memories(prompt)
        if not memories:
            await ctx.send(FALLBACK_MSG)
            return

        system_block = "Here are some facts you must use when they are relevant:\n" + \
                       "\n".join(f"- {m}" for m in memories)

        history = await self._get_history(ctx.channel.id)
        msgs = [{"role": "system", "content": system_block}] + history + \
               [{"role": "user", "content": prompt}]

        reply = await self._api_request(msgs)

        if (await self.config.model()).lower().startswith("deepseek"):
            reply = self._clean_deepseek(reply)

        await self._send_split(ctx, reply)
        await self._push_history(ctx.channel.id, "user", prompt)
        await self._push_history(ctx.channel.id, "assistant", reply)

    # ────────────────────────────────────────────
    # Public commands
    # ────────────────────────────────────────────
    @commands.hybrid_command(name="llmchat", with_app_command=True)
    async def llmchat(self, ctx: commands.Context, *, message: str):
        """Ask the knowledge-bot a question."""
        if ctx.interaction:
            await ctx.interaction.response.defer()
        await self._queue.put((ctx, message))

    @commands.hybrid_command()
    async def reset(self, ctx: commands.Context):
        """Clear history for this channel."""
        await self.config.custom("HIST", ctx.channel.id).history.clear()
        await ctx.send("✅ Conversation history cleared for this channel.")

    # ────────────────────────────────────────────
    # Owner-only config commands
    # ────────────────────────────────────────────
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

    # ────────────────────────────────────────────
    # Memory vault
    # ────────────────────────────────────────────
    @commands.group()
    @commands.is_owner()
    async def memory(self, ctx):
        """Manage stored facts."""
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
        out = "\n".join(f"{i+1}. {m}" for i, m in enumerate(mems))
        await self._send_split(ctx, out)

    @memory.command(name="del")
    async def mem_del(self, ctx, index: int):
        mems = await self.config.memories()
        if 1 <= index <= len(mems):
            removed = mems.pop(index - 1)
            await self.config.memories.set(mems)
            await ctx.send(f"❌ Removed: {removed}")
        else:
            await ctx.send("Index out of range.")


async def setup(bot: commands.Bot):
    await bot.add_cog(OpenWebUIChat(bot))
