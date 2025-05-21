from __future__ import annotations
import asyncio, os, re, time, json
from pathlib import Path
from typing import List, Tuple, Optional

import aiohttp, openai
from bs4 import BeautifulSoup
from openai import AsyncOpenAI, RateLimitError
from redbot.core import commands

CRASH_URL   = "https://docs.bannerlordmodding.lt/modding/crashes/"
CACHE_TTL   = 12 * 60 * 60
GPT_MODEL   = "gpt-4o-mini"
CHUNK_TOKENS = 2000               # ≈8 k chars – safe for 4-o

# ─── key helper ─────────────────────────────────────────────────────────
async def get_openai_client(bot, guild):
    # 1) Assistant per-guild key
    api = None
    assistant = bot.get_cog("Assistant")
    if assistant and guild:
        api = assistant.db.get_conf(guild).api_key
    # 2) env var
    api = api or os.getenv("OPENAI_API_KEY")
    # 3) shared token
    if not api:
        tok = await bot.get_shared_api_tokens("openai")
        api = tok.get("key") if tok else None
    if not api:
        raise RuntimeError("No OpenAI key found.")
    return AsyncOpenAI(api_key=api)

# ─── simple cache ───────────────────────────────────────────────────────
_cache_path = Path(__file__).parent / ".fullpage.html"
_cache_at   = 0.0

async def fetch_page(session) -> str:
    global _cache_at
    if _cache_path.exists() and (time.time() - _cache_at) < CACHE_TTL:
        return _cache_path.read_text(encoding="utf-8")
    async with session.get(CRASH_URL) as r:
        html = await r.text()
    _cache_path.write_text(html, encoding="utf-8")
    _cache_at = time.time()
    return html

def chunk_text(text: str) -> List[str]:
    # naïve split by paragraphs to ~CHUNK_TOKENS tokens (≈4 chars per token)
    approx = CHUNK_TOKENS * 4
    parts, buff = [], []
    count = 0
    for para in text.split("\n"):
        buff.append(para)
        count += len(para)
        if count >= approx:
            parts.append("\n".join(buff))
            buff, count = [], 0
    if buff:
        parts.append("\n".join(buff))
    return parts

# ─── cog ────────────────────────────────────────────────────────────────
class BannerlordCrashes(commands.Cog):
    """Look up Bannerlord crashes by letting GPT search the page live."""

    def __init__(self, bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None

    # assistant integration
    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog: commands.Cog):
        await cog.register_function(
            cog_name="BannerlordCrashes",
            schema={
                "name": "search_crash_database",
                "description": "Find reason & solution for a Bannerlord crash by name.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        )

    async def search_crash_database(self, query: str, guild=None, *_, **__) -> dict:
        # 1. make sure we have html
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        html = await fetch_page(self._session)

        # 2. plain-text
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)

        # 3. split
        chunks = chunk_text(text)

        client = await get_openai_client(self.bot, guild or (self.bot.guilds[0] if self.bot.guilds else None))

        sys_msg = (
            "You are a helper. The user asks for a Bannerlord crash. "
            "If this chunk contains the crash they're asking, respond JSON "
            '{ "found": true, "reason": "...", "solution": "..." } . '
            "If not, respond { \"found\": false } . "
            "Only output JSON."
        )

        for chunk in chunks:
            try:
                resp = await client.chat.completions.create(
                    model=GPT_MODEL,
                    messages=[
                        {"role": "system", "content": sys_msg},
                        {"role": "user", "content": f"Crash query: {query}\n\n---\n{chunk}"},
                    ],
                    temperature=0.0,
                    timeout=30,
                    response_format={"type": "json_object"},
                )
            except RateLimitError:
                return {
                    "found": False,
                    "result_text": "❌ OpenAI rate-limited me. Try again later."
                }

            data = json.loads(resp.choices[0].message.content)
            if data.get("found"):
                reason = data.get("reason", "")
                solution = data.get("solution", "")
                return {
                    "found": True,
                    "title": query,
                    "reason": reason,
                    "solution": solution,
                    "url": CRASH_URL,
                    "result_text": f"**{query}**\n\n**Reason:** {reason or '―'}\n**Solution:** {solution or '―'}"
                }

        return {
            "found": False,
            "result_text": "❌ I couldn’t find that crash on the page."
        }

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()
