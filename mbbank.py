import requests
from bs4 import BeautifulSoup as Bs4
import time
import os
import datetime
import random,json
from urllib.parse import unquote
import aiohttp
import base64,re



async def login(user,password):
  stop=False
  while not stop:
    try:
      url='https://online.mbbank.com.vn/api/retail-web-internetbankingms/getCaptchaImage'
      now=datetime.datetime.now()
      now=now+datetime.timedelta(hours=7)
      year=str(now.year)
      month=str(now.month if now.month>9 else ("0"+str(now.month)))
      day=str(now.day if now.day>9 else ("0"+str(now.day)))
      hour=str(now.hour if now.hour>9 else ("0"+str(now.hour)))
      minute=str(now.minute if now.minute>9 else ("0"+str(now.minute)))
      second=str(now.second if now.second>9 else ("0"+str(now.second)))
      minisecond=str(now.microsecond if now.microsecond>9 else ("0"+str(now.microsecond)))[0:1]
      ref=year+month+day+hour+minute+second+minisecond
      deviceId="es4uuquy-mbib-0000-0000-"+ref
      data={"refNo":ref,"deviceIdCommon":deviceId,"sessionId":""}
      headers={
        'authorization':'Basic RU1CUkVUQUlMV0VCOlNEMjM0ZGZnMzQlI0BGR0AzNHNmc2RmNDU4NDNm',
        'user-agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
        'Deviceid':deviceId
      }
      async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        async with session.post(url,headers=headers,json=data) as res:
          js=await res.json()
          imgdata = base64.b64decode(js['imageString'])
          filename = 'captcha.jpg'  # I assume you have a way of picking unique filenames
          with open(filename, 'wb') as f:
              f.write(imgdata)
          url='https://tmpfiles.org/'
          async with session.get(url,headers=headers) as res: 
            content=await res.text()
            html=Bs4(content,'html.parser')
            token=html.find('input',{'name':'_token'})['value']
            data={ 
              'file':open(filename,'rb'),
              '_token':token,
              'upload':'Upload'
            }
            async with session.post(url,data=data,allow_redirects=False) as res:
              if res.status<400:
                id=res.headers['location'].split('/')[3]
                urlFile='https://tmpfiles.org/dl/'+id+'/'+filename
                #print(urlFile)
                url='https://vision.googleapis.com/v1/images:annotate?key=AIzaSyAV-SXt0qiF5aHdn-Zgcl4Gr61_gxx28qs'
                data={"requests":[{"image":{"source":{"imageUri":urlFile}},"features":[{"type":"DOCUMENT_TEXT_DETECTION"}]}]}
                async with session.post(url,headers={'user-agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0','Referer':'https://brandfolder.com/'},json=data) as res:
                  if res.status<400:
                    js=await res.json()
                    text=js['responses'][0]['fullTextAnnotation']['text'] 
                    captcha=text.strip().replace(' ','')
                    data={"userId":user,"password":password,"captcha":captcha,"ibAuthen2faString":"d94f855ee10ccd70171ce81c879e3643","sessionId":None,"refNo":"59f482789f35c9b1d2c7ca80516043ad-2024050710134545","deviceIdCommon":deviceId}
                    #print(captcha)
                    async with session.post('https://online.mbbank.com.vn/api/retail_web/internetbanking/v2.0/doLogin',headers=headers,json=data) as res:
                      js=(await res.json())
                      #print(js)
                      if res.status<400 and js['result']['ok']==True:
                        stop=True
                        sessionId=js['sessionId']
                        userId=js['cust']['userId']
                        cards=js['cust']['cardList']
                        for i,item in enumerate(cards):
                          if i==len(cards)-1:
                            accNo=cards[item]['acctNo']
                        headers['RefNo']=userId+"-"+ref
                        print(user+' login success')
                        return {'headers':headers,'sessionId':sessionId,'userId':userId,'cards':cards,'deviceId':deviceId}
                      print(user,'Trying re-login...')
    except:
      pass

async def getTransaction(headers,deviceId,sessionId,userId,cards):
  for i,item in enumerate(cards):
    if i==len(cards)-1:
      accNo=cards[item]['acctNo']
  now=datetime.datetime.now()
  now=now+datetime.timedelta(hours=7)
  year=str(now.year)
  month=str(now.month if now.month>9 else ("0"+str(now.month)))
  day=str(now.day if now.day>9 else ("0"+str(now.day)))
  hour=str(now.hour if now.hour>9 else ("0"+str(now.hour)))
  minute=str(now.minute if now.minute>9 else ("0"+str(now.minute)))
  second=str(now.second if now.second>9 else ("0"+str(now.second)))
  minisecond=str(now.microsecond if now.microsecond>9 else ("0"+str(now.microsecond)))[0:1]
  ref=year+month+day+hour+minute+second+minisecond
  url='https://online.mbbank.com.vn/api/retail-transactionms/transactionms/get-account-transaction-history'
  data={"accountNo":accNo,"fromDate":f"{day}/{month}/{year}","toDate":f"{day}/{month}/{year}","sessionId":sessionId,"refNo":userId+"-"+ref,"deviceIdCommon":deviceId}
  async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
    async with session.post(url,headers=headers,json=data) as res:
      js=await res.json()
      print(js)
      if res.status<400 and js['result']['ok']==True:
        return js['transactionHistoryList']
      return False
                    
                                