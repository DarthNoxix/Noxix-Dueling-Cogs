from .supportmanager import setup as _real_setup

async def setup(bot):
    # Red looks here first; just forward the call.
    await _real_setup(bot)
