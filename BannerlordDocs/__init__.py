# BannerlordDocs/__init__.py
from .bannerlorddocs import BannerlordDocs          # ← import the class, exact case

async def setup(bot):
    await bot.add_cog(BannerlordDocs(bot))
