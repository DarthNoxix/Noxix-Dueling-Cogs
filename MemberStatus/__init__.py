from .memberstatus import MemberStatus

async def setup(bot):
    await bot.add_cog(MemberStatus(bot))
