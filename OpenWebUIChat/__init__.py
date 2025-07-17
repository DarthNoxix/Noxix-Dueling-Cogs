from .openwebuichat import OpenWebUIChat


async def setup(bot):
    await bot.add_cog(OpenWebUIChat(bot))
