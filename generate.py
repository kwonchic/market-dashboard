#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
시장 신호 대시보드 — Phase 2 제너레이터 (규칙 엔진 · 정적 렌더링판)
- 네트워크 접속 없음(순수 계산). 데이터는 data.json에서 읽는다.
- 실행: python3 generate.py  ->  dashboard.html 생성
- 중요: 모바일 미리보기가 인라인 JS를 실행하지 않으므로, 데이터를 HTML에 직접 박아 렌더링한다.
        탭 전환/설명 펼치기/보조지표는 모두 순수 CSS(라디오+details)로 처리 → JS 0줄.
"""
import json, os, html as _html

HERE = os.path.dirname(os.path.abspath(__file__))

# ── 1) 규칙(기준값) : 숫자 지표는 임계값으로 tone 자동 판정 ───────────────
def tone_cape(v):    return "red" if v > 40 else ("amber" if v >= 38 else "green")
def tone_hy(v):      return "green" if v < 300 else ("amber" if v <= 400 else "red")
def tone_vix(v):     return "green" if v < 20 else ("amber" if v <= 28 else "red")
def tone_breadth(v): return "green" if v >= 55 else ("amber" if v >= 40 else "red")
def tone_fx(v):      return "green" if v < 1350 else ("amber" if v <= 1520 else "red")
AUTO_RULES = {"cape":tone_cape,"hy":tone_hy,"vix":tone_vix,"breadth":tone_breadth,"fx":tone_fx}

# ── 2) 지표 정적 메타 ────────────────────────────────────────────────────
META = {
 "us": {
  "trend":{"name":"추세 (200일 평균선)","orig":"200-day moving average",
    "help":"주가가 200일 평균선 위이고 선이 우상향이면 '건강한 상승'이라 눌릴 때 사 모음. 200일선·50일선 모두 위면 상승 추세 확인.",
    "thr":"주가 &gt; 우상향 200일선 → 사 모으기 / 이탈+꺾임 → 전부 팔기 1차조건","sec":"core"},
  "cape":{"name":"주가 비쌈 정도","orig":"CAPE (경기조정 주가수익비율)",
    "help":"최근 10년 평균 이익 대비 주가가 몇 배인지. 40배대는 역사적 상단권(최고 44배, 닷컴버블급). 단기 방향은 못 맞히지만, 비쌀수록 앞으로 10년 수익이 낮고 폭락 시 낙폭이 큼. '사지 마'가 아니라 '조금씩 사라'.",
    "thr":"38 초과 → 조금씩 팔기 켜짐 / 40 초과 → 신규는 1/3씩만","sec":"core"},
  "hy":{"name":"신용 불안 (회사채 위험 프리미엄)","orig":"HY OAS (하이일드 스프레드)",
    "help":"위험한 회사채에 투자자가 얼마나 더 높은 이자를 요구하는지. 낮으면 시장이 안심(=매수 방해 없음), 저점에서 벌어지기 시작하면 하락의 조기경고. 주가보다 2~4주 앞서 움직이는 편.",
    "thr":"300 미만 우호 · 300~400 주의 · 400 초과 경고","sec":"core"},
  "breadth":{"name":"상승 종목 폭","orig":"breadth (시장 폭)",
    "help":"지수는 신고가인데 오르는 종목 수가 줄면 위험(소수 대형주만 견인). 200일선 위 종목 비율이 높고 넓어지는 중이면 꼭대기 징후 아님.",
    "thr":"지수 신고가인데 오르는 종목↓ 또는 평균선 위&lt;50% → 경고","sec":"core"},
  "vix":{"name":"공포지수","orig":"VIX",
    "help":"시장이 얼마나 겁먹었는지. 낮게 잠잠하다가 갑자기 튀는 '분위기 전환'이 추세 종료와 자주 겹침. 추세·신용과 같이 볼 때 전부 팔기 신호.",
    "thr":"28 초과 + 평균선 이탈 + 신용 400 초과 중 2개 → 전부 팔기","sec":"more"},
 },
 "kr": {
  "trend":{"name":"추세 (200일 평균선)","orig":"200-day moving average",
    "help":"코스피가 200일 평균선 위·우상향이면 사 모음. 다만 하루 ±6% 급등락이라 선 위여도 변동성 자체가 리스크 → 변동성은 별도 지표로 관리.",
    "thr":"주가 &gt; 우상향 200일선 → 사 모으기 / 이탈+꺾임 → 전부 팔기 1차조건","sec":"core"},
  "valuation":{"name":"주가 비쌈 정도","orig":"12개월 선행 PER · PBR",
    "help":"내년 예상 이익 대비 주가. 반도체 이익이 빨리 늘어 선행 PER은 낮게(저평가처럼) 보이지만, '싸다'는 건 실적 정점에 기댄 것 → 사이클 꺾이면 순식간에 비싸짐(그래서 초록 아닌 노랑). PBR은 사상 첫 2배 돌파.",
    "thr":"선행 PER 낮음=우호이나, 이익 정점 의존 → 중립+경고","sec":"core"},
  "flows":{"name":"외국인 수급","orig":"Foreign net buying",
    "help":"한국 증시는 외국인 자금 비중이 커서 이들의 매매가 유동성의 핵심. 순매수면 우호, 대규모 순매도 전환이면 경고. 하루 단위로 크게 출렁임.",
    "thr":"순매수 지속=우호 / 순매도 전환=경고","sec":"core"},
  "fx":{"name":"원달러 환율","orig":"USD/KRW",
    "help":"원화가 약하면 외국인 자금이 빠질 유인이 커짐(환손실). 한국 유동성의 핵심 변수라 외국인 수급과 짝으로 봐야 함.",
    "thr":"1,350 미만 우호 · ~1,520 주의 · 초과 경고(원화 급락)","sec":"core"},
  "bok":{"name":"통화정책 (한은 기준금리)","orig":"BOK base rate",
    "help":"중앙은행이 금리를 올리면 시중 돈을 죄는 것(주식엔 역풍). 인하 전환은 우호. 반도체 호황·물가·환율이 인상 명분.",
    "thr":"인상 = 유동성 역풍 / 인하 전환 = 우호","sec":"more"},
  "vol":{"name":"변동성 (코스피 급등락)","orig":"realized volatility",
    "help":"코스피가 하루걸러 급등락하고 사이드카가 발동하면, 방향과 별개로 변동성 자체가 큰 리스크. 당국이 개입할 정도면 켜짐.",
    "thr":"일간 변동 급확대·사이드카 잦음 → 소액·분할 대응","sec":"more"},
 },
}

SUPPORT = {"trend","hy","breadth","flows"}
PRESSURE_PTS = {
 "cape":{"amber":6,"red":12},"valuation":{"amber":6,"red":12},
 "vix":{"amber":5,"red":10},"fx":{"amber":4,"red":8},
 "bok":{"amber":3,"red":6},"vol":{"amber":4,"red":8},
 "breadth":{"amber":4,"red":8},"hy":{"amber":5,"red":10},
 "flows":{"amber":5,"red":8},"trend":{"amber":6,"red":12},
}
BANDS = [(20,"적극 매수","green"),(40,"매수","green"),(62,"보유","amber"),
         (82,"조금씩 매도","amber"),(101,"전량 매도","red")]
def band(pos):
    for hi,label,tone in BANDS:
        if pos < hi: return label,tone
    return "전량 매도","red"

def resolve_tone(mkt,key,d):
    if d.get("auto") and key in AUTO_RULES: return AUTO_RULES[key](d["num"])
    return d.get("tone","gray")

def compute(mkt,data):
    inds={}
    for key,meta in META[mkt].items():
        d=data[mkt][key]; inds[key]={**meta,**d,"tone":resolve_tone(mkt,key,d)}
    pos=50.0
    for key,v in inds.items():
        t=v["tone"]
        if key in SUPPORT and t=="green": pos-=4
        pos+=PRESSURE_PTS.get(key,{}).get(t,0)
    exit_hits=sum([1 if inds.get(k,{}).get("tone")=="red" else 0 for k in ("trend","vix","hy")])
    pos = 88 if exit_hits>=2 else min(pos,78)
    pos=max(8,min(92,round(pos)))
    return inds,pos,exit_hits

# ── 3) HTML 조각 렌더 ────────────────────────────────────────────────────
def esc(s): return s  # 메타는 신뢰된 소스라 그대로 사용(의도된 인라인 태그 포함)

def row_html(v):
    note = f"  <i style='color:#5C6C82'>({v['note']})</i>" if v.get("note") else ""
    return (f'<details class="drow">'
            f'<summary><span class="dot {v["tone"]}"></span>'
            f'<span class="dname">{v["name"]}</span>'
            f'<span class="dval {v["tone"]}">{v["val"]}</span>'
            f'<span class="caret">설명</span></summary>'
            f'<div class="help"><span class="orig">원어 · {v["orig"]}</span><br>'
            f'{v["help"]}{note}<span class="thr">▸ 기준값: {v["thr"]}</span></div>'
            f'</details>')

def sline_html(v):
    cls={"green":"now-green","amber":"now-amber","red":"now-red"}.get(v["tone"],"")
    nm=v["name"].split(" (")[0]
    return (f'<div class="sline"><b>{nm}</b> — <span class="{cls}">{v["val"]}</span>. '
            f'<span class="th">{v["thr"]}</span></div>')

def stance_of(mkt,pos):
    if mkt=="us":
        return {"text0":"보유","text1":' · 신규는 <span class="tone-amber">조금씩</span> (주가 비쌈)',
          "directive":"추세·신용·상승 종목 폭 모두 양호하고 <b>상승 폭이 넓어지는 중</b>(꼭대기 징후 아님). "
                      "다만 <b>주가가 역사적으로 비싸(CAPE 42)</b> 신규는 조금씩·여유 현금 확보.",
          "caveat":"핵심 지표 라이브 반영. 미터·색은 기준값 규칙으로 자동 산출(베타)."}
    return {"text0":band(pos)[0].replace(" ",""),"text1":' · <span class="tone-amber">소액·분할 방어</span>',
      "directive":"주가는 여전히 <b>싼 편</b>(선행 PER 6.4배)이나, 라이브 데이터상 <b>외국인이 순매도로 전환</b>·"
                  "<b>한은 금리 인상(2.75%)</b>·<b>원화 약세(~1,518원)</b>·<b>±6~8% 급등락</b>이 겹쳐 방어적. "
                  "추세는 아직 안 깨졌으니 전량 청산이 아니라 <b>비중 축소·소액 분할</b>.",
      "caveat":"미터·색은 기준값 규칙으로 자동 산출(베타). 가중치는 튜닝 대상."}

def tiles_html(mkt,inds,exit_hits):
    trim=sum(1 for k,v in inds.items() if k in ("cape","valuation") and v["tone"] in ("amber","red"))
    if mkt=="us":
        T=[("buy","사 모으기","BUY · 누적","우호","g","방해요인 0 · 비싸서 조금씩만"),
           ("trim","조금씩 팔기","TRIM · 트림",f"깃발 {trim}","a","주가 비쌈 켜짐"),
           ("exit","전부 팔기","EXIT · 전량","안전" if exit_hits<2 else "경고","g" if exit_hits<2 else "r",f"위험 신호 {exit_hits} / 3")]
    else:
        T=[("buy","사 모으기","BUY · 누적","조건부","a","싸지만 외국인 순매도 전환"),
           ("trim","조금씩 팔기","TRIM · 트림","주의","a","긴축·환율·변동성 부담"),
           ("exit","전부 팔기","EXIT · 전량","추세 유지","g","200일선 이탈은 아직 아님")]
    cells="".join(f'<div class="tile {c}"><div class="tname">{n}</div><div class="torig">{o}</div>'
                  f'<div class="big {tn}">{b}</div><div class="tmeta">{m}</div></div>' for c,n,o,b,tn,m in T)
    return f'<div class="tiles">{cells}</div>'

def meter_html(pos):
    zone=round(pos/25)
    labels=["적극<br>매수","매수","보유","조금씩<br>매도","전량<br>매도"]
    spans="".join(f'<span class="{"act" if i==zone else ""}">{t}</span>' for i,t in enumerate(labels))
    return (f'<div class="meter-track"><div class="meter-marker" style="left:{pos}%"></div></div>'
            f'<div class="meter-scale">{spans}</div>')

def pane_html(mkt,data):
    inds,pos,exit_hits=compute(mkt,data)
    order=list(META[mkt].keys())
    core="".join(row_html(inds[k]) for k in order if inds[k]["sec"]=="core")
    more="".join(row_html(inds[k]) for k in order if inds[k]["sec"]=="more")
    summary="".join(sline_html(inds[k]) for k in order)
    st=stance_of(mkt,pos)
    badge = '<div id="badgeWrap"><span class="badge">◆ 한은 기준금리 2.75% · 7/16 인상(긴축 전환)</span></div>' if mkt=="kr" else ""
    asof=f'데이터 기준 · <b>{data["asOf"]}</b><br>갱신 · 일~금 21:30 KST · 장 시작 전'
    mkt_name=f'{"미국" if mkt=="us" else "국내"} 증시 · 현재 판단'
    srcs={"us":"SRC · CAPE=multpl/Shiller · 신용(HY OAS)=FRED:BAMLH0A0HYM2 · VIX=FRED:VIXCLS · 추세/상승폭=시장데이터",
          "kr":"SRC · 기준금리=한국은행 · 환율=서울외국환중개/TradingEconomics · 외국인수급=KRX · 선행PER=근사(수동) · 변동성=시장데이터"}[mkt]
    disc={"us":"이 대시보드는 객관적 지표를 규칙에 대조해 보여주는 참고 도구이며, 재무·투자 조언이 아닙니다. 최종 판단과 책임은 이용자 본인에게 있습니다. 미터·색은 기준값 규칙 기반 자동 산출(베타)이며 가중치는 검증·튜닝 대상입니다.",
          "kr":"이 대시보드는 객관적 지표를 규칙에 대조해 보여주는 참고 도구이며, 재무·투자 조언이 아닙니다. 최종 판단과 책임은 이용자 본인에게 있습니다. 한국은 미국과 지표 구성이 다릅니다(환율·외국인 수급 비중 큼). 선행 PER 등 일부는 수동/근사값입니다."}[mkt]
    return f'''<section class="pane pane-{mkt}">
  <div class="hero">
    <div class="top"><div class="eyebrow">{mkt_name}</div><div class="asof">{asof}</div></div>
    {badge}
    <div class="stance-label"><span class="tone-{band(pos)[1]}">{st["text0"]}</span>{st["text1"]}</div>
    <div class="stance-directive">{st["directive"]}</div>
    {meter_html(pos)}
    <div class="caveat"><i>◈</i><span>{st["caveat"]}</span></div>
  </div>
  {tiles_html(mkt,inds,exit_hits)}
  <div class="section-h"><span class="eyebrow">핵심 지표 · 눌러서 설명</span><span class="rule"></span></div>
  <div class="detail">{core}</div>
  <details class="more-wrap"><summary class="more-btn">＋ 보조 지표 더보기</summary>
    <div class="detail" style="margin-top:10px">{more}</div></details>
  <div class="section-h"><span class="eyebrow">요약 · 기준값</span><span class="rule"></span></div>
  <div class="summary">{summary}</div>
  <div class="disclaimer">{disc}</div>
  <div class="srcs">{srcs}</div>
</section>'''

# ── 4) 페이지 (JS 0줄, 라디오+CSS 탭) ───────────────────────────────────
PAGE = '''<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>시장 신호 대시보드</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>
  :root{--bg:#0B1017;--panel:#111823;--panel-2:#0E141D;--line:#202A38;--hi:#E8EDF4;--mid:#93A1B5;--dim:#5C6C82;
    --green:#4FB286;--amber:#D9A441;--red:#E06A5C;--steel:#5FA8C4;
    --mono:'IBM Plex Mono',ui-monospace,Menlo,monospace;--disp:'Space Grotesk','Pretendard',sans-serif;
    --body:'Pretendard',-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo',sans-serif;}
  *{box-sizing:border-box;margin:0;padding:0}html{-webkit-text-size-adjust:100%}
  body{background:radial-gradient(1100px 480px at 80% -10%, rgba(95,168,196,.06), transparent 60%), var(--bg);
    color:var(--hi);font-family:var(--body);line-height:1.5;padding:18px 15px 60px;min-height:100vh;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1080px;margin:0 auto}
  .eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--steel)}
  .tabs-radio{position:absolute;left:-9999px;width:0;height:0;opacity:0}
  .tabbar{display:flex;gap:6px;background:var(--panel-2);border:1px solid var(--line);border-radius:12px;padding:5px;margin-bottom:14px}
  .tab{flex:1;color:var(--mid);font-family:var(--body);font-weight:600;font-size:15px;padding:11px 10px;border-radius:8px;
    cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:center;gap:8px;user-select:none}
  .tab .flag{font-size:16px}
  #t-us:checked ~ .tabbar label[for="t-us"], #t-kr:checked ~ .tabbar label[for="t-kr"]{background:var(--panel);color:var(--hi);box-shadow:inset 0 0 0 1px var(--line)}
  .pane{display:none}
  #t-us:checked ~ .content .pane-us{display:block}
  #t-kr:checked ~ .content .pane-kr{display:block}
  .hero{border:1px solid var(--line);border-radius:16px;background:linear-gradient(180deg,var(--panel),var(--panel-2));padding:20px 20px 22px;margin-bottom:14px}
  .hero .top{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px}
  .hero .asof{font-family:var(--mono);font-size:11.5px;color:var(--dim);text-align:right}.hero .asof b{color:var(--mid);font-weight:500}
  .badge{display:inline-block;margin-top:10px;font-size:11.5px;font-family:var(--mono);color:var(--amber);
    background:rgba(217,164,65,.1);border:1px solid rgba(217,164,65,.3);border-radius:6px;padding:4px 9px}
  .stance-label{font-family:var(--disp);font-weight:700;font-size:29px;letter-spacing:-.01em;line-height:1.1;margin:12px 0 4px}
  .stance-label .tone-amber{color:var(--amber)}.stance-label .tone-green{color:var(--green)}.stance-label .tone-red{color:var(--red)}
  .stance-directive{font-size:14.5px;color:var(--mid);margin-bottom:20px}.stance-directive b{color:var(--hi);font-weight:600}
  .meter-track{position:relative;height:16px;border-radius:9px;background:linear-gradient(90deg,#2E8B6E 0%,#4FB286 24%,#6C7C92 50%,#D9A441 74%,#E06A5C 100%);opacity:.92}
  .meter-marker{position:absolute;top:50%;width:4px;height:34px;background:var(--hi);border-radius:3px;transform:translate(-50%,-50%);box-shadow:0 0 0 3px rgba(11,16,23,.9),0 0 14px rgba(232,237,244,.5)}
  .meter-marker::after{content:"";position:absolute;left:50%;top:-11px;transform:translateX(-50%);border-left:6px solid transparent;border-right:6px solid transparent;border-top:8px solid var(--hi)}
  .meter-scale{display:flex;justify-content:space-between;margin-top:14px}
  .meter-scale span{font-family:var(--mono);font-size:10.5px;color:var(--dim);letter-spacing:.02em;text-align:center;flex:1}
  .meter-scale span.act{color:var(--amber);font-weight:600}
  .caveat{margin-top:16px;font-size:12px;color:var(--dim);font-family:var(--mono);display:flex;gap:7px;align-items:baseline}.caveat i{color:var(--steel);font-style:normal}
  .tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:14px}@media(max-width:720px){.tiles{grid-template-columns:1fr}}
  .tile{border:1px solid var(--line);border-radius:14px;background:var(--panel);padding:15px 16px;position:relative;overflow:hidden}
  .tile::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px}
  .tile.buy::before{background:var(--green)}.tile.trim::before{background:var(--amber)}.tile.exit::before{background:var(--red)}
  .tile .tname{font-family:var(--disp);font-weight:600;font-size:15px}.tile .torig{font-family:var(--mono);font-size:10.5px;color:var(--dim);margin-top:3px}
  .tile .big{font-family:var(--disp);font-weight:700;font-size:20px;line-height:1;margin-top:11px}
  .tile .big.g{color:var(--green)}.tile .big.a{color:var(--amber)}.tile .big.r{color:var(--red)}.tile .tmeta{font-size:12px;color:var(--mid);margin-top:8px}
  .section-h{display:flex;align-items:center;gap:10px;margin:22px 4px 12px}.section-h .rule{height:1px;background:var(--line);flex:1}
  .detail{border:1px solid var(--line);border-radius:14px;background:var(--panel);overflow:hidden}
  details.drow{border-bottom:1px solid rgba(32,42,56,.7)}details.drow:last-child{border-bottom:none}
  details.drow summary{list-style:none;cursor:pointer;padding:13px 16px;display:flex;align-items:center;gap:10px}
  details.drow summary::-webkit-details-marker{display:none}
  .dot{width:10px;height:10px;border-radius:50%;flex:none}
  .dot.green{background:var(--green)}.dot.amber{background:var(--amber)}.dot.red{background:var(--red)}.dot.gray{background:#3A4759}
  .dname{font-size:14.5px;font-weight:500;flex:1}.dval{font-family:var(--mono);font-size:12.5px;font-weight:500;text-align:right}
  .dval.green{color:var(--green)}.dval.amber{color:var(--amber)}.dval.red{color:var(--red)}.dval.gray{color:var(--dim)}
  .caret{font-family:var(--mono);font-size:10.5px;color:var(--dim);border:1px solid var(--line);border-radius:5px;padding:2px 6px;flex:none}
  details[open] .caret{color:var(--steel);border-color:var(--steel)}
  .help{margin:0 16px 13px 36px;padding:11px 13px;border-left:2px solid var(--steel);background:rgba(95,168,196,.06);
    border-radius:0 8px 8px 0;font-size:13px;color:var(--mid);line-height:1.55}
  .help .orig{display:inline-block;font-family:var(--mono);font-size:11.5px;color:var(--steel);margin-bottom:5px}
  .help .thr{font-family:var(--mono);font-size:12px;color:var(--hi);display:block;margin-top:8px}
  .more-wrap{margin-top:10px}
  .more-btn{list-style:none;cursor:pointer;display:block;text-align:center;padding:11px;border:1px dashed var(--line);
    background:transparent;color:var(--mid);font-family:var(--body);font-size:13px;font-weight:500;border-radius:10px}
  .more-btn::-webkit-details-marker{display:none}
  details[open] > .more-btn{color:var(--steel);border-color:var(--steel)}
  .summary{border:1px solid var(--line);border-radius:14px;background:var(--panel-2);padding:16px 17px}
  .sline{padding:11px 0;border-bottom:1px solid rgba(32,42,56,.6);font-size:13.5px;color:var(--mid);line-height:1.55}
  .sline:first-of-type{padding-top:2px}.sline:last-child{border-bottom:none}.sline b{color:var(--hi);font-weight:600}
  .sline .th{font-family:var(--mono);font-size:12px;color:var(--steel)}
  .sline .now-red{color:var(--red);font-weight:600}.sline .now-amber{color:var(--amber);font-weight:600}.sline .now-green{color:var(--green);font-weight:600}
  .disclaimer{margin-top:14px;font-size:11.5px;color:var(--dim);line-height:1.55}
  .srcs{margin-top:10px;font-family:var(--mono);font-size:10.5px;color:#3F4C5E;line-height:1.7}
</style>
</head><body><div class="wrap">
  <input type="radio" name="tab" id="t-us" class="tabs-radio" checked>
  <input type="radio" name="tab" id="t-kr" class="tabs-radio">
  <div class="tabbar">
    <label class="tab" for="t-us"><span class="flag">🇺🇸</span> 미국 증시</label>
    <label class="tab" for="t-kr"><span class="flag">🇰🇷</span> 국내 증시</label>
  </div>
  <div class="content">__US____KR__</div>
</div></body></html>'''

def main():
    with open(os.path.join(HERE,"data.json"),encoding="utf-8") as f:
        data=json.load(f)
    page=PAGE.replace("__US__",pane_html("us",data)).replace("__KR__",pane_html("kr",data))
    out=os.path.join(HERE,"dashboard.html")
    open(out,"w",encoding="utf-8").write(page)
    for mkt in ("us","kr"):
        inds,pos,ex=compute(mkt,data)
        print(f"[{mkt}] pos={pos} -> {band(pos)[0]}  exit={ex}")
    print("OK ->",out)

if __name__=="__main__": main()
