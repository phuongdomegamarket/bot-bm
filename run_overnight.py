"""Local launcher for the overnight production run.

Streamlit 1.42 on Windows swaps the asyncio policy to
WindowsSelectorEventLoopPolicy inside ``_fix_tornado_platform_bug`` (called
early in ``bootstrap.run``); installing a new policy clears the thread's
current event loop, so a loop created before that point disappears and
``bootstrap.run`` crashes at ``asyncio.get_event_loop()`` — Python 3.14 has
no implicit loop creation any more.  Fix: install the Selector policy
ourselves BEFORE streamlit runs, so streamlit's swap is a no-op and the loop
created here survives into ``bootstrap.run``.
"""
import asyncio
import sys

from asyncio import WindowsSelectorEventLoopPolicy

asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())
asyncio.set_event_loop(asyncio.new_event_loop())

sys.argv = ["streamlit", "run", "main.py", "--server.headless", "true"]

from streamlit.web import cli as stcli  # noqa: E402

stcli.main()
