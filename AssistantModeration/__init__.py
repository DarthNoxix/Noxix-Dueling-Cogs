from .alicentmoderation import AlicentModeration

async def setup(bot):
    await bot.add_cog(AlicentModeration(bot))