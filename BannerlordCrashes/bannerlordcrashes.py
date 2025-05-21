from __future__ import annotations

import asyncio, re, time, unicodedata
from typing import Dict, Optional, Tuple

import aiohttp
from bs4 import BeautifulSoup
from redbot.core import commands, Config

CRASH_URL = "https://docs.bannerlordmodding.lt/modding/crashes/"

class BannerlordCrashes(commands.Cog):
    """
    Cog that scrapes docs.bannerlordmodding.lt/modding/crashes
    and exposes an Assistant-callable function `search_crash_database`.
    """

    def __init__(self, bot):
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Tuple[str, str]] = {}      # {normalized_title: (title, full_text)}
        self._cache_time: float = 0                       # unix ts of last refresh
        # allow manual refresh every 12 h via command
        self.config = Config.get_conf(self, identifier=0xC0DED06)
        self.config.register_global(last_refresh=0.0)

    # ---------------------------------------------------------------------- #
    # Assistant integration
    # ---------------------------------------------------------------------- #
    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog: commands.Cog):
        """Register Assistant-callable search function."""
        await cog.register_function(
            cog_name="BannerlordCrashes",
            schema={
                "name": "search_crash_database",
                "description": (
                    "Look up a Mount & Blade II: Bannerlord crash/exception name and return the "
                    "reason and solution from docs.bannerlordmodding.lt if present."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Crash name or exception (e.g. 'NullReferenceException')"
                        }
                    },
                    "required": ["query"],
                },
            },
        )

    async def search_crash_database(self, query: str, *_, **__) -> dict:
        """
        Assistant-callable method.
        :param query: crash / exception name supplied by the Assistant.
        :return: dict with keys: found (bool), title, reason, solution, url
        """
        await self._ensure_cache(refresh_if_stale=True)
        key = self._normalize(query)
        # naive exact-match first, then substring search
        hit = self._cache.get(key)
        if not hit:
            for norm_title, data in self._cache.items():
                if key in norm_title:
                    hit = data
                    break
        if not hit:
            return {"found": False}

        title, body = hit
        reason, solution = self._extract_reason_solution(body)
        return {
            "found": True,
            "title": title,
            "reason": reason or "",
            "solution": solution or "",
            "url": CRASH_URL + f"#{self._anchor_from_title(title)}"
        }

    # ---------------------------------------------------------------------- #
    # Discord-side quality-of-life commands (optional)
    # ---------------------------------------------------------------------- #
    @commands.command(name="parsecrashes")
    @commands.is_owner()
    async def force_parse(self, ctx):
        """Force-refresh the local crash database."""
        await self._ensure_cache(force=True)
        await ctx.send(f"Crash database parsed. {len(self._cache)} entries cached.")

    @commands.command(name="crashfix")
    async def crash_fix_lookup(self, ctx, *, query: str):
        """Look up a crash/exception reason & fix."""
        data = await self.search_crash_database(query)
        if not data["found"]:
            await ctx.send("❌ Couldn’t find that crash in the database.")
            return

        embed = (
            f"**{data['title']}**\n\n"
            f"**Reason:** {data['reason'] or '—'}\n"
            f"**Solution / Notes:** {data['solution'] or '—'}\n\n"
            f"<{data['url']}>"
        )
        await ctx.send(embed[:2000])

    # ---------------------------------------------------------------------- #
    # Core scraping / parsing helpers
    # ---------------------------------------------------------------------- #
    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def _ensure_cache(self, force: bool = False, refresh_if_stale: bool = False):
        stale = (time.time() - self._cache_time) > 12 * 60 * 60  # 12 h TTL
        if force or (refresh_if_stale and stale) or not self._cache:
            await self._build_cache()
            self._cache_time = time.time()
            await self.config.last_refresh.set(self._cache_time)

    async def _build_cache(self):
        await self._ensure_session()
        async with self._session.get(CRASH_URL) as resp:
            html = await resp.text(encoding="utf-8", errors="ignore")

        soup = BeautifulSoup(html, "lxml")
        self._cache.clear()

        for header in soup.select("h2, h3"):
            title = header.get_text(strip=True)
            norm = self._normalize(title)
            # grab everything until the next h2/h3
            parts = []
            node = header.find_next_sibling()
            while node and node.name not in ("h2", "h3"):
                parts.append(node.get_text(" ", strip=True))
                node = node.find_next_sibling()
            body = "\n".join(parts)
            self._cache[norm] = (title, body)

    @staticmethod
    def _normalize(text: str) -> str:
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        return re.sub(r"[^\w]+", "", text).lower()

    @staticmethod
    def _anchor_from_title(title: str) -> str:
        # replicate MkDocs/GitHub-style id generation
        anchor = re.sub(r"[^\w\- ]", "", title).strip().lower()
        anchor = re.sub(r"[\s]+", "-", anchor)
        return anchor

    @staticmethod
    def _extract_reason_solution(body: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Heuristically pull out 'REASON:' (and sometimes 'Solution:') lines.
        """
        reason, solution = None, None
        for line in body.splitlines():
            if line.lower().startswith("reason"):
                reason = line.partition(":")[2].strip()
            elif line.lower().startswith("solution"):
                solution = line.partition(":")[2].strip()
        # fallback: split first sentence(s)
        if not reason:
            reason = body.split(".")[0].strip()
        return reason, solution

    # ------------------------------------------------------------------ #
    # Cog cleanup
    # ------------------------------------------------------------------ #
    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()
