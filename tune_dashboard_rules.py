#!/usr/bin/env python3
import csv, io, re, subprocess, statistics, itertools, json
from datetime import datetime
from bisect import bisect_right
from collections import defaultdict


def curl(url):
    r = subprocess.run(['curl','-sL','--max-time','60',url], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f'curl fail {r.returncode} {url}')
    return r.stdout


def fred(series, start='2005-01-01'):
    txt = curl(f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}')
    out=[]
    for row in csv.DictReader(io.StringIO(txt)):
        v=row[series]
        if v and v!='.':
            out.append((row['observation_date'], float(v)))
    return out


def multpl_cape():
    html = curl('https://www.multpl.com/shiller-pe/table/by-month')
    rows = re.findall(r'<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>', html, re.S|re.I)
    out=[]
    for d,v in rows:
        d=re.sub('<.*?>','',d).strip()
        v=re.sub('<.*?>','',v).replace('&nbsp;','').replace('&#x2002;','').strip()
        try:
            out.append((datetime.strptime(d,'%b %d, %Y').date().isoformat(), float(v)))
        except Exception:
            pass
    return sorted(out)

sp = fred('SP500')
vix = fred('VIXCLS')
real = fred('DFII10')
hy = fred('BAMLH0A0HYM2')  # currently only 2023-07+ from FRED endpoint
cape = multpl_cape()

maps = { 'vix': ([d for d,_ in vix],[v for _,v in vix]),
         'real': ([d for d,_ in real],[v for _,v in real]),
         'hy': ([d for d,_ in hy],[v for _,v in hy]),
         'cape': ([d for d,_ in cape],[v for _,v in cape]) }
dates=[d for d,_ in sp]; closes=[v for _,v in sp]


def last(key, d):
    ds, vs = maps[key]
    i = bisect_right(ds, d)-1
    if i < 0: return None
    return vs[i]


def ma(i,n):
    if i<n-1: return None
    return sum(closes[i-n+1:i+1])/n


def pct(a,b): return a/b-1

def feats(i):
    d=dates[i]; c=closes[i]
    m20=ma(i,20); m50=ma(i,50); m200=ma(i,200)
    m200_prev=ma(i-20,200) if i>=220 else None
    high63=max(closes[max(0,i-62):i+1]); low63=min(closes[max(0,i-62):i+1])
    high252=max(closes[max(0,i-251):i+1]); low252=min(closes[max(0,i-251):i+1])
    v=last('vix',d); rv=last('real',d); hv=last('hy',d); cv=last('cape',d)
    v20=None
    # VIX 20 trading-day trend by nearest date index on vix series
    vds, vvs = maps['vix']; vi=bisect_right(vds,d)-1
    if vi>=20: v20=vvs[vi-20]
    return dict(
        date=d, close=c,
        above20=(m20 is not None and c>m20),
        above50=(m50 is not None and c>m50),
        above200=(m200 is not None and c>m200),
        ma200_up=(m200 is not None and m200_prev is not None and m200>m200_prev),
        golden=(m50 is not None and m200 is not None and m50>m200),
        dd63=c/high63-1, dd252=c/high252-1,
        reb63=c/low63-1, reb252=c/low252-1,
        r20=pct(c,closes[i-20]) if i>=20 else 0,
        r60=pct(c,closes[i-60]) if i>=60 else 0,
        vix=v, vix_falling=(v is not None and v20 is not None and v < v20),
        real=rv, hy_bp=(hv*100 if hv is not None else None), cape=cv,
    )


def label_old(i):
    f=feats(i)
    pos=50
    # approximate current dashboard US incl breadth green assumed
    if f['above200'] and f['ma200_up']: pos-=4
    elif f['above200']: pos+=6
    else: pos+=12
    pos-=4  # breadth green assumption
    if f['hy_bp'] is not None:
        if f['hy_bp']<300: pos-=4
        elif f['hy_bp']<=400: pos+=5
        else: pos+=10
    if f['vix'] is not None:
        if f['vix']<20: pass
        elif f['vix']<=28: pos+=5
        else: pos+=10
    if f['real'] is not None:
        if f['real']<1.5: pass
        elif f['real']<=2.5: pos+=4
        else: pos+=8
    if f['cape'] is not None:
        if f['cape']>40: pos+=12
        elif f['cape']>=38: pos+=6
    pos=max(8,min(92,round(pos)))
    return band(pos), pos


def band(pos):
    if pos<20: return '적극 매수'
    if pos<40: return '매수'
    if pos<62: return '보유'
    if pos<82: return '조금씩 매도'
    return '전량 매도'


def new_score(i, W):
    f=feats(i); pos=50
    # trend/risk-off structure
    if f['above200'] and f['ma200_up']: pos -= W['trend_up']
    elif f['above200']: pos += W['trend_flat']
    else: pos += W['below200']
    if f['above50']: pos -= W['above50']
    else: pos += W['below50']
    if f['golden']: pos -= W['golden']
    # capitulation / exact bottom signal: deep 1Y drawdown + VIX panic should be buy,
    # not wait for 50/200d confirmation.
    capitulation = f['dd252'] <= -0.20 and f['vix'] is not None and f['vix'] >= 30 and not (f['cape'] is not None and f['cape'] > 40)
    if capitulation:
        return 34
    # bottom recovery: low에서 벗어나지만 아직 과열 전인 구간
    if f['reb63'] >= W['reb_thr'] and f['r20'] > 0 and f['vix_falling']:
        pos -= W['recovery']
    # top rollover / 고점 꺾임
    if f['dd63'] <= -W['dd63_thr'] and f['r20'] < 0 and not f['above20']:
        pos += W['rollover']
    if f['dd252'] <= -0.10 and not f['above50']:
        pos += W['deep_dd']
    # valuation/liquidity pressure
    if f['cape'] is not None:
        if f['cape']>40: pos += W['cape_red']
        elif f['cape']>=38: pos += W['cape_amber']
    if f['real'] is not None:
        if f['real']>2.5: pos += W['real_red']
        elif f['real']>=1.5: pos += W['real_amber']
        else: pos -= W['real_green']
    if f['vix'] is not None:
        if f['vix']>28: pos += W['vix_red']
        elif f['vix']>=20: pos += W['vix_amber']
        else: pos -= W['vix_green']
    if f['vix_falling']: pos -= W['vix_falling']
    if f['hy_bp'] is not None:
        if f['hy_bp']>400: pos += W['hy_red']
        elif f['hy_bp']>=300: pos += W['hy_amber']
        else: pos -= W['hy_green']
    # crisis override
    exit_hits = int((not f['above200']) and f['r60']<0) + int(f['vix'] is not None and f['vix']>28) + int(f['hy_bp'] is not None and f['hy_bp']>400)
    if exit_hits>=2 and not capitulation: pos=max(pos,82)
    return max(8,min(92,round(pos)))

# Target evaluation: buy should have high 6/12m returns; sell should avoid bad next 3/6m drawdowns.
# Use every 10 trading days to reduce overlap.
valid = [i for i in range(252, len(dates)-252, 10)]

def eval_W(W):
    buckets=defaultdict(list)
    misses=[]
    for i in valid:
        pos=new_score(i,W); lab=band(pos)
        r3=closes[i+63]/closes[i]-1; r6=closes[i+126]/closes[i]-1; r12=closes[i+252]/closes[i]-1
        buckets[lab].append((r3,r6,r12,i,pos))
    # score: buy 12m avg/win, sell lower 3m/6m avg, monotonicity + enough signals
    score=0
    if buckets['매수']:
        b=buckets['매수']; score += statistics.mean(x[2] for x in b)*180 + (sum(x[2]>0 for x in b)/len(b))*20
        score += min(len(b),20)*0.2
    else: score -= 30
    if buckets['조금씩 매도']:
        s=buckets['조금씩 매도']; score -= statistics.mean(x[0] for x in s)*80; score -= statistics.mean(x[1] for x in s)*40
    if buckets['전량 매도']:
        e=buckets['전량 매도']; score -= statistics.mean(x[0] for x in e)*100; score -= statistics.mean(x[1] for x in e)*60
    # COVID/2022 recovery must become buy within some window after low
    for a,b in [('2020-03-23','2020-09-30'),('2022-10-12','2023-08-31')]:
        idxs=[i for i,d in enumerate(dates) if a<=d<=b and 252<=i<len(dates)-252]
        got=any(band(new_score(i,W))=='매수' for i in idxs)
        score += 15 if got else -25
    return score,buckets

base=dict(trend_up=8,trend_flat=4,below200=12,above50=5,below50=6,golden=4,reb_thr=0.08,recovery=8,dd63_thr=0.03,rollover=10,deep_dd=8,cape_red=10,cape_amber=5,real_red=6,real_amber=3,real_green=2,vix_red=10,vix_amber=4,vix_green=2,vix_falling=4,hy_red=10,hy_amber=5,hy_green=4)
space=[]
for recovery in [8,12,16]:
  for rollover in [10,14,18]:
    for above50 in [5,7,9]:
      for cape_red in [8,10,12]:
        for real_amber in [2,3,4]:
          W=base|dict(recovery=recovery, rollover=rollover, above50=above50, cape_red=cape_red, real_amber=real_amber)
          sc,b=eval_W(W); space.append((sc,W,b))
space.sort(key=lambda x:x[0], reverse=True)
best=space[0]
print('DATA', dates[0], dates[-1], 'n', len(dates), 'valid', len(valid))
print('BEST_SCORE', round(best[0],2), json.dumps(best[1], ensure_ascii=False))
for name,buck in best[2].items():
    if not buck: continue
    print('BUCKET', name, 'n', len(buck), '3m_avg', round(statistics.mean(x[0] for x in buck)*100,1), '6m_avg', round(statistics.mean(x[1] for x in buck)*100,1), '12m_avg', round(statistics.mean(x[2] for x in buck)*100,1), '12m_win', round(sum(x[2]>0 for x in buck)/len(buck)*100,1))

W=best[1]
for name,a,b in [('COVID','2020-02-01','2021-03-31'),('2022_bear','2022-06-01','2023-12-31'),('current_2026','2026-01-01','2026-07-29')]:
    idxs=[i for i,d in enumerate(dates) if a<=d<=b and 252<=i<len(dates)-252]
    if not idxs: idxs=[i for i,d in enumerate(dates) if a<=d<=b and 252<=i]
    low=min(idxs, key=lambda i:closes[i])
    high=max(idxs, key=lambda i:closes[i])
    first_buy=next((i for i in idxs if i>=low and band(new_score(i,W))=='매수'), None)
    first_sell=next((i for i in idxs if i>=high and band(new_score(i,W)) in ('조금씩 매도','전량 매도')), None)
    print('\nPERIOD', name, 'LOW', dates[low], round(closes[low],2), 'HIGH', dates[high], round(closes[high],2))
    for tag,i in [('first_buy_after_low',first_buy),('first_sell_after_high',first_sell)]:
        if i is None:
            print(tag, 'NONE'); continue
        pos=new_score(i,W); f=feats(i)
        r3 = closes[min(i+63,len(closes)-1)]/closes[i]-1
        r12 = closes[min(i+252,len(closes)-1)]/closes[i]-1 if i+252 < len(closes) else None
        print(tag, dates[i], round(closes[i],2), 'pos', pos, band(pos), 'from_low%', round((closes[i]/closes[low]-1)*100,1), 'from_high%', round((closes[i]/closes[high]-1)*100,1), '3m%', round(r3*100,1), '12m%', None if r12 is None else round(r12*100,1), 'feat', {k:f[k] for k in ['above50','above200','ma200_up','golden','dd63','reb63','r20','vix','vix_falling','real','cape','hy_bp']})

latest=len(dates)-1
print('\nLATEST', dates[latest], round(closes[latest],2), 'old', label_old(latest), 'new', (new_score(latest,W), band(new_score(latest,W))), {k:feats(latest)[k] for k in ['above50','above200','ma200_up','golden','dd63','reb63','r20','vix','vix_falling','real','cape','hy_bp']})
