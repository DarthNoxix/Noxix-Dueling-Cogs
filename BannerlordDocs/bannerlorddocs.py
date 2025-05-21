from __future__ import annotations
import asyncio, re, os, time, json, html
from pathlib import Path
from typing import List, Tuple

import aiohttp, openai
from bs4 import BeautifulSoup, SoupStrainer, element as bs4element
from openai import AsyncOpenAI, RateLimitError
from rapidfuzz import fuzz
from redbot.core import commands

ROOT_URL   = "https://docs.bannerlordmodding.lt"
START_URL  = f"{ROOT_URL}/"
CACHE_TTL  = 12 * 60 * 60          # refresh every 12 h
MAX_PAGES  = 300                   # safety cap
CHUNK_CHARS = 8000                 # ≈ 2 k tokens for gpt-4o
PRESELECT  = 100                    # chunks passed to GPT
GPT_MODEL  = "gpt-4o-mini"

DATA_DIR = Path(__file__).parent / ".cache"
DATA_DIR.mkdir(exist_ok=True)


# ─── OpenAI-key helper ─────────────────────────────────────────────────
async def get_openai_client(bot, guild):
    api = None
    assistant = bot.get_cog("Assistant")
    if assistant and guild:
        api = assistant.db.get_conf(guild).api_key
    api = api or os.getenv("OPENAI_API_KEY")
    if not api:
        tok = await bot.get_shared_api_tokens("openai")
        api = tok.get("key") if tok else None
    if not api:
        raise RuntimeError("OpenAI key not found.")
    return AsyncOpenAI(api_key=api)


# ─── crawler / on-disk cache ───────────────────────────────────────────
class SiteCache:
    def __init__(self):
        self.index_file = DATA_DIR / "docs_index.json"
        self.pages: dict[str, str] = {}   # url → plain text
        self.last_fetch = 0.0

    def fresh(self):
        return self.pages and (time.time() - self.last_fetch) < CACHE_TTL

    async def load(self):
        if self.index_file.exists():
            raw = json.loads(self.index_file.read_text(encoding="utf-8"))
            self.pages = raw["pages"]
            self.last_fetch = raw["time"]

    def save(self):
        self.index_file.write_text(
            json.dumps({"pages": self.pages, "time": self.last_fetch}, ensure_ascii=False),
            encoding="utf-8",
        )

    async def crawl(self, session):
        self.pages.clear()
        queue = [START_URL]
        seen: set[str] = set()
        while queue and len(self.pages) < MAX_PAGES:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                async with session.get(url) as r:
                    if r.status != 200 or "text/html" not in r.headers.get("content-type", ""):
                        continue
                    html_text = await r.text()
            except Exception:
                continue

            # store plain-text body
            self.pages[url] = self.html_to_text(html_text)

            # collect same-site links
            soup = BeautifulSoup(html_text, "lxml", parse_only=SoupStrainer("a"))
            for link in soup:
                if not isinstance(link, bs4element.Tag):            # <-- skip Doctype etc.
                    continue
                href = link.get("href", "")
                if not href or href.startswith("#") or (
                    "://" in href and not href.startswith(ROOT_URL)
                ):
                    continue
                full = href if href.startswith(ROOT_URL) else ROOT_URL + href.lstrip("/")
                if full not in seen:
                    queue.append(full)

        self.last_fetch = time.time()
        self.save()

    @staticmethod
    def html_to_text(html_text: str) -> str:
        soup = BeautifulSoup(html_text, "lxml")
        for tag in soup.select("nav, header, footer, script, style"):
            tag.decompose()
        return soup.get_text("\n", strip=True)


# ─── main Cog ──────────────────────────────────────────────────────────
class BannerlordDocs(commands.Cog):
    """Answer any Bannerlord-modding question using docs.bannerlordmodding.lt."""

    def __init__(self, bot):
        self.bot = bot
        self.cache = SiteCache()
        self._session: aiohttp.ClientSession | None = None

    # Register function with Assistant
    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog: commands.Cog):
        await cog.register_function(
            cog_name="BannerlordDocs",
            schema={
                "name": "ask_modding_docs",
                "description": "Answer a Mount & Blade Bannerlord modding question using the docs site.",
                "parameters": {
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                },
            },
        )

    # Assistant-callable method
    async def ask_modding_docs(self, question: str, guild=None, *_, **__) -> dict:
        await self.ensure_cache()
        chunks: List[Tuple[str, str]] = []
        for url, text in self.cache.pages.items():
            for seg in self.segment(text):
                chunks.append((url, seg))

        # simple keyword scoring
        key_words = re.sub(r"\W+", " ", question.lower()).split()
        scored = []
        for url, seg in chunks:
            score = fuzz.token_set_ratio(question, seg)
            scored.append((score, url, seg))
        scored.sort(reverse=True)
        candidates = scored[:PRESELECT]

        client = await get_openai_client(self.bot, guild or (self.bot.guilds[0] if self.bot.guilds else None))
        sys_msg = (
            "Using ONLY the provided text, answer the question if possible.\n"
            'Respond JSON {"found":true,"answer":"...","excerpt":"..."} or {"found":false}.'
        )

        for _, url, seg in candidates:
            try:
                resp = await client.chat.completions.create(
                    model=GPT_MODEL,
                    messages=[
                        {"role": "system", "content": sys_msg},
                        {"role": "user", "content": f"QUESTION: {question}\n\nTEXT:\n{seg}"},
                    ],
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
            except RateLimitError:
                return {"found": False, "result_text": "❌ OpenAI rate-limited me; try later."}

            data = json.loads(resp.choices[0].message.content)
            if data.get("found"):
                answer  = html.unescape(data.get("answer", ""))
                excerpt = data.get("excerpt", "")
                return {
                    "found": True,
                    "result_text": f"**Answer:** {answer}\n\n*Source:* <{url}>\n\n> {excerpt}"
                }

        return {"found": False, "result_text": "❌ I couldn’t find an answer on the docs."}

    # manual crawl
    @commands.is_owner()
    @commands.command(name="parsedocs")
    async def parse_docs(self, ctx):
        await ctx.send("⏳ Crawling Bannerlord docs…")
        await self.ensure_cache(force=True)
        await ctx.send(f"Indexed {len(self.cache.pages)} pages ✔️")

    # helpers
    async def ensure_cache(self, force=False):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        await self.cache.load()
        if force or not self.cache.fresh():
            await self.cache.crawl(self._session)

    @staticmethod
    def segment(text: str) -> List[str]:
        parts, buff = [], []
        chars = 0
        for line in text.splitlines():
            buff.append(line)
            chars += len(line)
            if chars >= CHUNK_CHARS:
                parts.append("\n".join(buff))
                buff, chars = [], 0
        if buff:
            parts.append("\n".join(buff))
        return parts

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()
