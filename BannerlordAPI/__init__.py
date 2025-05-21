from .bannerlordapi import BannerlordAPI

async def setup(bot):
    await bot.add_cog(BannerlordAPI(bot))
