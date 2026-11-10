"""Optional voice announcements of MB Bank transactions.

When enabled (``VOICE_CHANNEL`` env var set to a voice channel name, e.g.
``voices``), every announced transaction is spoken into that channel:

- the bank's own voice sentence when the inbox row carried one
  (``NotificationEvent.voice_text`` -> ``Transaction.addDescription``),
- otherwise a sentence synthesised from the amount
  ("Đã nhận thành công năm mươi nghìn Việt Nam Đồng").

Design notes
------------
* gTTS (Google Translate TTS, ``lang='vi'``) does the synthesis - it only
  needs internet access and produced correct Vietnamese audio in testing,
  while edge-tts was rejected by Microsoft with HTTP 403.
* Playback needs an ``ffmpeg`` binary (discord.py FFmpegOpusAudio).  The
  Streamlit Cloud image has one; locally it is optional - without ffmpeg the
  announcer logs a clear error and the tracker keeps running.
* Announcements are serialised through an :class:`asyncio.Queue` so two
  transactions landing in the same poll never talk over each other.
* Every failure is swallowed and logged: voice must never take the tracker
  or the Discord thread posting down.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any, Optional

log = logging.getLogger(__name__)

# Amount -> Vietnamese words (up to billions, enough for bank transfers).
_UNITS = [
    "", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín",
]
_SCALES = ["", "nghìn", "triệu", "tỷ"]


def _three_digits_to_words(n: int, full: bool) -> str:
    """Words for 0..999.

    ``full`` (a non-leading group, e.g. the 025 of 4,025,000) pads leading
    zeros: "không trăm hai mươi lăm".  The 'lẻ' separator is inserted
    whenever a hundreds digit is followed by a single unit, padded or not.
    """
    hundreds, rest = divmod(n, 100)
    parts: list[str] = []
    if hundreds:
        parts.append(_UNITS[hundreds] + " trăm")
    elif full:
        parts.append("không trăm")
    if rest == 0:
        return " ".join(parts)
    tens, units = divmod(rest, 10)
    if tens == 0:
        if hundreds or full:
            parts.append("lẻ")
        parts.append(_UNITS[units])
    elif tens == 1:
        if units == 5:
            parts.append("mười lăm")
        elif units:
            parts.append("mười " + _UNITS[units])
        else:
            parts.append("mười")
    else:
        word = _UNITS[tens] + " mươi"
        if units == 1:
            word += " mốt"
        elif units == 4:
            word += " tư"
        elif units == 5:
            word += " lăm"
        elif units:
            word += " " + _UNITS[units]
        parts.append(word)
    return " ".join(parts)


def amount_to_vietnamese(amount: int) -> str:
    """4025000 -> "bốn triệu không trăm hai mươi lăm nghìn"."""
    if amount < 0:
        return "âm " + amount_to_vietnamese(-amount)
    if amount == 0:
        return "không"
    if amount < 1000:
        return _three_digits_to_words(amount, full=False)
    groups: list[int] = []
    while amount:
        groups.append(amount % 1000)
        amount //= 1000
    top = len(groups) - 1
    words: list[str] = []
    for i in range(top, -1, -1):
        if groups[i] == 0:
            continue
        # the leading group reads plainly ("bốn"), lower groups are padded
        # so 4,025,000 reads "... không trăm hai mươi lăm nghìn"
        words.append(_three_digits_to_words(groups[i], full=(i != top)))
        words.append(_SCALES[i])
    return " ".join(w for w in words if w).strip()


def voice_text_for(transaction: Any) -> Optional[str]:
    """Build the sentence to speak for one transaction, or None to skip."""
    try:
        # the bank's own sentence (VOICE-type inbox rows)
        spoken = (getattr(transaction, "addDescription", "") or "").strip()
        credit = (getattr(transaction, "creditAmount", "") or "").strip()
        debit = (getattr(transaction, "debitAmount", "") or "").strip()
        currency = (getattr(transaction, "currency", "") or "VND").strip()
        if credit not in ("", "0"):
            amount = int(float(credit))
            prefix = "Đã nhận thành công"
        elif debit not in ("", "0"):
            amount = int(float(debit))
            prefix = "Đã chuyển đi"
        else:
            return None
        if spoken:
            return spoken
        words = amount_to_vietnamese(amount)
        if currency.upper() == "VND":
            return f"{prefix} {words} Việt Nam Đồng"
        return f"{prefix} {words} {currency}"
    except Exception:
        return None


class VoiceAnnouncer:
    """Speak transactions into a named voice channel of one guild."""

    def __init__(self, guild: Any, channel_name: str, enabled: bool = True):
        self.guild = guild
        self.channel_name = channel_name
        self.enabled = enabled
        self._queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        # Startup backlog (restarted bot re-announcing days-old
        # transactions) must not blast through the speaker before anyone
        # has joined; only transactions arriving after this moment speak.
        self._live_only = True

    def start(self) -> None:
        if not self.enabled or self._worker is not None:
            return
        self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        vc = self.guild.voice_client
        if vc:
            await vc.disconnect(force=True)

    def announce(self, text: str) -> None:
        """Queue one sentence (never blocks, never raises)."""
        if not self.enabled or not text:
            return
        if self._live_only:
            # Dropped until the first "warm" transaction flows through;
            # the backlog re-announcements right after startup stay mute.
            return
        try:
            self._queue.put_nowait(text)
        except Exception:
            pass

    def go_live(self) -> None:
        """Enable speaking after the startup backlog has been drained."""
        self._live_only = False

    async def _run(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self._speak(text)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning("voice announce failed: %s", error)

    async def _find_channel(self) -> Optional[Any]:
        name = self.channel_name.lower()
        for channel in self.guild.voice_channels:
            if channel.name.lower() == name:
                return channel
        return None

    async def _ensure_connected(self) -> Optional[Any]:
        vc = self.guild.voice_client
        if vc and vc.is_connected():
            return vc
        channel = await self._find_channel()
        if channel is None:
            log.warning("voice channel %r not found", self.channel_name)
            return None
        vc = await channel.connect(self_deaf=True)
        # The UDP socket needs a couple of heartbeats before latency is
        # known; inf latency means audio packets cannot reach Discord
        # (blocked UDP - e.g. Streamlit Cloud) and playback stays silent.
        latency = float("inf")
        # Discord's voice heartbeat runs every ~5 s, so the first real
        # latency reading can take that long to arrive; 10 s keeps the
        # "UDP blocked" verdict from firing on a healthy link.
        for _ in range(20):
            value = getattr(vc, "average_latency", float("inf"))
            if isinstance(value, float) and 0 < value < float("inf"):
                latency = value
                break
            await asyncio.sleep(0.5)
        log.info(
            "voice connected: channel=%s latency=%s (%s)",
            channel.name,
            latency,
            "UDP OK" if latency < float("inf") else "UDP bị chặn - không phát được âm thanh",
        )
        return vc

    async def _speak(self, text: str) -> None:
        import gtts

        def synthesize() -> str:
            path = os.path.join(
                tempfile.gettempdir(), f"mb_tts_{os.getpid()}_{id(text)}.mp3"
            )
            gtts.gTTS(text, lang="vi").save(path)
            return path

        path = await asyncio.to_thread(synthesize)
        try:
            vc = await self._ensure_connected()
            if vc is None:
                return

            done = asyncio.Event()
            play_error: list = []

            def _after(error: Optional[Exception]) -> None:
                if error is not None:
                    play_error.append(error)
                done.set()

            source = discord.FFmpegOpusAudio(path)
            vc.play(source, after=_after)
            await done.wait()
            if play_error:
                log.warning("voice playback error: %s", play_error[0])
            else:
                log.info("voice played x%s: latency=%s", text[:40], getattr(vc, "average_latency", "?"))
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


import discord  # noqa: E402  (used only inside _speak)


async def create_from_env(guild: Any) -> Optional[VoiceAnnouncer]:
    """Build an announcer when VOICE_CHANNEL is configured, else None."""
    channel_name = (os.environ.get("VOICE_CHANNEL") or "").strip()
    if not channel_name:
        return None
    return VoiceAnnouncer(guild, channel_name, enabled=True)
