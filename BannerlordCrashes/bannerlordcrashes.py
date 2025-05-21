from __future__ import annotations

import os, re, time, unicodedata, math, json, asyncio
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp, openai, tiktoken
from bs4 import BeautifulSoup
from redbot.core import commands, Config

CRASH_URL = "https://docs.bannerlordmodding.lt/modding/crashes/"
CACHE_TTL = 12 * 60 * 60           # scrape HTML once every 12 h
EMBED_MODEL = "text-embedding-3-small"  # cheap + good enough
GPT_MODEL = "gpt-4o-mini"

DATA_DIR = Path(__file__).parent / ".cache"
DATA_DIR.mkdir(exist_ok=True)

class Section:
    __slots__ = ("title", "body", "anchor", "embedding")
    def __init__(self, title: str, body: str):
        self.title = title
        self.body = body.strip()
        self.anchor = BannerlordCrashes._anchor_from_title(title)
        self.embedding: List[float] | None = None

class BannerlordCrashes(commands.Cog):
    """
    AI-enhanced crash database look-up for Bannerlord.
    """

    def __init__(self, bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        self._sections: List[Section] = []
        self._cache_time = 0.0
        self.config = Config.get_conf(self, identifier=0xC0DED06)
        self.config.register_global(last_refresh=0.0)

    # -------------------------------------------------------- #
    # Assistant integration
    # -------------------------------------------------------- #
    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog: commands.Cog):
        await cog.register_function(
            cog_name="BannerlordCrashes",
            schema={
                "name": "search_crash_database",
                "description": (
                    "Look up a Mount & Blade II: Bannerlord crash / exception name "
                    "and return reason & solution from docs.bannerlordmodding.lt, if found. "
                    "Uses AI for fuzzy matching and extraction."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Crash name, exception, or free-form description"
                        }
                    },
                    "required": ["query"],
                },
            },
        )

    async def search_crash_database(self, query: str, *_, **__) -> dict:
        await self._ensure_index()

        # 1) semantic search
        query_vec = await self._embed_text(query)
        best, score = self._semantic_lookup(query_vec)

        if not best or score < 0.75:          # can tune this threshold
            return {
                "found": False,
                "result_text": (
                    "❌ I couldn’t confidently match that crash. "
                    "Try re-phrasing or give me the exact exception/stack-trace line."
                ),
            }

        # 2) extract reason / solution with GPT-4o (few-shot style prompt)
        reason, solution = await self._extract_with_llm(best)

        url = f"{CRASH_URL}#{best.anchor}"
        result_text = (
            f"**{best.title}**  \n"
            f"**Reason:** {reason or '―'}  \n"
            f"**Solution / Notes:** {solution or '―'}  \n"
            f"<{url}>"
        )

        return {
            "found": True,
            "title": best.title,
            "reason": reason or "",
            "solution": solution or "",
            "url": url,
            "result_text": result_text,
        }

    # -------------------------------------------------------- #
    # Owner / debug commands
    # -------------------------------------------------------- #
    @commands.is_owner()
    @commands.command(name="parsecrashes")
    async def force_refresh(self, ctx):
        await self._ensure_index(force=True)
        await ctx.send(f"Indexed {len(self._sections)} sections with embeddings ✔️")

    @commands.command(name="crashfix")
    async def crashfix_cmd(self, ctx, *, query: str):
        data = await self.search_crash_database(query)
        await ctx.send(data["result_text"][:2000])

    # -------------------------------------------------------- #
    # Index building: scrape → split → embed → disk-cache
    # -------------------------------------------------------- #
    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30))

    async def _ensure_index(self, force: bool = False):
        stale = (time.time() - self._cache_time) > CACHE_TTL
        if self._sections and not (force or stale):
            return

        # try loading cached json with embeddings
        cache_file = DATA_DIR / "sections.json"
        if not force and cache_file.exists():
            with cache_file.open("r", encoding="utf-8") as fp:
                raw = json.load(fp)
            self._sections = [Section(s["title"], s["body"]) for s in raw]
            for s, d in zip(self._sections, raw):
                s.anchor, s.embedding = d["anchor"], d["embedding"]
            self._cache_time = time.time()
            return

        # scrape html
        await self._ensure_session()
        async with self._session.get(CRASH_URL) as resp:
            html = await resp.text()

        soup = BeautifulSoup(html, "lxml")
        self._sections.clear()

        for header in soup.select("h2, h3"):
            title = header.get_text(strip=True)
            body_parts = []
            node = header.find_next_sibling()
            while node and node.name not in ("h2", "h3"):
                body_parts.append(node.get_text(" ", strip=True))
                node = node.find_next_sibling()
            body = "\n".join(body_parts)
            self._sections.append(Section(title, body))

        # compute embeddings in batches of 100 (rate-limit friendly)
        await self._embed_all_sections()

        # persist
        cache_file.write_text(json.dumps([
            {
                "title": s.title,
                "body": s.body,
                "anchor": s.anchor,
                "embedding": s.embedding,
            } for s in self._sections
        ], ensure_ascii=False), encoding="utf-8")

        self._cache_time = time.time()

    # ----------------- Embedding helpers ------------------- #
    async def _embed_all_sections(self):
        chunks = [self._sections[i:i+100] for i in range(0, len(self._sections), 100)]
        for chunk in chunks:
            texts = [s.title + "\n" + s.body for s in chunk]
            embeds = await openai.Embedding.async_create(
                model=EMBED_MODEL,
                input=texts
            )
            for s, e in zip(chunk, embeds.data):
                s.embedding = e.embedding

    async def _embed_text(self, text: str) -> List[float]:
        resp = await openai.Embedding.async_create(model=EMBED_MODEL, input=[text])
        return resp.data[0].embedding

    def _semantic_lookup(self, query_vec: List[float]) -> Tuple[Section | None, float]:
        def cosine(a: List[float], b: List[float]) -> float:
            dot = sum(x*y for x, y in zip(a, b))
            na = math.sqrt(sum(x*x for x in a))
            nb = math.sqrt(sum(y*y for y in b))
            return dot / (na*nb + 1e-6)

        best_sec, best_score = None, 0.0
        for sec in self._sections:
            score = cosine(query_vec, sec.embedding)
            if score > best_score:
                best_sec, best_score = sec, score
        return best_sec, best_score

    # ------------- GPT-4o extraction helper --------------- #
    async def _extract_with_llm(self, sec: Section) -> Tuple[str | None, str | None]:
        """
        Let GPT cleanly pull out reason / solution from the raw section text.
        Falls back to heuristic if model fails.
        """
        sys = (
            "You are an assistant that extracts the cause (reason) and the solution/fix from a "
            "Bannerlord crash note. If either field is missing, return an empty string."
        )
        prompt = (
            f"Crash-note text:\n\n{sec.body}\n\n"
            "Return JSON with keys reason and solution, no extra text."
        )
        try:
            resp = await openai.ChatCompletion.async_create(
                model=GPT_MODEL,
                messages=[{"role":"system","content":sys},
                          {"role":"user","content":prompt}],
                temperature=0.0,
            )
            import json as _json
            data = _json.loads(resp.choices[0].message.content)
            return data.get("reason"), data.get("solution")
        except Exception:
            # fallback heuristic
            return self._extract_reason_solution(sec.body)

    # ------------------ Misc utils ------------------------ #
    @staticmethod
    def _normalize(text: str) -> str:
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        return re.sub(r"\W+", "", text).lower()

    @staticmethod
    def _anchor_from_title(title: str) -> str:
        anchor = re.sub(r"[^\w\- ]", "", title).strip().lower()
        return re.sub(r"\s+", "-", anchor)

    @staticmethod
    def _extract_reason_solution(body: str) -> Tuple[str | None, str | None]:
        reason = solution = None
        for line in body.splitlines():
            if line.lower().startswith("reason"):
                reason = line.partition(":")[2].strip()
            elif line.lower().startswith("solution"):
                solution = line.partition(":")[2].strip()
        if not reason:
            reason = body.split(".")[0].strip()
        return reason, solution

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

# ---------- helper to pull key from env / Red config --------- #
def get_openai_key() -> str:
    # Option 1: environment variable
    if os.getenv("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    # Option 2: Red’s shared config (if you store it there)
    # from redbot.core import Config; cfg = Config.get_conf("OpenAIKey", 12345)
    # return await cfg.api_key()
    raise RuntimeError("Set OPENAI_API_KEY environment variable")
openai.api_key = get_openai_key()
