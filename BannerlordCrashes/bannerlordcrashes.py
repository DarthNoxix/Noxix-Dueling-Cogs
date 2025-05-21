from __future__ import annotations

import json, math, os, re, time, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
from bs4 import BeautifulSoup
from redbot.core import commands, Config

# ---------------- OpenAI v1 client ---------------- #
# --- top of bannerlordcrashes.py, after `import os` and before we build the client ---
import openai
from openai import AsyncOpenAI

# grab the key from whichever place the Assistant already set it, or from env
_api_key = getattr(openai, "api_key", None) or os.getenv("OPENAI_API_KEY")
if not _api_key:
    raise RuntimeError("OPENAI_API_KEY not found – make sure the Assistant sets it or export it.")

openai_client = AsyncOpenAI(api_key=_api_key)
                 # api_key picked up from env/assistant

# -------------------------------------------------- #

CRASH_URL   = "https://docs.bannerlordmodding.lt/modding/crashes/"
CACHE_TTL   = 12 * 60 * 60            # re-scrape every 12 h
EMBED_MODEL = "text-embedding-3-small"
GPT_MODEL   = "gpt-4o-mini"

DATA_DIR = Path(__file__).parent / ".cache"
DATA_DIR.mkdir(exist_ok=True)


class Section:
    __slots__ = ("title", "body", "anchor", "embedding")

    def __init__(self, title: str, body: str):
        self.title: str = title
        self.body: str = body.strip()
        self.anchor: str = BannerlordCrashes._anchor_from_title(title)
        self.embedding: List[float] | None = None


class BannerlordCrashes(commands.Cog):
    """
    AI-powered Bannerlord crash lookup (OpenAI v1 client).
    """

    def __init__(self, bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        self._sections: List[Section] = []
        self._cache_ts: float = 0.0

        self.config = Config.get_conf(self, identifier=0xC0DED06)
        self.config.register_global(last_refresh=0.0)

    # ~~~~~~~~~~~~~~~~~ Assistant hook ~~~~~~~~~~~~~~~~ #
    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog: commands.Cog):
        await cog.register_function(
            cog_name="BannerlordCrashes",
            schema={
                "name": "search_crash_database",
                "description": (
                    "Look up a Mount & Blade II: Bannerlord crash/exception (fuzzy) and "
                    "return reason & solution from docs.bannerlordmodding.lt."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Crash name, exception, or short description"
                        }
                    },
                    "required": ["query"],
                },
            },
        )

    # -------------------------------------------------- #
    # Public (Assistant-callable) function
    # -------------------------------------------------- #
    async def search_crash_database(self, query: str, *_, **__) -> dict:
        await self._ensure_index()

        q_vec = await self._embed_text(query)
        sec, score = self._semantic_lookup(q_vec)

        if not sec or score < 0.75:
            return {
                "found": False,
                "result_text": (
                    "❌ I couldn’t confidently match that crash. "
                    "Please provide the exact exception or a longer snippet."
                )
            }

        reason, solution = await self._extract_with_llm(sec)
        url = f"{CRASH_URL}#{sec.anchor}"

        return {
            "found": True,
            "title": sec.title,
            "reason": reason or "",
            "solution": solution or "",
            "url": url,
            "result_text": (
                f"**{sec.title}**\n\n"
                f"**Reason:** {reason or '―'}\n"
                f"**Solution / Notes:** {solution or '―'}\n"
                f"<{url}>"
            ),
        }

    # -------------------------------------------------- #
    # Owner / convenience commands
    # -------------------------------------------------- #
    @commands.is_owner()
    @commands.command(name="parsecrashes")
    async def force_refresh(self, ctx):
        await self._ensure_index(force=True)
        await ctx.send(f"Indexed {len(self._sections)} sections with embeddings ✔️")

    @commands.command(name="crashfix")
    async def crashfix_cmd(self, ctx, *, query: str):
        data = await self.search_crash_database(query)
        await ctx.send(data["result_text"][:2000])

    # -------------------------------------------------- #
    # Index building (scrape → embed → cache)
    # -------------------------------------------------- #
    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30))

    async def _ensure_index(self, force: bool = False):
        stale = (time.time() - self._cache_ts) > CACHE_TTL
        if self._sections and not (force or stale):
            return

        cache_file = DATA_DIR / "sections.json"
        if not force and cache_file.exists():
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
            self._sections = [Section(d["title"], d["body"]) for d in raw]
            for sec, d in zip(self._sections, raw):
                sec.anchor, sec.embedding = d["anchor"], d["embedding"]
            self._cache_ts = time.time()
            return

        # scrape fresh HTML
        await self._ensure_session()
        async with self._session.get(CRASH_URL) as resp:
            html = await resp.text()

        soup = BeautifulSoup(html, "lxml")
        self._sections.clear()

        for hdr in soup.select("h2, h3"):
            title = hdr.get_text(strip=True)
            parts = []
            node = hdr.find_next_sibling()
            while node and node.name not in ("h2", "h3"):
                parts.append(node.get_text(" ", strip=True))
                node = node.find_next_sibling()
            self._sections.append(Section(title, "\n".join(parts)))

        await self._embed_all_sections()

        cache_file.write_text(json.dumps([
            {
                "title": s.title,
                "body": s.body,
                "anchor": s.anchor,
                "embedding": s.embedding,
            } for s in self._sections
        ], ensure_ascii=False), encoding="utf-8")
        self._cache_ts = time.time()

    # ---------------- Embeddings (v1) ---------------- #
    async def _embed_all_sections(self):
        batch = 100
        for i in range(0, len(self._sections), batch):
            chunk = self._sections[i:i+batch]
            texts = [s.title + "\n" + s.body for s in chunk]
            resp = await openai_client.embeddings.create(
                model=EMBED_MODEL,
                input=texts,
            )
            for s, e in zip(chunk, resp.data):
                s.embedding = e.embedding

    async def _embed_text(self, text: str) -> List[float]:
        resp = await openai_client.embeddings.create(
            model=EMBED_MODEL, input=[text])
        return resp.data[0].embedding

    def _semantic_lookup(self, q: List[float]) -> Tuple[Optional[Section], float]:
        def cos(a, b):
            dot = sum(x*y for x, y in zip(a, b))
            na = math.sqrt(sum(x*x for x in a)); nb = math.sqrt(sum(y*y for y in b))
            return dot / (na*nb + 1e-6)

        best, score = None, 0.0
        for sec in self._sections:
            s = cos(q, sec.embedding)
            if s > score:
                best, score = sec, s
        return best, score

    # --------------- GPT-4o extraction --------------- #
    async def _extract_with_llm(self, sec: Section) -> Tuple[str | None, str | None]:
        sys = ("Extract the crash ‘reason’ (cause) and the ‘solution’ (fix) from the "
               "following text. If absent, leave the field empty and return JSON only.")
        prompt = f"```text\n{sec.body}\n```"
        try:
            resp = await openai_client.chat.completions.create(
                model=GPT_MODEL,
                messages=[
                    {"role": "system", "content": sys},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            return data.get("reason"), data.get("solution")
        except Exception:
            # fallback heuristic
            return self._extract_reason_solution(sec.body)

    # ---------------- Utility helpers ---------------- #
    @staticmethod
    def _anchor_from_title(title: str) -> str:
        return re.sub(r"\s+", "-", re.sub(r"[^\w\- ]", "", title).strip().lower())

    @staticmethod
    def _extract_reason_solution(body: str) -> Tuple[str | None, str | None]:
        reason = solution = None
        for line in body.splitlines():
            l = line.lower()
            if l.startswith("reason"):
                reason = line.partition(":")[2].strip()
            elif l.startswith("solution"):
                solution = line.partition(":")[2].strip()
        if not reason:
            reason = body.split(".")[0].strip()
        return reason, solution

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()
