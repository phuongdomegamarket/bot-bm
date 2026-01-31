import asyncio
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
from guild import *

load_dotenv()
if "log_queue" not in st.session_state:
    st.session_state["log_queue"] = queue.Queue()

if "logs" not in st.session_state:
    st.session_state["logs"] = []

if "task_running" not in st.session_state:
    st.session_state["task_running"] = False
processed_thread = set()


def myStyle(log_queue):
    log_queue.put(("info", "Starting process data..."))
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    HEADERS = []
    THREADS = []
    USERNAMES = []
    USERNAME = os.environ.get("username")
    PASSWORD = os.environ.get("password")
    ACCOUNT_NO = os.environ.get("account_no")
    MAIN_CHANNEL = os.environ.get("main_channel")
    RESULT = None

    mb = None

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
        global INFO, mb
        try:
            req = requests.get("http://localhost:8888")
            print(req.status_code)
            log_queue.put(("info", req.status_code))
            print("Client closed")
            log_queue.put(("info", "Client closed"))
            sys.exit("Exited")
        except Exception as error:
            print(error)
            server.b()
            for guild in client.guilds:
                if guild.name.lower() == "phượng đỏ mega":
                    RESULT = await getBasic(guild)
                    mb = bm_lib.MBBank(username=USERNAME, password=PASSWORD)
                    if not getTransMb.is_running():
                        getTransMb.start(guild)

    @tasks.loop(seconds=1)
    async def getTransMb(guild):
        global processed_thread, mb, st
        print("getTransMb is running")
        log_queue.put(("info", "getTransMb is running"))
        if mb:
            try:
                channels = guild.channels
                basic = None
                for channel in channels:
                    if channel.name == MAIN_CHANNEL:
                        basic = channel
                if basic:
                    threads = basic.threads + [
                        thread async for thread in basic.archived_threads()
                    ]
                    applied_tags = []
                    # Get the main account balance and info to find the account number
                    balance_info = mb.getBalance()
                    if not balance_info.acct_list:
                        print("No accounts found.")
                        log_queue.put(("info", "No accounts found."))
                        return

                    # Use the first account for history
                    main_account = None
                    for acc in balance_info.acct_list:
                        if acc.acctNo == ACCOUNT_NO:
                            main_account = acc
                            break
                    if main_account:
                        account_number = main_account.acctNo
                        print(
                            f"Fetching history for account: {account_number} ({main_account.acctAlias})"
                        )
                        log_queue.put(
                            (
                                "info",
                                f"Fetching history for account: {account_number} ({main_account.acctAlias})",
                            )
                        )

                        # Define date range: last 30 days
                        to_date = datetime.now()
                        from_date = to_date - timedelta(days=7)

                        history = mb.getTransactionAccountHistory(
                            accountNo=account_number,
                            from_date=from_date,
                            to_date=to_date,
                        )

                        if not history.transactionHistoryList:
                            print("No transactions found in the last 30 days.")
                            log_queue.put(
                                ("info", "No transactions found in the last 30 days.")
                            )
                        else:
                            for transaction in history.transactionHistoryList:
                                refNo = transaction.refNo
                                currency = transaction.currency
                                amount = (
                                    transaction.creditAmount
                                    if transaction.creditAmount != "0"
                                    else transaction.debitAmount
                                )
                                amount = [f"{cur:,}" for cur in [int(amount)]][0]
                                sign = "+" if transaction.creditAmount != "0" else "-"
                                description = transaction.description
                                transactionAt = transaction.transactionDate
                                timestamp = str(
                                    datetime.strptime(
                                        transactionAt, "%d/%m/%Y %H:%M:%S"
                                    ).timestamp()
                                    * 1000
                                ).split(".")[0]
                                threadName = f"{sign} {amount} {currency}/ {timestamp}/ {refNo}/ {account_number}"
                                if threadName not in str(
                                    threads
                                ) and threadName not in str(processed_thread):
                                    tags = basic.available_tags
                                    st = ""
                                    if sign == "+":
                                        for tag in tags:
                                            if (
                                                "in" in tag.name.lower()
                                                or "chuyển đến" in tag.name.lower()
                                            ):
                                                applied_tags.append(tag)
                                    else:
                                        for tag in tags:
                                            if (
                                                "out" in tag.name.lower()
                                                or "chuyển đi" in tag.name.lower()
                                            ):
                                                applied_tags.append(tag)
                                        st += (
                                            "\nTới ngân hàng: **"
                                            + transaction.bankName
                                            + "**\nSố tài khoản: **"
                                            + transaction.benAccountNo
                                            + "**\nChủ tài khoản: **"
                                            + transaction.benAccountName
                                            + "**"
                                        )
                                    allowed_mentions = discord.AllowedMentions(
                                        everyone=True
                                    )
                                    balance = [
                                        f"{cur:,}"
                                        for cur in [int(transaction.availableBalance)]
                                    ][0]
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
                                        + st
                                        + "\nSố dư hiện tại: **"
                                        + balance
                                        + " "
                                        + currency
                                        + "**\n@everyone",
                                        applied_tags=applied_tags,
                                    )
                                    if thread:
                                        processed_thread.add(threadName)
            except Exception as error:
                log_queue.put(("error", str(error)))
                mb = bm_lib.MBBank(username=USERNAME, password=PASSWORD)
                pass
        else:
            mb = bm_lib.MBBank(username=USERNAME, password=PASSWORD)

    client.run(os.environ.get("botToken"))


thread = None


@st.cache_resource
def initialize_heavy_stuff():
    global thread
    # Đây là phần chỉ chạy ĐÚNG 1 LẦN khi server khởi động (hoặc khi cache miss)
    with st.spinner("running your scripts..."):
        thread = threading.Thread(target=myStyle, args=(st.session_state.log_queue,))
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
with st.status("Processing...", expanded=True) as status:
    placeholder = st.empty()
    logs = []
    while thread.is_alive() or not st.session_state.log_queue.empty():
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
st.success("The system is ready!")
st.write("Result:")
st.json(result)
