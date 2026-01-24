import asyncio
import json
import locale
import os
import random
import re
import sys
from datetime import datetime, timedelta

import aiohttp
import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks
from discord.utils import get

import bm_lib
import server
from guild import *

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
HEADERS = []
THREADS = []
USERNAMES = []
USERNAME = os.environ.get("username")
PASSWORD = os.environ.get("password")
ACCOUNT_NO = os.environ.get("account_no")
RESULT = None
processed_thread = set()
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
        print("Client closed")
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
    global processed_thread, mb
    print("getTransMb is running")
    if mb:
        try:
            basic = await getBasic(guild)
            threads = basic["mbCh"].threads + [
                thread async for thread in basic["mbCh"].archived_threads()
            ]
            applied_tags = []
            # Get the main account balance and info to find the account number
            balance_info = mb.getBalance()
            if not balance_info.acct_list:
                print("No accounts found.")
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

                # Define date range: last 30 days
                to_date = datetime.now()
                from_date = to_date - timedelta(days=30)

                history = mb.getTransactionAccountHistory(
                    accountNo=account_number, from_date=from_date, to_date=to_date
                )

                if not history.transactionHistoryList:
                    print("No transactions found in the last 30 days.")
                else:
                    print(
                        f"\nTransaction History ({from_date.date()} to {to_date.date()}):"
                    )
                    print("-" * 80)
                    print(f"{'Date':<20} | {'Amount':<15} | {'Description'}")
                    print("-" * 80)
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
                        if threadName not in str(threads) and threadName not in str(
                            processed_thread
                        ):
                            tags = basic["mbCh"].available_tags
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
                            allowed_mentions = discord.AllowedMentions(everyone=True)
                            balance = [
                                f"{cur:,}"
                                for cur in [int(transaction.availableBalance)]
                            ][0]
                            thread = await basic["mbCh"].create_thread(
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
            mb = bm_lib.MBBank(username=USERNAME, password=PASSWORD)
            pass
    else:
        mb = bm_lib.MBBank(username=USERNAME, password=PASSWORD)


client.run(os.environ.get("botToken"))
