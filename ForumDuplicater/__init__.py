from .forumduplicator import ForumDuplicator


async def setup(bot):
    await bot.add_cog(ForumDuplicator(bot))
