from .welcomeembed import WelcomeEmbed


async def setup(bot):
    await bot.add_cog(WelcomeEmbed(bot))
