import asyncio


async def getBasic(guild):
    for category in guild.categories:
      if 'banking transactions' in category.name.lower():
          for channel in category.channels:
            if 'mb' in channel.name:
              mbCh = channel
            elif 'tcb' in channel.name:
              tcbCh=channel
            elif 'acb' in channel.name:
              acbCh=channel
    return {'mbCh':mbCh,'acbCh':acbCh,'tcbCh':tcbCh}
