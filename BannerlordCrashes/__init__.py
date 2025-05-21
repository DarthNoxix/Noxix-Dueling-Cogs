from .bannerlordcrashes import BannerlordCrashes

async def setup(bot):
    await bot.add_cog(BannerlordCrashes(bot))
