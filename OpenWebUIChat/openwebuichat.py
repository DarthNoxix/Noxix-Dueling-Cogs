import asyncio
import contextlib
import logging
from typing import Dict, List, Optional, Tuple
import re
import numpy as np
import discord
import httpx
from redbot.core import Config, commands
from rank_bm25 import BM25Okapi

log = logging.getLogger("red.OpenWebUIChat")

MAX_MSG = 1900
FALLBACK = "My lords and ladies, I lack the knowledge to answer your query. Pray, seek counsel from the learned members of our Discord court."
SIM_THRESHOLD = 0.8  # Cosine similarity gate (0-1), matching ChatGPT
TOP_K = 9  # Max memories sent to the LLM, matching ChatGPT

class OpenWebUIMemoryBot(commands.Cog):
    """A regal assistant, in the likeness of Queen Alicent Hightower, guiding courtiers through the 'A Dance of Dragons' mod with wisdom and authority."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.q: "asyncio.Queue[Tuple[commands.Context, str]]" = asyncio.Queue()
        self.worker: Optional[asyncio.Task] = None
        self.config = Config.get_conf(self, 0xBADA55, force_registration=True)
        self.config.register_global(
            api_base="",
            api_key="",
            chat_model="deepseek-r1:8b",
            embed_model="bge-large-en-v1.5",
            memories={},  # {name: {"text": str, "vec": List[float]}}
        )

    # ───────────────── lifecycle ─────────────────
    async def cog_load(self):
        self.worker = asyncio.create_task(self._worker())
        log.info("OpenWebUIMemoryBot, in service to Queen Alicent, is ready.")

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
            raise RuntimeError("OpenWebUI URL or key not set, as befits a royal court.")
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
            raise RuntimeError("OpenWebUI URL or key not set, as befits a royal court.")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        ollama_base = base.replace('/api', '/ollama')
        async with httpx.AsyncClient(timeout=60) as c:
            try:
                r = await c.post(f"{ollama_base.rstrip('/')}/api/embed",
                                headers=headers,
                                json={"model": embed_model, "input": [self._normalize_text(text)]})
                r.raise_for_status()
                return r.json()["embeddings"][0]
            except httpx.HTTPStatusError:
                r = await c.post(f"{base.rstrip('/')}/api/embeddings",
                                headers=headers,
                                json={"model": embed_model, "input": [self._normalize_text(text)]})
                r.raise_for_status()
                return r.json()["data"][0]["embedding"]

    def _normalize_text(self, text: str) -> str:
        text = text.lower()
        text = re.sub(r'[^\w\s]', '', text)  # Remove punctuation
        text = re.sub(r'\bdo\s+i\b|\bcan\s+i\b|\bhow\s+to\b|\bhow\s+do\s+i\b', 'can i', text)
        text = re.sub(r'\bi\s+download\b|\bi\s+get\b', 'i download', text)
        text = re.sub(r'\badod\b|\ba\s+dance\s+of\s+dragons\b|\badod\s+mod\b|\badod\s+mdo\b', 'ADOD', text)
        text = re.sub(r'\bdownload\s+the\s+ADOD\s+mod\b|\bget\s+the\s+ADOD\s+mod\b', 'download ADOD mod', text)
        text = re.sub(r'\bwher\b|\bwhere\b', 'where', text)
        text = re.sub(r'\bdownlod\b|\bdl\b', 'download', text)
        text = re.sub(r'\bmod\b|\bmdo\b', 'ADOD mod', text)
        return text.strip()

    # ───────────────── memory utils ──────────────
    @staticmethod
    def _cos(a: np.ndarray, b: np.ndarray) -> float:
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))

    async def _best_memories(self, prompt_vec: np.ndarray, question: str, mems: Dict[str, Dict]) -> List[str]:
        # Dense retrieval (cosine similarity)
        dense_scored = []
        for name, data in mems.items():
            vec = np.array(data["vec"])
            sim = self._cos(prompt_vec, vec)
            log.info(f"Memory '{name}' dense similarity: {sim:.3f}")
            if sim >= SIM_THRESHOLD:
                dense_scored.append((sim, data["text"]))
        dense_scored.sort(reverse=True)
        
        # Sparse retrieval (BM25)
        texts = [data["text"] for data in mems.values()]
        bm25 = BM25Okapi([self._normalize_text(t).split() for t in texts])
        query_tokens = self._normalize_text(question).split()
        bm25_scores = bm25.get_scores(query_tokens)
        sparse_scored = [(score, text) for score, text in zip(bm25_scores, texts) if score > 0]
        sparse_scored.sort(reverse=True)
        
        # Combine dense and sparse (hybrid retrieval)
        scored = {}
        for sim, text in dense_scored:
            scored[text] = scored.get(text, 0) + sim * 0.7  # Weight dense higher
        for score, text in sparse_scored:
            scored[text] = scored.get(text, 0) + score * 0.3  # Weight sparse lower
        sorted_scored = sorted(scored.items(), key=lambda x: x[1], reverse=True)
        
        # Select top_k or fallback to best match
        relevant = [text for text, score in sorted_scored[:TOP_K]]
        if not relevant and sorted_scored:
            best_text = sorted_scored[0][0]
            log.info(f"No memories above threshold, using best match: '{best_text}' (score: {sorted_scored[0][1]:.3f})")
            relevant.append(best_text)
        
        log.info(f"Selected memories: {relevant}")
        return relevant

    async def _add_memory(self, name: str, text: str):
        mems = await self.config.memories()
        if name in mems:
            raise ValueError("A memory with that name already exists in the royal archives.")
        vec = await self._api_embed(text)
        mems[name] = {"text": text, "vec": vec}
        await self.config.memories.set(mems)
        log.info(f"Added memory '{name}' with embedding length: {len(vec)}")

    # ───────────────── worker loop ───────────────
    async def _worker(self):
        while True:
            ctx, question = await self.q.get()
            try:
                await self._handle(ctx, question)
            except Exception:
                log.exception("Error while processing the courtier’s query")
            finally:
                self.q.task_done()

    async def _handle(self, ctx: commands.Context, question: str):
        await ctx.typing()

        mems = await self.config.memories()
        if not mems:
            log.info("No memories stored in the royal archives.")
            return await ctx.send(FALLBACK)

        prompt_vec = np.array(await self._api_embed(question))
        relevant = await self._best_memories(prompt_vec, question, mems)

        if not relevant:
            log.info(f"No relevant memories found for query: '{question}'")
            return await ctx.send(FALLBACK)

        log.info(f"Selected memories: {relevant}")
        system = (
            "You are Alicent Hightower, Queen of the Seven Kingdoms, entrusted with guiding courtiers through the 'A Dance of Dragons' mod with wisdom and authority.\n"
            "Speak with the dignity, poise, and firmness befitting your regal standing. "
            "Answer queries using only the facts provided below, weaving them into a response that reflects your comprehensive knowledge of the mod. "
            "For inquiries about procuring the A Dance of Dragons (ADOD) mod, direct courtiers to the official Discord at https://discord.gg/gameofthronesmod, where further guidance awaits. "
            "If facts contain placeholders like 'HERE', interpret them as referring to the official Discord link. "
            "Always provide a clear, authoritative, and helpful response, even for vague or misspelled queries, and never return 'NO_ANSWER'.\n\n"
            "Facts:\n" + "\n".join(f"- {t}" for t in relevant)
        )

        reply = await self._api_chat([
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ])

        log.info(f"Royal decree: '{reply}'")
        for part in [reply[i:i + MAX_MSG] for i in range(0, len(reply), MAX_MSG)]:
            await ctx.send(part)

    # ───────────────── commands ──────────────────
    @commands.hybrid_command()
    async def llmchat(self, ctx: commands.Context, *, message: str):
        """Seek the wisdom of Queen Alicent Hightower regarding the A Dance of Dragons mod."""
        if ctx.interaction:
            await ctx.interaction.response.defer()
        await self.q.put((ctx, message))

    # ───────────────── setup & memory management ─────────────────
    @commands.group()
    @commands.is_owner()
    async def setopenwebui(self, ctx):
        """Configure the royal connection to the OpenWebUI archives."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @setopenwebui.command()
    async def url(self, ctx, url: str):
        await self.config.api_base.set(url)
        await ctx.send("✅ Royal URL decreed.")

    @setopenwebui.command()
    async def key(self, ctx, key: str):
        await self.config.api_key.set(key)
        await ctx.send("✅ Royal key secured.")

    @setopenwebui.command()
    async def chatmodel(self, ctx, model: str):
        await self.config.chat_model.set(model)
        await ctx.send(f"✅ Chat model decreed as {model}.")

    @setopenwebui.command()
    async def embedmodel(self, ctx, model: str):
        await self.config.embed_model.set(model)
        await ctx.send(f"✅ Embed model decreed as {model}.")

    @commands.group()
    @commands.is_owner()
    async def memory(self, ctx):
        """Manage the royal archives of the A Dance of Dragons mod."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @memory.command()
    async def add(self, ctx, name: str, *, text: str):
        """Add a memory to the royal archives."""
        try:
            await self._add_memory(name, text)
        except ValueError as e:
            await ctx.send(str(e))
        else:
            await ctx.send("✅ Memory enshrined in the royal archives.")

    @memory.command(name="list")
    async def _list(self, ctx):
        mems = await self.config.memories()
        if not mems:
            return await ctx.send("*The royal archives are empty.*")
        out = "\n".join(f"- **{n}**: {d['text'][:80]}…" for n, d in mems.items())
        await ctx.send(out)

    @memory.command(name="del")
    async def _del(self, ctx, name: str):
        mems = await self.config.memories()
        if name not in mems:
            return await ctx.send("No such memory exists in the royal archives.")
        del mems[name]
        await self.config.memories.set(mems)
        await ctx.send("❌ Memory removed from the royal archives.")