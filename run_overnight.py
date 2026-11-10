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
import os
import shutil
import sys

from asyncio import WindowsSelectorEventLoopPolicy

asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())
asyncio.set_event_loop(asyncio.new_event_loop())

# Voice TTS needs an ffmpeg binary on PATH for discord.FFmpegPCMAudio.
# This machine has none, but imageio_ffmpeg ships a static one - put its
# directory on PATH before discord.py is imported.  Harmless if the
# package is missing (cloud installs real ffmpeg via packages.txt).
if not shutil.which("ffmpeg"):
    try:
        import imageio_ffmpeg

        # The shipped binary is named ffmpeg-win-x86_64-v7.1.exe, which
        # which("ffmpeg") and subprocess("ffmpeg") never find - copy it
        # under the expected name into a temp dir first.
        target_dir = os.path.join(
            os.environ.get("TEMP", os.path.expanduser("~")), "mb_ffmpeg"
        )
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, "ffmpeg.exe")
        if not os.path.isfile(target):
            shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), target)
        os.environ["PATH"] = target_dir + os.pathsep + os.environ["PATH"]
    except Exception:
        pass

sys.argv = ["streamlit", "run", "main.py", "--server.headless", "true"]

from streamlit.web import cli as stcli  # noqa: E402

stcli.main()
