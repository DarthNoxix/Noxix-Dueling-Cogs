from .embedcopier import EmbedCopier


async def setup(bot):
    await bot.add_cog(EmbedCopier(bot))
