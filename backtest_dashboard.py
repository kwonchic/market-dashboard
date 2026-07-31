#!/usr/bin/env python3
import csv, io, re, subprocess, statistics
from datetime import datetime
from bisect import bisect_right
from collections import defaultdict

def curl(url):
    r=subprocess.run(['curl','-sL','--max-time','60',url],capture_output=True,text=True)
    if r.returncode!=0 or not r.stdout:
        raise RuntimeError(f'curl fail {r.returncode} {url} stderr={r.stderr[:100]}')
    return r.stdout

def fred(series, start='2005-01-01'):
    txt=curl(f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}')
    out=[]
    for row in csv.DictReader(io.StringIO(txt)):
        v=row[series]
        if v and v!='.':
            out.append((row['observation_date'], float(v)))
    return out

series={s:fred(s) for s in ['SP500','VIXCLS','BAMLH0A0HYM2','DFII10']}
print('SERIES', {k:(v[0],v[-1],len(v)) for k,v in series.items()})

cape=[]
try:
    html=curl('https://www.multpl.com/shiller-pe/table/by-month')
    rows=re.findall(r'<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>', html, flags=re.S|re.I)
    for d,v in rows:
        d=re.sub('<.*?>','',d).strip(); v=re.sub('<.*?>','',v).replace('&nbsp;','').replace('&#x2002;','').strip()
        try:
            dt=datetime.strptime(d,'%b %d, %Y').date().isoformat()
            cape.append((dt,float(v)))
        except Exception:
            pass
    cape=sorted(cape)
except Exception as e:
    print('CAPE_PARSE_FAIL',e)
print('CAPE', cape[:1], cape[-3:], len(cape))

maps={k:([d for d,_ in vals],[v for _,v in vals]) for k,vals in series.items()}
caped=[d for d,_ in cape]; capev=[v for _,v in cape]
sp_dates, sp_vals = maps['SP500']

def last_val(k, d):
    dates, vals=maps[k]
    i=bisect_right(dates,d)-1
    return vals[i] if i>=0 else None

def last_cape(d):
    if not cape: return 35.0
    i=bisect_right(caped,d)-1
    return capev[i] if i>=0 else capev[0]

def tone_trend(idx):
    if idx<220: return 'gray'
    last=sp_vals[idx]
    ma=sum(sp_vals[idx-199:idx+1])/200
    ma_prev=sum(sp_vals[idx-219:idx-19])/200
    if last>ma and ma>ma_prev: return 'green'
    if last>ma: return 'amber'
    return 'red'

def tone_cape(v): return 'red' if v>40 else ('amber' if v>=38 else 'green')
def tone_hy(v):
    if v is None: return 'gray'
    bp = v * 100  # FRED raw is percent, dashboard stores bp
    return 'green' if bp<300 else ('amber' if bp<=400 else 'red')
def tone_vix(v): return 'green' if v<20 else ('amber' if v<=28 else 'red')
def tone_real(v): return 'green' if v<1.5 else ('amber' if v<=2.5 else 'red')
PRESS={'cape':{'amber':6,'red':12},'vix':{'amber':5,'red':10},'real_yield':{'amber':4,'red':8},'hy':{'amber':5,'red':10},'trend':{'amber':6,'red':12}}
SUPPORT={'trend','hy'}
def classify(idx, assume_breadth_green=True):
    d=sp_dates[idx]
    tones={}
    tones['trend']=tone_trend(idx)
    tones['hy']=tone_hy(last_val('BAMLH0A0HYM2',d))
    tones['vix']=tone_vix(last_val('VIXCLS',d))
    tones['real_yield']=tone_real(last_val('DFII10',d))
    tones['cape']=tone_cape(last_cape(d))
    if assume_breadth_green:
        tones['breadth']='green'
    pos=50.0
    for k,t in tones.items():
        if k in SUPPORT or k=='breadth':
            if t=='green': pos-=4
        pos+=PRESS.get(k,{}).get(t,0)
    exit_hits=sum(1 for k in ['trend','vix','hy'] if tones.get(k)=='red')
    pos=88 if exit_hits>=2 else min(pos,78)
    pos=max(8,min(92,round(pos)))
    label='적극 매수' if pos<20 else '매수' if pos<40 else '보유' if pos<62 else '조금씩 매도' if pos<82 else '전량 매도'
    return pos,label,tones

rows=[]
for i in range(220,len(sp_dates)-252):
    if i%20==0:
        pos,label,tones=classify(i)
        rows.append((sp_dates[i],sp_vals[i],pos,label,sp_vals[i+63]/sp_vals[i]-1,sp_vals[i+252]/sp_vals[i]-1,tones))
stats=defaultdict(list)
for r in rows: stats[r[3]].append(r)
print('\nSTATS_20D_SAMPLED')
for lab in ['매수','보유','조금씩 매도','전량 매도']:
    rs=stats.get(lab,[])
    if not rs: continue
    r3=[x[4] for x in rs]; r12=[x[5] for x in rs]
    print(lab, 'n',len(rs),'3m_avg',round(statistics.mean(r3)*100,1),'3m_win',round(sum(x>0 for x in r3)/len(r3)*100,1),'12m_avg',round(statistics.mean(r12)*100,1),'12m_win',round(sum(x>0 for x in r12)/len(r12)*100,1))

periods=[('COVID','2020-02-01','2021-03-31'),('2022_bear','2022-06-01','2023-12-31')]
for name,a,b in periods:
    idxs=[i for i,d in enumerate(sp_dates) if a<=d<=b and i>=220 and i+252<len(sp_dates)]
    low=min(idxs,key=lambda i:sp_vals[i])
    first_buy=next((i for i in idxs if i>=low and classify(i)[1] in ['매수','적극 매수']),None)
    first_green=next((i for i in idxs if i>=low and classify(i)[2]['trend']=='green'),None)
    print('\nPERIOD',name,'LOW',sp_dates[low],round(sp_vals[low],2))
    for tag,i in [('first_buy',first_buy),('first_green_trend',first_green)]:
        if i is None:
            print(tag,'NONE'); continue
        pos,label,tones=classify(i)
        print(tag,sp_dates[i],round(sp_vals[i],2),'pos',pos,label,'tones',tones,'from_low_%',round((sp_vals[i]/sp_vals[low]-1)*100,1),'3m_%',round((sp_vals[i+63]/sp_vals[i]-1)*100,1),'12m_%',round((sp_vals[i+252]/sp_vals[i]-1)*100,1))

idx=len(sp_dates)-1
print('\nLATEST',sp_dates[idx],round(sp_vals[idx],2),classify(idx))
