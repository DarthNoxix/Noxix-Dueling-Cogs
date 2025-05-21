from __future__ import annotations
import re, os, time, json, html
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urljoin

import aiohttp, openai
from bs4 import BeautifulSoup, SoupStrainer, element as bs4element
from openai import AsyncOpenAI, RateLimitError
from rapidfuzz import fuzz
from redbot.core import commands

ROOT_URL    = "https://apidoc.bannerlord.com/v/1.2.12"
CACHE_TTL   = 24 * 60 * 60         # API pages change rarely
MAX_PAGES   = 600                  # ~500 html files total
CHUNK_CHARS = 60_000               # one class page fits easily
PRESELECT   = 120                  # send more chunks (API is terse)
GPT_MODEL   = "gpt-4o"             # same model as docs cog

DATA_DIR = Path(__file__).parent / ".cache"
DATA_DIR.mkdir(exist_ok=True)


# ─── OpenAI helper ──────────────────────────────────────────────────────
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


# ─── crawler / cache ────────────────────────────────────────────────────
class SiteCache:
    def __init__(self):
        self.file = DATA_DIR / "api_index.json"
        self.pages: dict[str, str] = {}
        self.stamp = 0.0

    def fresh(self):
        return self.pages and (time.time() - self.stamp) < CACHE_TTL

    async def load(self):
        if self.file.exists():
            data = json.loads(self.file.read_text(encoding="utf-8"))
            self.pages, self.stamp = data["pages"], data["time"]

    def save(self):
        self.file.write_text(
            json.dumps({"pages": self.pages, "time": self.stamp}, ensure_ascii=False),
            encoding="utf-8",
        )

    async def crawl(self, session):
        queue = [ROOT_URL + "/index.html"]
        seen: set[str] = set()
        self.pages.clear()

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

            self.pages[url] = self.clean_text(html_text)

            soup = BeautifulSoup(html_text, "lxml", parse_only=SoupStrainer("a"))
            for link in soup:
                if not isinstance(link, bs4element.Tag):
                    continue
                href = link.get("href", "")
                if not href or href.startswith("#"):
                    continue
                # stay within the /v/1.2.12/ subtree, follow only .html files
                full = urljoin(url, href)
                if not full.startswith(ROOT_URL) or not full.endswith(".html"):
                    continue
                if full not in seen:
                    queue.append(full)

        self.stamp = time.time()
        self.save()

    @staticmethod
    def clean_text(html_text: str) -> str:
        soup = BeautifulSoup(html_text, "lxml")
        for junk in soup.select("script, style, nav, footer, header"):
            junk.decompose()
        return soup.get_text("\n", strip=True)


# ─── main Cog ───────────────────────────────────────────────────────────
class BannerlordAPI(commands.Cog):
    """Query the Bannerlord 1.2.12 Doxygen API documentation."""

    def __init__(self, bot):
        self.bot = bot
        self.cache = SiteCache()
        self._session: aiohttp.ClientSession | None = None

    # Assistant registration
    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog):
        await cog.register_function(
            cog_name="BannerlordAPI",
            schema={
                "name": "ask_api_docs",
                "description": "Answer a coding-level question using Bannerlord 1.2.12 API documentation.",
                "parameters": {
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                },
            },
        )

    # Assistant-callable
    async def ask_api_docs(self, question: str, guild=None, *_, **__) -> dict:
        await self.ensure_cache()

        chunks: List[Tuple[str, str]] = []
        for url, txt in self.cache.pages.items():
            for seg in self.segment(txt):
                chunks.append((url, seg))

        words = re.sub(r"\W+", " ", question.lower()).split()
        scored = []
        for url, seg in chunks:
            score = fuzz.token_set_ratio(question, seg)
            scored.append((score, url, seg))
        must = [(999, url, seg) for url, seg in chunks if all(w in seg.lower() for w in words)]
        scored.sort(reverse=True)
        candidates = (must + scored)[:PRESELECT]

        client = await get_openai_client(self.bot, guild or (self.bot.guilds[0] if self.bot.guilds else None))
        sys = ("Using ONLY the provided API text, answer the question. "
               'JSON response: {"found":true,"answer":"...","excerpt":"..."} or {"found":false}')

        for _, url, seg in candidates:
            try:
                r = await client.chat.completions.create(
                    model=GPT_MODEL,
                    messages=[
                        {"role": "system", "content": sys},
                        {"role": "user", "content": f"QUESTION: {question}\n\nTEXT:\n{seg}"},
                    ],
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
            except RateLimitError:
                return {"found": False,
                        "result_text": "❌ OpenAI rate-limited me; try again soon."}

            data = json.loads(r.choices[0].message.content)
            if data.get("found"):
                answer  = html.unescape(data.get("answer", ""))
                excerpt = data.get("excerpt", "")
                return {
                    "found": True,
                    "result_text": f"**Answer:** {answer}\n\n*Source:* <{url}>\n\n> {excerpt}"
                }

        return {"found": False,
                "result_text": "❌ I couldn’t locate that in the 1.2.12 API docs."}

    # owner command
    @commands.is_owner()
    @commands.command(name="parseapi")
    async def parse_api_cmd(self, ctx):
        await ctx.send("⏳ Crawling Bannerlord API docs…")
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
    def segment(txt: str) -> List[str]:
        parts, buff, chars = [], [], 0
        for line in txt.splitlines():
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
