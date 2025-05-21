from __future__ import annotations

import json, math, os, re, time, unicodedata
from pathlib import Path
from typing import List, Optional, Tuple

import aiohttp, openai
from bs4 import BeautifulSoup
from openai import AsyncOpenAI, RateLimitError, BadRequestError
from redbot.core import commands, Config

CRASH_URL   = "https://docs.bannerlordmodding.lt/modding/crashes/"
CACHE_TTL   = 12 * 60 * 60
EMBED_MODEL = "text-embedding-3-small"
GPT_MODEL   = "gpt-4o-mini"

DATA_DIR = Path(__file__).parent / ".cache"
DATA_DIR.mkdir(exist_ok=True)

_openai_client: AsyncOpenAI | None = None


async def _get_openai_client(bot, guild) -> AsyncOpenAI:
    global _openai_client
    if _openai_client:
        return _openai_client

    # 1) Assistant per-guild key
    api_key = None
    assistant = bot.get_cog("Assistant")
    if assistant and guild:
        api_key = assistant.db.get_conf(guild).api_key or None

    # 2) Env var
    api_key = api_key or os.getenv("OPENAI_API_KEY")

    # 3) Shared tokens
    if not api_key:
        tokens = await bot.get_shared_api_tokens("openai")
        api_key = tokens.get("key") if tokens else None

    if not api_key:
        raise RuntimeError(
            "OpenAI key not found. "
            "Set one with `[p]assist openaikey`, an env var, or `[p]set api openai key`."
        )

    _openai_client = AsyncOpenAI(api_key=api_key)
    return _openai_client


class Section:
    __slots__ = ("title", "body", "anchor", "embedding")
    def __init__(self, title: str, body: str):
        self.title, self.body = title, body.strip()
        self.anchor = BannerlordCrashes._anchor_from_title(title)
        self.embedding: List[float] | None = None


class BannerlordCrashes(commands.Cog):
    """AI-powered Bannerlord crash lookup (quota-safe)."""

    def __init__(self, bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        self._sections: List[Section] = []
        self._cache_ts = 0.0
        self.config = Config.get_conf(self, identifier=0xC0DED06)
        self.config.register_global(last_refresh=0.0)

    # ───────────────── Assistant registration ─────────────
    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog: commands.Cog):
        await cog.register_function(
            cog_name="BannerlordCrashes",
            schema={
                "name": "search_crash_database",
                "description": "Find reason & solution for a Bannerlord crash (fuzzy).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Crash name / snippet"}
                    },
                    "required": ["query"],
                },
            },
        )

    # ───────────────── Assistant-callable ──────────────────
    async def search_crash_database(self, query: str, *_, **__) -> dict:
        await self._ensure_index()

        try:
            q_vec = await self._embed_text(query)
        except RateLimitError:
            q_vec = None

        sec, score = self._semantic_lookup(q_vec, query)

        if not sec or score < 0.75:
            return {
                "found": False,
                "result_text": (
                    "❌ I couldn’t confidently match that crash. "
                    "Try the exact exception or give more detail."
                ),
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

    # ───────────────── Owner helper ────────────────────────
    @commands.is_owner()
    @commands.command(name="parsecrashes")
    async def force_refresh(self, ctx):
        await self._ensure_index(force=True)
        await ctx.send(f"Indexed {len(self._sections)} sections ✔️")

    @commands.command(name="crashfix")
    async def crashfix_cmd(self, ctx, *, query: str):
        data = await self.search_crash_database(query)
        await ctx.send(data["result_text"][:2000])

    # ───────────────── Index building ──────────────────────
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
        cache_file.write_text(json.dumps([
            {"title": s.title, "body": s.body, "anchor": s.anchor, "embedding": s.embedding}
            for s in self._sections
        ], ensure_ascii=False), encoding="utf-8")
        self._cache_ts = time.time()

    async def _scrape_and_embed(self):
        await self._ensure_session()
        async with self._session.get(CRASH_URL) as r:
            html = await r.text()
        soup = BeautifulSoup(html, "lxml")
        self._sections = []
        for h in soup.select("h2, h3"):
            title = h.get_text(strip=True)
            parts, n = [], h.find_next_sibling()
            while n and n.name not in ("h2", "h3"):
                parts.append(n.get_text(" ", strip=True))
                n = n.find_next_sibling()
            self._sections.append(Section(title, "\n".join(parts)))
        await self._embed_all_sections()

    # ───────────────── Embeddings ──────────────────────────
    async def _embed_all_sections(self):
        client = await _get_openai_client(self.bot, self.bot.guilds[0] if self.bot.guilds else None)
        try:
            for i in range(0, len(self._sections), 100):
                chunk = self._sections[i:i+100]
                texts = [s.title+"\n"+s.body for s in chunk]
                resp = await client.embeddings.create(model=EMBED_MODEL, input=texts)
                for s, e in zip(chunk, resp.data):
                    s.embedding = e.embedding
        except RateLimitError:
            for s in self._sections:
                s.embedding = None
            self.bot.logger.warning("Embeddings quota exhausted – using substring fallback.")

    async def _embed_text(self, text: str) -> List[float]:
        client = await _get_openai_client(self.bot, self.bot.guilds[0] if self.bot.guilds else None)
        resp = await client.embeddings.create(model=EMBED_MODEL, input=[text])
        return resp.data[0].embedding

    # ───────────────── Search logic ───────────────────────
    def _semantic_lookup(self, q_vec: Optional[List[float]], raw: str):
        if q_vec and self._sections and self._sections[0].embedding is not None:
            def cos(a,b):
                dot=sum(x*y for x,y in zip(a,b))
                na=math.sqrt(sum(x*x for x in a)); nb=math.sqrt(sum(y*y for y in b))
                return dot/(na*nb+1e-6)
            best=None; score=0.0
            for s in self._sections:
                sc=cos(q_vec,s.embedding)
                if sc>score:
                    best,score=s,sc
            return best,score
        norm=self._normalize(raw)
        for s in self._sections:
            if norm in self._normalize(s.title):
                return s,1.0
            if norm in self._normalize(s.body):
                return s,0.9
        return None,0.0

    # ───────────────── GPT extraction ─────────────────────
    async def _extract_with_llm(self, sec: Section):
        try:
            client = await _get_openai_client(self.bot, self.bot.guilds[0] if self.bot.guilds else None)
            sys = "Extract crash reason and solution as JSON {\"reason\":\"\",\"solution\":\"\"}."
            resp = await client.chat.completions.create(
                model=GPT_MODEL,
                messages=[{"role":"system","content":sys},
                          {"role":"user","content":sec.body}],
                temperature=0.0,
                response_format={"type":"json_object"},
            )
            data=json.loads(resp.choices[0].message.content)
            return data.get("reason"),data.get("solution")
        except Exception:
            return self._heuristic(sec.body)

    @staticmethod
    def _heuristic(body):
        reason=solution=None
        for line in body.splitlines():
            l=line.lower()
            if l.startswith("reason"):
                reason=line.partition(":")[2].strip()
            elif l.startswith("solution"):
                solution=line.partition(":")[2].strip()
        if not reason:
            reason=body.split(".")[0].strip()
        return reason,solution

    # ───────────────── Utils ──────────────────────────────
    @staticmethod
    def _normalize(txt):
        return re.sub(r"\W+","",unicodedata.normalize("NFKD",txt).encode("ascii","ignore").decode()).lower()
    @staticmethod
    def _anchor_from_title(t):
        return re.sub(r"\s+","-",re.sub(r"[^\w\- ]","",t).strip().lower())
    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()
