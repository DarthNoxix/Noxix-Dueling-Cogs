import asyncio
import contextlib
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import discord
import httpx
from redbot.core import Config, commands

log = logging.getLogger("red.OpenWebUIChat")

MAX_MSG = 1900
FALLBACK = "I do not know that information, please ask a member of the team."
SIM_THRESHOLD = 0.80        # cosine similarity gate (0-1)
TOP_K = 5                   # max memories sent to the LLM


class OpenWebUIMemoryBot(commands.Cog):
    """LLM wiki-bot: answers only when a stored memory is relevant."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.q: "asyncio.Queue[Tuple[commands.Context, str]]" = asyncio.Queue()
        self.worker: Optional[asyncio.Task] = None

        self.config = Config.get_conf(self, 0xBADA55, force_registration=True)
        self.config.register_global(
            api_base="",
            api_key="",
            chat_model="mistral",
            embed_model="mistral-embed",
            memories={},          # {name: {"text": str, "vec": List[float]}}
        )

    # ───────────────── lifecycle ─────────────────
    async def cog_load(self):
        self.worker = asyncio.create_task(self._worker())
        log.info("OpenWebUIMemoryBot ready.")

    async def cog_unload(self):
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker

    # ───────────────── backend helpers ───────────
    async def _get_keys(self):
        return await asyncio.gather(
            self.config.api_base(), self.config.api_key(),
            self.config.chat_model(), self.config.embed_model()
        )

    async def _api_chat(self, messages: list) -> str:
        base, key, chat_model, _ = await self._get_keys()
        if not base or not key:
            raise RuntimeError("OpenWebUI URL / key not set.")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{base.rstrip('/')}/chat/completions",
                             headers=headers,
                             json={"model": chat_model, "messages": messages})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    async def _api_embed(self, text: str) -> List[float]:
        base, key, _, embed_model = await self._get_keys()
        if not base or not key:
            raise RuntimeError("OpenWebUI URL / key not set.")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{base.rstrip('/')}/ollama/api/embed",
                            headers=headers,
                            json={"model": embed_model, "input": [text]})
            r.raise_for_status()
            return r.json()["embeddings"][0]

    # ───────────────── memory utils ──────────────
    @staticmethod
    def _cos(a: np.ndarray, b: np.ndarray) -> float:
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))

    async def _best_memories(self, prompt_vec: np.ndarray, mems: Dict[str, Dict]) -> List[str]:
        scored = []
        for name, data in mems.items():
            vec = np.array(data["vec"])
            sim = self._cos(prompt_vec, vec)
            if sim >= SIM_THRESHOLD:
                scored.append((sim, data["text"]))
        scored.sort(reverse=True)
        return [t for _, t in scored[:TOP_K]]

    async def _add_memory(self, name: str, text: str):
        mems = await self.config.memories()
        if name in mems:
            raise ValueError("Memory with that name exists")
        vec = await self._api_embed(text)
        mems[name] = {"text": text, "vec": vec}
        await self.config.memories.set(mems)

    # ───────────────── worker loop ───────────────
    async def _worker(self):
        while True:
            ctx, question = await self.q.get()
            try:
                await self._handle(ctx, question)
            except Exception:
                log.exception("Error while processing prompt")
            finally:
                self.q.task_done()

    async def _handle(self, ctx: commands.Context, question: str):
        await ctx.typing()

        mems = await self.config.memories()
        if not mems:
            return await ctx.send(FALLBACK)

        prompt_vec = np.array(await self._api_embed(question))
        relevant = await self._best_memories(prompt_vec, mems)

        if not relevant:
            return await ctx.send(FALLBACK)

        system = (
            "You are a strict knowledge assistant.\n"
            "Answer ONLY from the facts below. "
            "If the facts are insufficient, reply with exactly NO_ANSWER.\n\n"
            "Facts:\n" + "\n".join(f"- {t}" for t in relevant)
        )

        reply = await self._api_chat([
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ])

        if reply.strip().upper() == "NO_ANSWER":
            await ctx.send(FALLBACK)
        else:
            for part in [reply[i:i + MAX_MSG] for i in range(0, len(reply), MAX_MSG)]:
                await ctx.send(part)

    # ───────────────── commands ──────────────────
    @commands.hybrid_command()
    async def llmchat(self, ctx: commands.Context, *, message: str):
        """Ask the knowledge-bot a question."""
        if ctx.interaction:
            await ctx.interaction.response.defer()
        await self.q.put((ctx, message))

    # —— setup & memory management —— #
    @commands.group()
    @commands.is_owner()
    async def setopenwebui(self, ctx):
        """Configure backend connection."""
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
    async def chatmodel(self, ctx, model: str):
        await self.config.chat_model.set(model)
        await ctx.send(f"✅ Chat model set to {model}")

    @setopenwebui.command()
    async def embedmodel(self, ctx, model: str):
        await self.config.embed_model.set(model)
        await ctx.send(f"✅ Embed model set to {model}")

    # memory CRUD
    @commands.group()
    @commands.is_owner()
    async def memory(self, ctx):
        """Manage stored facts."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @memory.command()
    async def add(self, ctx, name: str, *, text: str):
        """Add a memory entry."""
        try:
            await self._add_memory(name, text)
        except ValueError as e:
            await ctx.send(str(e))
        else:
            await ctx.send("✅ Memory added.")

    @memory.command(name="list")
    async def _list(self, ctx):
        mems = await self.config.memories()
        if not mems:
            return await ctx.send("*No memories stored.*")
        out = "\n".join(f"- **{n}**: {d['text'][:80]}…" for n, d in mems.items())
        await ctx.send(out)

    @memory.command(name="del")
    async def _del(self, ctx, name: str):
        mems = await self.config.memories()
        if name not in mems:
            return await ctx.send("No such memory.")
        del mems[name]
        await self.config.memories.set(mems)
        await ctx.send("❌ Memory removed.")
