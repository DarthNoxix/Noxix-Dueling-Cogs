from .bannerlorddocs import bannerlorddocs

async def setup(bot):
    await bot.add_cog(BannerlordDocs(bot))
