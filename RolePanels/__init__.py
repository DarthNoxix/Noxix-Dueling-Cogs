from .rolepanels import RolePanels


async def setup(bot):
    """Red-bot entry-point."""
    await bot.add_cog(RolePanels(bot))
