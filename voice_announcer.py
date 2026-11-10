"""Optional voice announcements of MB Bank transactions.

When enabled (``VOICE_CHANNEL`` env var set to a voice channel name, e.g.
``voices``), every announced transaction is spoken into that channel:

- the bank's own voice sentence when the inbox row carried one
  (``NotificationEvent.voice_text`` -> ``Transaction.addDescription``),
- otherwise a sentence synthesised from the amount
  ("Đã nhận thành công năm mươi nghìn đồng").

Design notes
------------
* gTTS (Google Translate TTS, ``lang='vi'``) does the synthesis - it only
  needs internet access and produced correct Vietnamese audio in testing,
  while edge-tts was rejected by Microsoft with HTTP 403.
* Playback needs an ``ffmpeg`` binary (discord.py FFmpegPCMAudio; ffmpeg only
  decodes to raw PCM - discord.py itself encodes Opus through its libopus
  binding, the same path music bots use).  The Streamlit Cloud image has
  both; locally ffmpeg is optional - without it the announcer logs a clear
  error and the tracker keeps running.
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

# discord.py's client.run() only configures the "discord.*" loggers (its
# setup_logging uses root_logger=False by default), so this module's INFO
# lines would otherwise never reach the console on Streamlit Cloud.  Attach
# our own handler so diagnostics like "voice connected: ... (UDP OK)" print.
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    log.addHandler(_handler)
    log.setLevel(logging.INFO)

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
            prefix = "Đã nhận"
        elif debit not in ("", "0"):
            amount = int(float(debit))
            prefix = "Đã chuyển đi"
        else:
            return None
        if spoken:
            # the bank's sentence starts with "Đã nhận thành công"; drop
            # "thành công" the same way as the synthesised ones (user
            # preference), and shorten "Việt Nam Đồng"
            return (
                spoken.replace("thành công ", "")
                .replace("Việt Nam Đồng", "đồng")
            )
        words = amount_to_vietnamese(amount)
        if currency.upper() == "VND":
            return f"{prefix} {words} đồng"
        return f"{prefix} {words} {currency}"
    except Exception:
        return None


def synthesize_cash_sound(path: str, ticks: int = 7, rate: int = 44100) -> str:
    """Write a short "counting money" sound as a WAV file.

    A train of paper-like ticks (short filtered noise bursts with a metallic
    ring) resembling a banknote being counted - synthesised directly into a
    16-bit mono WAV so no audio asset file has to ship with the bot and the
    cloud image needs nothing beyond stdlib ``wave``/``math``/``random``.
    """
    import math
    import random
    import struct
    import wave

    random.seed(20261110)  # stable sound across runs
    tick_len = int(0.028 * rate)  # ~28 ms per tick
    gap = int(0.075 * rate)  # ~75 ms between ticks -> ~0.7 s total
    total = ticks * (tick_len + gap) + int(0.05 * rate)
    samples = bytearray(total * 2)

    for t in range(ticks):
        start = t * (tick_len + gap)
        # each tick slightly different pitch -> natural "riffle" feel
        ring = 2600 + random.randint(-300, 300)
        for i in range(tick_len):
            env = math.exp(-i / (tick_len / 4.5))  # fast attack/decay
            noise = random.uniform(-1.0, 1.0)
            tone = math.sin(2 * math.pi * ring * i / rate)
            # mostly noise (paper) + a little metallic ring
            value = 0.72 * noise * env + 0.28 * tone * env
            frame = int(value * 32767 * 0.55)
            offset = (start + i) * 2
            samples[offset] = frame & 0xFF
            samples[offset + 1] = (frame >> 8) & 0xFF

    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(samples))
    return path


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
        # Discord channel names can carry odd whitespace (the guild's voice
        # channel is literally "voice\ntransactions"); normalise every run of
        # whitespace to a single space on both sides before comparing.
        def _norm(value: str) -> str:
            return " ".join(value.split()).lower()

        name = _norm(self.channel_name)
        for channel in self.guild.voice_channels:
            if _norm(channel.name) == name:
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

            def _play(source: "discord.AudioSource") -> None:
                play_error.clear()
                source = discord.PCMVolumeTransformer(source)
                vc.play(source, after=_after)

            # Cash-counting pre-roll (a short synthetic tick train) before
            # the spoken sentence: signals "money incoming" while gTTS is
            # still synthesising, cutting perceived latency.
            pre = os.path.join(tempfile.gettempdir(), f"mb_cash_{os.getpid()}.wav")
            try:
                await asyncio.to_thread(synthesize_cash_sound, pre)
                _play(discord.FFmpegPCMAudio(pre))
                await done.wait()
                if play_error:
                    log.warning("voice pre-roll error: %s", play_error[0])
                await asyncio.sleep(0.15)  # small gap before speech
            except Exception as error:
                log.warning("voice pre-roll failed: %s", error)

            # PCM path: ffmpeg only decodes to s16le, discord.py encodes Opus
            # itself through its own libopus binding. The previous
            # FFmpegOpusAudio route let the cloud ffmpeg encode and parse Ogg
            # packets, and the produced stream was never heard in the channel
            # even though ffmpeg exited 0 and the UDP link was healthy.
            _play(discord.FFmpegPCMAudio(path))
            await done.wait()
            if play_error:
                log.warning("voice playback error: %s", play_error[0])
            else:
                log.info("voice played x%s: latency=%s", text[:40], getattr(vc, "average_latency", "?"))
        finally:
            for stale in (path,):
                try:
                    os.remove(stale)
                except OSError:
                    pass


import discord  # noqa: E402  (used only inside _speak)


async def create_from_env(guild: Any) -> Optional[VoiceAnnouncer]:
    """Build an announcer when VOICE_CHANNEL is configured, else None."""
    channel_name = (os.environ.get("VOICE_CHANNEL") or "").strip()
    if not channel_name:
        return None
    return VoiceAnnouncer(guild, channel_name, enabled=True)
