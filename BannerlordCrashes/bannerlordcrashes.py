from __future__ import annotations

import re, time, unicodedata
from typing import Dict, Optional, Tuple

import aiohttp
from bs4 import BeautifulSoup
from redbot.core import commands, Config

CRASH_URL = "https://docs.bannerlordmodding.lt/modding/crashes/"

class BannerlordCrashes(commands.Cog):
    """
    Scrapes docs.bannerlordmodding.lt/modding/crashes
    and exposes `search_crash_database` for the Assistant.
    """

    def __init__(self, bot):
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Tuple[str, str]] = {}   # {normalized_title: (title, body)}
        self._cache_time: float = 0
        self.config = Config.get_conf(self, identifier=0xC0DED06)
        self.config.register_global(last_refresh=0.0)

    # ------------------------------------------------------------------ #
    # Assistant integration
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog: commands.Cog):
        await cog.register_function(
            cog_name="BannerlordCrashes",
            schema={
                "name": "search_crash_database",
                "description": (
                    "Look up a Mount & Blade II: Bannerlord crash/exception name and "
                    "return reason & solution from docs.bannerlordmodding.lt, if found."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Crash name or exception, e.g. 'GetBodyProperties'"
                        }
                    },
                    "required": ["query"],
                },
            },
        )

    async def search_crash_database(self, query: str, *_, **__) -> dict:
        await self._ensure_cache(refresh_if_stale=True)
        key = self._normalize(query)
        hit = self._cache.get(key)

        if not hit:                                       # fallback substring search
            for norm_title, data in self._cache.items():
                if key in norm_title:
                    hit = data
                    break

        if not hit:
            return {"found": False, "result_text": f"❌ No entry for “{query}”."}

        title, body = hit
        reason, solution = self._extract_reason_solution(body)
        result_text = (
            f"**{title}**\n\n"
            f"**Reason:** {reason or '―'}\n"
            f"**Solution / Notes:** {solution or '―'}\n"
            f"<{CRASH_URL}#{self._anchor_from_title(title)}>"
        )
        return {
            "found": True,
            "title": title,
            "reason": reason or "",
            "solution": solution or "",
            "url": CRASH_URL + f"#{self._anchor_from_title(title)}",
            "result_text": result_text,
        }

    # ------------------------------------------------------------------ #
    # Owner / convenience commands
    # ------------------------------------------------------------------ #
    @commands.command(name="parsecrashes")
    @commands.is_owner()
    async def _force_parse(self, ctx):
        await self._ensure_cache(force=True)
        await ctx.send(f"Indexed {len(self._cache)} crash entries.")

    @commands.command(name="crashfix")
    async def _crash_fix_lookup(self, ctx, *, query: str):
        data = await self.search_crash_database(query)
        await ctx.send(data["result_text"][:2000])

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def _ensure_cache(self, force=False, refresh_if_stale=False):
        stale = (time.time() - self._cache_time) > 12 * 60 * 60
        if force or (refresh_if_stale and stale) or not self._cache:
            await self._build_cache()
            self._cache_time = time.time()
            await self.config.last_refresh.set(self._cache_time)

    async def _build_cache(self):
        await self._ensure_session()
        async with self._session.get(CRASH_URL) as resp:
            html = await resp.text()

        soup = BeautifulSoup(html, "lxml")
        self._cache.clear()

        for header in soup.select("h2, h3"):
            title = header.get_text(strip=True)
            body_nodes = []
            node = header.find_next_sibling()
            while node and node.name not in ("h2", "h3"):
                body_nodes.append(node.get_text(" ", strip=True))
                node = node.find_next_sibling()
            body_text = "\n".join(body_nodes)

            # index the whole section by its header
            self._cache[self._normalize(title)] = (title, body_text)

            # additionally index bullet-style crash lines
            for line in body_text.splitlines():
                m = re.match(r"^([\w\.]+)\s+\-\s+.+", line)
                if m:
                    crash_name = m.group(1)
                    norm = self._normalize(crash_name)
                    # store entire section body so reason/solution parsing still works
                    self._cache.setdefault(norm, (crash_name, body_text))

    @staticmethod
    def _normalize(text: str) -> str:
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        return re.sub(r"[^\w]+", "", text).lower()

    @staticmethod
    def _anchor_from_title(title: str) -> str:
        anchor = re.sub(r"[^\w\- ]", "", title).strip().lower()
        return re.sub(r"\s+", "-", anchor)

    @staticmethod
    def _extract_reason_solution(body: str) -> Tuple[str | None, str | None]:
        reason, solution = None, None
        for line in body.splitlines():
            lower = line.lower()
            if lower.startswith("reason"):
                reason = line.partition(":")[2].strip()
            elif lower.startswith("solution"):
                solution = line.partition(":")[2].strip()
        if not reason:
            reason = body.split(".")[0].strip()
        return reason, solution

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()
