import asyncio
import os
import re,json
import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.utils import get
import random
from datetime import datetime,timedelta
import server
from guild import *
from mbbank import *
import aiohttp
import locale
server.b()


intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
HEADERS = []
THREADS = []
USERNAMES = [] 
GUILDID = 1236962671824211998 
USERNAME=os.environ.get('username')
PASSWORD=os.environ.get('password')
RESULT=None

def correctSingleQuoteJSON(s):
    rstr = ""
    escaped = False

    for c in s:
    
        if c == "'" and not escaped:
            c = '"' # replace single with double quote
        
        elif c == "'" and escaped:
            rstr = rstr[:-1] # remove escape character before single quotes
        
        elif c == '"':
            c = '\\' + c # escape existing double quotes
   
        escaped = (c == "\\") # check for an escape character
        rstr += c # append the correct json
    
    return rstr

INFO=False
@client.event
async def on_ready():
    global INFO
    try:
      req=requests.get('http://localhost:8888')
      print(req.status_code)
      await client.close() 
      print('Client closed')
      exit()
    except:
      server.b()  
      guild = client.get_guild(GUILDID)
      rs=await login(USERNAME,PASSWORD)
      if rs:
        INFO=rs
      if not getTransMb.is_running():
        getTransMb.start(guild)
@tasks.loop(seconds=1)
async def getTransMb(guild): 
  global INFO
  print('getTransMb is running')
  if INFO:
    try:
      rs=await getTransaction(INFO['headers'],INFO['deviceId'],INFO['sessionId'],INFO['userId'],INFO['cards'])
      basic=await getBasic(guild)
      threads=basic['mbCh'].threads+[thread async for thread in basic['mbCh'].archived_threads()]
      if rs:
        for item in rs:
          applied_tags=[]
          if item['pos'] not in str(threads):
            tags=basic['mbCh'].available_tags
            st=''
            if item['bankName']=='':
              for tag in tags:
                if 'in' in tag.name.lower() or 'chuyển đến' in tag.name.lower():
                  applied_tags.append(tag)
            elif item['bankName']!='':
              for tag in tags:
                if 'out' in tag.name.lower() or 'chuyển đi' in tag.name.lower():
                  applied_tags.append(tag)
              st+='\nTới ngân hàng: **'+item['bankName']+'**\nSố tài khoản: **'+item['benAccountNo']+'**\nChủ tài khoản: **'+item['benAccountName']+'**'
            allowed_mentions = discord.AllowedMentions(everyone = True)
            amount=item['creditAmount'] if item['bankName']=='' else item['debitAmount']
            amount=[f'{cur:,}' for cur in [int(amount)]][0]
            balance=[f'{cur:,}' for cur in [int(item['availableBalance'])]][0]
            thread=await basic['mbCh'].create_thread(name=('+ ' if item['bankName']=='' else '- ')+amount+' '+item['currency']+'/ '+item['pos'],content='\nSố tiền: **'+amount+' '+item['currency']+'**\nNội dung: **'+item['description']+'**\nThời điểm: **'+item['transactionDate'].split(' ')[1]+'** ngày **'+item['transactionDate'].split(' ')[0]+'**'+st+'\nSố dư hiện tại: **'+balance+' '+item['currency']+'**\n@everyone',applied_tags =applied_tags )
      elif rs!=None:
        INFO=await login(USERNAME,PASSWORD) 
    except Exception as error:
      print(error) 
      INFO=await login(USERNAME,PASSWORD)
      pass
client.run(os.environ.get('botToken'))