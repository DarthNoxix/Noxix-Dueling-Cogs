from __future__ import annotations

import json, math, os, re, time, unicodedata
from pathlib import Path
from typing import List, Optional, Tuple

import aiohttp
from bs4 import BeautifulSoup
from redbot.core import commands, Config

# ─── OpenAI v1 lazy-init ──────────────────────────────────────────────────────
import openai
from openai import AsyncOpenAI

_openai_client: AsyncOpenAI | None = None
def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        key = getattr(openai, "api_key", None) or os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set.")
        _openai_client = AsyncOpenAI(api_key=key)
    return _openai_client
# ──────────────────────────────────────────────────────────────────────────────

CRASH_URL   = "https://docs.bannerlordmodding.lt/modding/crashes/"
CACHE_TTL   = 12 * 60 * 60
EMBED_MODEL = "text-embedding-3-small"
GPT_MODEL   = "gpt-4o-mini"

DATA_DIR = Path(__file__).parent / ".cache"
DATA_DIR.mkdir(exist_ok=True)

class Section:
    __slots__ = ("title", "body", "anchor", "embedding")
    def __init__(self, title: str, body: str):
        self.title = title
        self.body  = body.strip()
        self.anchor = BannerlordCrashes._anchor_from_title(title)
        self.embedding: List[float] | None = None

class BannerlordCrashes(commands.Cog):
    """AI-powered Bannerlord crash lookup."""

    def __init__(self, bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        self._sections: List[Section] = []
        self._cache_ts = 0.0
        self.config = Config.get_conf(self, identifier=0xC0DED06)
        self.config.register_global(last_refresh=0.0)

    # Assistant-callable function registration
    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog: commands.Cog):
        await cog.register_function(
            cog_name="BannerlordCrashes",
            schema={
                "name": "search_crash_database",
                "description": "Fuzzy search Bannerlord crash DB and return reason & solution.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Crash name/description"}
                    },
                    "required": ["query"],
                },
            },
        )

    # Assistant-exposed search
    async def search_crash_database(self, query: str, *_, **__) -> dict:
        await self._ensure_index()
        q_vec = await self._embed_text(query)
        sec, score = self._semantic_lookup(q_vec)

        if not sec or score < 0.75:
            return {
                "found": False,
                "result_text": "❌ No confident match. Provide the exact exception or more context."
            }

        reason, solution = await self._extract_with_llm(sec)
        url = f"{CRASH_URL}#{sec.anchor}"
        return {
            "found": True,
            "title": sec.title,
            "reason": reason or "",
            "solution": solution or "",
            "url": url,
            "result_text": f"**{sec.title}**\n\n**Reason:** {reason or '―'}\n"
                           f"**Solution / Notes:** {solution or '―'}\n<{url}>",
        }

    # Owner helper commands
    @commands.is_owner()
    @commands.command(name="parsecrashes")
    async def _force_refresh(self, ctx):
        await self._ensure_index(force=True)
        await ctx.send(f"Indexed {len(self._sections)} sections ✔️")

    @commands.command(name="crashfix")
    async def _crashfix(self, ctx, *, query: str):
        data = await self.search_crash_database(query)
        await ctx.send(data["result_text"][:2000])

    # Index build / refresh
    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def _ensure_index(self, force=False):
        stale = (time.time() - self._cache_ts) > CACHE_TTL
        if self._sections and not (force or stale):
            return
        cache_file = DATA_DIR / "sections.json"
        if not force and cache_file.exists():
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
            self._sections = [Section(d["title"], d["body"]) for d in raw]
            for s, d in zip(self._sections, raw):
                s.anchor, s.embedding = d["anchor"], d["embedding"]
            self._cache_ts = time.time()
            return
        await self._scrape_and_embed()
        self._cache_ts = time.time()
        cache_file.write_text(json.dumps([
            {"title":s.title,"body":s.body,"anchor":s.anchor,"embedding":s.embedding}
            for s in self._sections
        ], ensure_ascii=False), encoding="utf-8")

    async def _scrape_and_embed(self):
        await self._ensure_session()
        async with self._session.get(CRASH_URL) as r:
            soup = BeautifulSoup(await r.text(), "lxml")
        self._sections.clear()
        for h in soup.select("h2, h3"):
            title = h.get_text(strip=True)
            parts, n = [], h.find_next_sibling()
            while n and n.name not in ("h2", "h3"):
                parts.append(n.get_text(" ", strip=True))
                n = n.find_next_sibling()
            self._sections.append(Section(title, "\n".join(parts)))
        await self._embed_all_sections()

    # Embeddings
    async def _embed_all_sections(self):
        batch = 100
        for i in range(0, len(self._sections), batch):
            texts = [s.title+"\n"+s.body for s in self._sections[i:i+batch]]
            resp = await _get_openai_client().embeddings.create(
                model=EMBED_MODEL, input=texts)
            for s, e in zip(self._sections[i:i+batch], resp.data):
                s.embedding = e.embedding

    async def _embed_text(self, text: str) -> List[float]:
        resp = await _get_openai_client().embeddings.create(
            model=EMBED_MODEL, input=[text])
        return resp.data[0].embedding

    def _semantic_lookup(self, q: List[float]) -> Tuple[Optional[Section], float]:
        def cos(a,b):
            dot=sum(x*y for x,y in zip(a,b))
            na=math.sqrt(sum(x*x for x in a)); nb=math.sqrt(sum(y*y for y in b))
            return dot/(na*nb+1e-6)
        best, score = None, 0.0
        for s in self._sections:
            sc = cos(q, s.embedding)
            if sc > score:
                best, score = s, sc
        return best, score

    # GPT-4o extraction
    async def _extract_with_llm(self, sec: Section) -> Tuple[str|None, str|None]:
        sys = ("Extract the crash reason (cause) and solution (fix) as JSON keys "
               "`reason` and `solution`. Leave empty strings if absent.")
        try:
            resp = await _get_openai_client().chat.completions.create(
                model=GPT_MODEL,
                messages=[{"role":"system","content":sys},
                          {"role":"user","content":sec.body}],
                temperature=0.0,
                response_format={"type":"json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            return data.get("reason"), data.get("solution")
        except Exception:
            return self._heuristic_extract(sec.body)

    @staticmethod
    def _heuristic_extract(body: str) -> Tuple[str|None,str|None]:
        reason=solution=None
        for line in body.splitlines():
            l=line.lower()
            if l.startswith("reason"):
                reason=line.partition(":")[2].strip()
            elif l.startswith("solution"):
                solution=line.partition(":")[2].strip()
        if not reason:
            reason = body.split(".")[0].strip()
        return reason, solution

    @staticmethod
    def _anchor_from_title(t: str) -> str:
        return re.sub(r"\s+","-",re.sub(r"[^\w\- ]","",t).strip().lower())

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()
