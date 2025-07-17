from .openwebuichat import OpenWebUIMemoryBot


async def setup(bot):
    await bot.add_cog(OpenWebUIMemoryBot(bot))
