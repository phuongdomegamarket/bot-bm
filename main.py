import asyncio

# Streamlit 1.42 bootstrap calls asyncio.get_event_loop() in MainThread;
# Python 3.14 removed the implicit loop creation, so create it explicitly.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import threading
import json
import locale
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import aiohttp
import discord
import requests
import streamlit as st
from discord import app_commands
from discord.ext import commands, tasks
from discord.utils import get
from dotenv import load_dotenv

import bm_lib
import server
from bm_tracker import MBTracker, thread_ref_no
from guild import *

# `override=True` is required on Windows: os.environ is case-insensitive and
# USERNAME is already set to the Windows account, so load_dotenv() would skip
# our .env "username" entry and the bot would log into MB as the Windows user.
load_dotenv(override=True)
if "log_queue" not in st.session_state:
    st.session_state["log_queue"] = queue.Queue()

if "logs" not in st.session_state:
    st.session_state["logs"] = []

if "task_running" not in st.session_state:
    st.session_state["task_running"] = False
processed_thread = set()
pause_watching = False


def myStyle(log_queue):
    MY_GUILD = discord.Object(id=1236962671824211998)  # replace with your guild id

    class MyClient(discord.Client):
        # Suppress error on the User attribute being None since it fills up later
        user: discord.ClientUser

        def __init__(self, *, intents: discord.Intents):
            super().__init__(intents=intents)
            # A CommandTree is a special type that holds all the application command
            # state required to make it work. This is a separate class because it
            # allows all the extra state to be opt-in.
            # Whenever you want to work with application commands, your tree is used
            # to store and work with them.
            # Note: When using commands.Bot instead of discord.Client, the bot will
            # maintain its own tree instead.
            self.tree = app_commands.CommandTree(self)

        # In this basic example, we just synchronize the app commands to one guild.
        # Instead of specifying a guild to every command, we copy over our global commands instead.
        # By doing so, we don't have to wait up to an hour until they are shown to the end-user.
        async def setup_hook(self):
            # This copies the global commands over to your guild.
            self.tree.copy_global_to(guild=MY_GUILD)
            await self.tree.sync(guild=MY_GUILD)

    intents = discord.Intents.default()
    client = MyClient(intents=intents)
    log_queue.put(("info", "Starting process data..."))
    # intents = discord.Intents.default()
    # client = discord.Client(intents=intents)
    # tree = app_commands.CommandTree(client)
    HEADERS = []
    THREADS = []
    USERNAMES = []
    USERNAME = os.environ.get("username")
    PASSWORD = os.environ.get("password")
    ACCOUNT_NO = os.environ.get("account_no")
    MAIN_CHANNEL = os.environ.get("main_channel")
    RESULT = None

    mb = None
    tracker = None
    tracker_task = None
    SEEN_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".seen_tx.json")

    def correctSingleQuoteJSON(s):
        rstr = ""
        escaped = False

        for c in s:
            if c == "'" and not escaped:
                c = '"'  # replace single with double quote

            elif c == "'" and escaped:
                rstr = rstr[:-1]  # remove escape character before single quotes

            elif c == '"':
                c = "\\" + c  # escape existing double quotes

            escaped = c == "\\"  # check for an escape character
            rstr += c  # append the correct json

        return rstr

    INFO = False

    @client.event
    async def on_ready():
        global INFO, mb, tracker, tracker_task
        try:
            req = requests.get("http://localhost:8888")
            log_queue.put(
                (
                    "info",
                    f"Phát hiện instance khác đang chạy (status {req.status_code}), dừng instance này",
                )
            )
            await client.close()
            return
        except Exception as error:
            print(error)
            server.b()
            for guild in client.guilds:
                if guild.name.lower() == "phượng đỏ mega":
                    RESULT = await getBasic(guild)
                    tracker = MBTracker(
                        username=USERNAME,
                        password=PASSWORD,
                        account_no=ACCOUNT_NO,
                        seen_store_path=SEEN_STORE,
                        log=lambda level, message: log_queue.put((level, message)),
                        log_file=os.path.join(
                            os.path.dirname(os.path.abspath(SEEN_STORE)),
                            "tracker.log",
                        ),
                    )
                    try:
                        await tracker.start()
                    except Exception as start_error:
                        log_queue.put(
                            ("error", f"Không đăng nhập được MB: {start_error}")
                        )
                        continue
                    await seed_seen_from_threads(guild)
                    tracker_task = asyncio.create_task(
                        tracker.run_forever(handle_new_transactions)
                    )
                    if not getTransMb.is_running():
                        getTransMb.start(guild)
                    log_queue.put(("info", "Đã bắt đầu theo dõi giao dịch realtime"))

    async def seed_seen_from_threads(guild):
        """Record transactions already posted as Discord threads.

        Keeps a restart from re-announcing everything that happened while the
        bot was down.

        The thread name is built as
            "<sign> <amount> <currency>/ <timestamp>/ <refNo>/ <accountNo>"
        The timestamp and the account number are pure digits while the refNo
        always carries letters, so the refNo is the first segment after the
        header that contains a letter.  Picking "the first numeric part"
        would grab the timestamp instead - that key never matches a real
        transaction and the dedupe silently does nothing.
        """
        if tracker is None:
            return
        try:
            for channel in guild.channels:
                if channel.name != MAIN_CHANNEL:
                    continue
                keys = []
                threads = list(channel.threads)
                archived = [t async for t in channel.archived_threads()]
                for thread in threads + archived:
                    ref_no = thread_ref_no(thread.name)
                    if ref_no:
                        keys.append(f"refNo:{ref_no}")
                if keys:
                    tracker.seed_seen(keys)
                    log_queue.put(
                        ("info", f"Đã đánh dấu {len(keys)} giao dịch cũ từ thread")
                    )
        except Exception as error:
            log_queue.put(("error", f"Không seed được thread cũ: {error}"))

    async def handle_new_transactions(transactions, result):
        """Create one Discord thread per newly seen transaction."""
        guild = None
        for g in client.guilds:
            if g.name.lower() == "phượng đỏ mega":
                guild = g
                break
        if guild is None:
            return
        basic = None
        for channel in guild.channels:
            if channel.name == MAIN_CHANNEL:
                basic = channel
        if basic is None:
            return

        for transaction in transactions:
            try:
                await post_transaction(basic, transaction)
            except Exception as error:
                log_queue.put(("error", f"Lỗi tạo thread: {error}"))

    async def post_transaction(basic, transaction):
        currency = transaction.currency
        amount = (
            transaction.creditAmount
            if transaction.creditAmount != "0"
            else transaction.debitAmount
        )
        amount = f"{int(amount):,}"
        sign = "+" if transaction.creditAmount != "0" else "-"
        description = transaction.description
        transactionAt = transaction.transactionDate
        try:
            timestamp = str(
                datetime.strptime(transactionAt, "%d/%m/%Y %H:%M:%S").timestamp()
                * 1000
            ).split(".")[0]
        except ValueError:
            timestamp = "0"
        threadName = f"{sign} {amount} {currency}/ {timestamp}/ {transaction.refNo}/ {transaction.accountNo}"

        applied_tags = []
        string = ""
        if sign == "+":
            for tag in basic.available_tags:
                if "in" in tag.name.lower() or "chuyển đến" in tag.name.lower():
                    applied_tags.append(tag)
        else:
            for tag in basic.available_tags:
                if "out" in tag.name.lower() or "chuyển đi" in tag.name.lower():
                    applied_tags.append(tag)
            string += (
                "\nTới ngân hàng: **"
                + (transaction.bankName or "")
                + "**\nSố tài khoản: **"
                + (transaction.benAccountNo or "")
                + "**\nChủ tài khoản: **"
                + (transaction.benAccountName or "")
                + "**"
            )
        try:
            balance = f"{int(transaction.availableBalance):,}"
        except (TypeError, ValueError):
            balance = str(transaction.availableBalance)
        thread = await basic.create_thread(
            name=threadName,
            content="\nSố tiền: **"
            + amount
            + " "
            + currency
            + "**\nNội dung: **"
            + description
            + "**\nThời điểm: **"
            + transaction.transactionDate.split(" ")[1]
            + "** ngày **"
            + transaction.transactionDate.split(" ")[0]
            + "**"
            + string
            + "\nSố dư hiện tại: **"
            + balance
            + " "
            + currency
            + "**\n@everyone",
            applied_tags=applied_tags,
        )
        if thread and tracker is not None:
            tracker.mark_seen(transaction)
            log_queue.put(
                ("success", f"Đã đăng giao dịch {sign} {amount} {currency}")
            )

    @tasks.loop(seconds=2)
    async def getTransMb(guild):
        """Status heartbeat for the Streamlit panel.

        The actual polling now lives in bm_tracker (persistent session,
        adaptive interval, change detection). This loop only reports health.
        """
        global tracker, tracker_task
        if tracker is None:
            return
        # Watchdog: the poll loop swallows every ordinary exception, so the
        # only way it can die is CancelledError - which is exactly how the
        # overnight run of 2026-11-09 lost its tracker at 23:24 while the
        # Discord gateway kept running until 05:13. Revive the task here.
        if tracker_task is not None and tracker_task.done():
            error = tracker_task.exception()
            log_queue.put(
                (
                    "error",
                    "Task theo dõi đã chết: "
                    + (f"{error}" if error else "bị hủy (CancelledError)"),
                )
            )
            tracker_task = asyncio.create_task(
                tracker.run_forever(handle_new_transactions)
            )
            log_queue.put(("info", "Đã hồi sinh task theo dõi giao dịch"))
        stats = tracker.stats
        extra = f" err={stats['last_error']}" if stats.get("last_error") else ""
        log_queue.put(
            (
                "info",
                f"polls={stats['polls']} tx={stats['transactions']} "
                f"inbox={stats['inbox_rows']} errors={stats['errors']} "
                f"latency={stats['last_latency_ms']}ms interval={stats['interval']}s "
                f"balance={stats.get('balance')}{extra}",
            )
        )

    @client.tree.command()
    async def stop_instance(interaction: discord.Interaction):
        global pause_watching, tracker
        pause_watching = True
        if tracker is not None:
            tracker.paused = True
        await interaction.response.send_message(f"{client.user.name} is paused")

    @client.tree.command()
    @app_commands.describe(
        bot_id="Confirm bot ID to pause watching transactions",
    )
    async def pause_watching_transactions(
        interaction: discord.Interaction, bot_id: str
    ):
        global pause_watching, tracker
        if int(bot_id) == client.user.id:
            pause_watching = True
            if tracker is not None:
                tracker.paused = True
            await interaction.response.send_message(f"{client.user.name} is paused")
        else:
            await interaction.response.send_message(f"Invalid bot ID")

    @client.tree.command()
    async def resume_watching_transactions(interaction: discord.Interaction):
        global pause_watching, tracker
        if pause_watching:
            pause_watching = False
            if tracker is not None:
                tracker.paused = False
            await interaction.response.send_message(f"{client.user.name} is resumed")

    client.run(os.environ.get("botToken"))


def run_bot_forever(log_queue):
    """Watchdog: chạy myStyle trong loop, tự restart nếu thread chết vì lỗi."""
    retry_count = 0
    while True:
        try:
            log_queue.put(("info", f"Khởi động bot (lần thử #{retry_count + 1})"))
            myStyle(log_queue)  # đây là hàm blocking (client.run() bên trong)
            # nếu chạy tới đây tức là client.run() thoát êm (VD: client.close() bình thường)
            log_queue.put(("info", "Bot đã dừng bình thường"))
        except SystemExit as e:
            log_queue.put(("error", f"Bot bị SystemExit: {e}"))
        except Exception as e:
            log_queue.put(("error", f"Bot crash: {e}"))

        retry_count += 1
        wait = min(60, 2 ** min(retry_count, 6))  # backoff: 2,4,8,...tối đa 60s
        log_queue.put(("info", f"Tự động restart sau {wait}s..."))
        time.sleep(wait)


thread = None


@st.cache_resource
def initialize_heavy_stuff():
    global thread
    # Đây là phần chỉ chạy ĐÚNG 1 LẦN khi server khởi động (hoặc khi cache miss)
    with st.spinner("running your scripts..."):
        thread = threading.Thread(
            target=run_bot_forever,  # <-- đổi từ myStyle sang wrapper này
            args=(st.session_state.log_queue,),
            daemon=True,  # để thread không giữ process sống khi app tắt
        )
        thread.start()
        print(
            "Heavy initialization running..."
        )  # bạn sẽ thấy log này chỉ 1 lần trong console/cloud log

        return {
            "model": "loaded_successfully",
            "timestamp": time.time(),
            "db_status": "connected",
        }


# Trong phần chính của app
st.title("my style")

# Dòng này đảm bảo: chạy 1 lần duy nhất, mọi user đều dùng chung kết quả
result = initialize_heavy_stuff()

st.success("The system is ready!")
st.write("Result:")
st.json(result)
with st.status("Processing...", expanded=True) as status:
    placeholder = st.empty()
    logs = []
    while (thread and thread.is_alive()) or not st.session_state.log_queue.empty():
        try:
            level, message = st.session_state.log_queue.get_nowait()
            logs.append((level, message))

            with placeholder.container():
                for lvl, msg in logs:
                    if lvl == "info":
                        st.write(msg)
                    elif lvl == "success":
                        st.success(msg)
                    elif lvl == "error":
                        st.error(msg)

            time.sleep(0.2)
        except queue.Empty:
            time.sleep(0.3)

    status.update(label="Hoàn thành!", state="complete", expanded=False)
