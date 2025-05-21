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
        self._cache: Dict[str, Tuple[str, str]] = {}   # {normalized_title: (title, full_text)}
        self._cache_time: float = 0                    # unix ts of last refresh
        self.config = Config.get_conf(self, identifier=0xC0DED06)
        self.config.register_global(last_refresh=0.0)

    # ------------------------------------------------------------------ #
    # Assistant integration
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog: commands.Cog):
        """Register the function with the Assistant."""
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

    # ------------------------- Assistant callable ---------------------- #
    async def search_crash_database(self, query: str, *_, **__) -> dict:
        """
        Assistant-callable method.

        Returns a dict **MUST** containing `result_text` so the
        Assistant wrapper can pass it straight back to the user.
        """
        await self._ensure_cache(refresh_if_stale=True)
        key = self._normalize(query)

        # exact-match first, then substring search
        hit = self._cache.get(key) or next(
            (data for norm, data in self._cache.items() if key in norm), None
        )

        if not hit:
            return {
                "found": False,
                "result_text": f"❌ I couldn’t find a crash called **{query}** in the Bannerlord database."
            }

        title, body = hit
        reason, solution = self._extract_reason_solution(body)
        url = CRASH_URL + f"#{self._anchor_from_title(title)}"

        summary = (
            f"**{title}**\n"
            f"**Reason:** {reason or '—'}\n"
            f"**Solution / Notes:** {solution or '—'}\n"
            f"<{url}>"
        )

        return {
            "found": True,
            "title": title,
            "reason": reason or "",
            "solution": solution or "",
            "url": url,
            "result_text": summary
        }

    # ------------------------------------------------------------------ #
    # Discord helper commands (optional)
    # ------------------------------------------------------------------ #
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
        await ctx.send(data["result_text"][:2000])

    # ------------------------------------------------------------------ #
    # Core scraping / parsing helpers
    # ------------------------------------------------------------------ #
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

            # everything until the next h2/h3
            parts = []
            node = header.find_next_sibling()
            while node and node.name not in ("h2", "h3"):
                parts.append(node.get_text(" ", strip=True))
                node = node.find_next_sibling()

            self._cache[norm] = (title, "\n".join(parts))

    # ------------------------------------------------------------------ #
    # Utility helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize(text: str) -> str:
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        return re.sub(r"[^\w]+", "", text).lower()

    @staticmethod
    def _anchor_from_title(title: str) -> str:
        anchor = re.sub(r"[^\w\- ]", "", title).strip().lower()
        return re.sub(r"[\s]+", "-", anchor)

    @staticmethod
    def _extract_reason_solution(body: str) -> Tuple[Optional[str], Optional[str]]:
        reason = solution = None
        for line in body.splitlines():
            if line.lower().startswith("reason"):
                reason = line.partition(":")[2].strip()
            elif line.lower().startswith("solution"):
                solution = line.partition(":")[2].strip()
        if not reason:
            reason = body.split(".")[0].strip()
        return reason, solution

    # ------------------------------------------------------------------ #
    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()
