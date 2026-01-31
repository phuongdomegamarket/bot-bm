import asyncio


async def getBasic(guild):
    for category in guild.categories:
        if "banking transactions" in category.name.lower():
            for channel in category.channels:
                if "mb" == channel.name:
                    mbCh = channel
                elif "tcb" == channel.name:
                    tcbCh = channel
                elif "acb" == channel.name:
                    acbCh = channel
                elif "vcb" == channel.name:
                    vcbCh = channel
    return {"mbCh": mbCh, "acbCh": acbCh, "tcbCh": tcbCh, "vcbCh": vcbCh}
