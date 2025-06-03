"""
Loader for the SupportManager cog + its slash-command façade.
Just put this file next to supportmanager.py.
"""

from .supportmanager import setup as _real_setup  # re-export setup()

# Red will import this file and call setup(bot)
async def setup(bot):
    # Delegate to the setup() that lives in supportmanager.py
    await _real_setup(bot)
