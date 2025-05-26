from .supportmanager import SupportManager


async def setup(bot):
    await bot.add_cog(SupportManager(bot))
