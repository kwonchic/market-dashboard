# 시장 신호 대시보드

미국·국내 증시의 객관적 지표를 정해진 기준값에 대조해
"사 모으기 / 보유 / 조금씩 매도 / 전량 매도" 판단을 자동 산출하는 정적 대시보드.

**공개 URL:** https://kwonchic.github.io/market-dashboard/

## 구조

```
fetch_data.py ──▶ data.json ──▶ generate.py ──▶ dashboard.html ──▶ index.html (Pages 게시)
 (지표 수집)      (현재값)       (규칙 엔진)      (정적 HTML, JS 0줄)
```

- **자동 수집** (매일 21:30 KST, GitHub Actions): HY OAS·VIX·S&P 200일선(FRED CSV), CAPE(multpl), USD/KRW(open.er-api)
- **수동 유지** (무료 소스 없음): us.breadth, kr.valuation, kr.flows, kr.bok, kr.vol — `data.json`에서 직접 수정
- 로컬 실행: `python3 fetch_data.py && python3 generate.py` (외부 의존성 없음, curl만 사용)

## 주의

투자 조언이 아닌 참고용 지표 도구입니다. 미터 가중치는 베타(튜닝 대상).
값은 실시간이 아니라 각 소스의 최신 관측일 기준입니다.
