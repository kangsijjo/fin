# -*- coding: utf-8 -*-
"""
strategy_explain.py — 전략을 '쉬운 말'로 풀어 주는 설명기. (2026-10-18)

대시보드 기간 백테스트 테스트기에서 전략 이름을 누르면 뜨는 팝업의 내용.
설명은 **실제 전략 객체의 파라미터**에서 만든다(손으로 숫자를 적지 않는다) — 파라미터를 바꾸면
설명도 같이 바뀌고, 테스트가 55개 전부 설명이 있는지 확인한다.

explain(strategy) → {"title", "idea", "buy": [...], "sell": [...], "data": [...], "caution": [...]}
"""

_MKT = "시장 분위기가 좋을 때만: 전 종목 하루 등락률 평균의 60일 평균이 0보다 큼(약세장엔 안 삼)"


def _won(v):
    v = float(v or 0)
    return f"{v / 1e8:,.0f}억" if v >= 1e8 else f"{v:,.0f}원"


def _days(n):
    n = int(n)
    return {252: "252거래일(약 1년)", 500: "500거래일(약 2년)", 60: "60거래일(약 3개월)",
            20: "20거래일(약 1달)"}.get(n, f"{n}거래일")


def _tv(v):
    return f"그날 거래대금 {_won(v)} 이상(거래가 적은 종목 제외)"


def _hold(n, extra=""):
    return f"산 날 포함 {int(n)}거래일째 종가에 판다{extra}"


def _common(s, buy, sell, data=None, caution=None, title="", idea=""):
    v = vars(s)
    if v.get("use_mkt") or v.get("use_market_filter"):
        buy.append(_MKT)
    if v.get("min_tv") is not None:
        buy.append(_tv(v["min_tv"]))
    stop = v.get("stop_loss_pct")
    if stop:
        sell.append(f"손절: 산 값보다 {abs(float(stop)):.0f}% 떨어지면 판다")
    return {"title": title, "idea": idea, "buy": buy, "sell": sell,
            "data": data or ["가격·거래대금(일봉)"], "caution": caution or []}


def explain(s):
    """전략 객체 → 쉬운 설명 dict. 모르는 클래스면 None."""
    c = type(s).__name__
    v = vars(s)
    H = v.get("holding_days", v.get("holding", 20))

    if c == "HighWithFiltersStrategy":
        L = v["lookback"]
        return _common(s, [f"오늘 종가가 직전 {_days(L)} 동안의 최고가를 넘었다(신고가)",
                           f"그날 거래대금이 최근 30일 평균의 {v['vol_mult']:g}배 이상(관심이 몰림)"],
                       [_hold(v["holding"], " — 손절·익절 없음")],
                       title=f"{'52주' if L == 252 else str(L) + '일'} 신고가 + 거래 급증 + 시장 강세",
                       idea="오랫동안 못 넘던 가격을, 거래가 몰리며 넘어선 종목을 따라 탄다(추세 추종). "
                            "기간이 길수록(500일) 더 드문 돌파만 고른다.")
    if c == "FiftyTwoWeekHighStrategy":
        L = v["lookback"]
        return _common(s, [f"오늘 종가가 직전 {_days(L)} 최고가를 넘었다"], [_hold(H)],
                       title=f"{'52주' if L == 252 else str(L) + '일'} 신고가(다른 조건 없음)",
                       idea="그 기간 중 가장 높은 가격을 넘으면 산다 — 거래량·시장 조건이 없는 가장 단순한 신고가 전략.")
    if c == "FiveDayBreakoutStrategy":
        return _common(s, [f"오늘 종가가 직전 {v['lookback']}일 최고가를 넘었다",
                           f"거래량이 최근 {v['lookback']}일 평균의 {v['vol_mult']:g}배 이상"], [_hold(H)],
                       title=f"{v['lookback']}일 고점 돌파 + 거래량 {v['vol_mult']:g}배",
                       idea="며칠 사이 고점을 거래량과 함께 뚫는 단기 급등 초입을 노린다.")
    if c == "VolumeSurgeStrategy":
        return _common(s, [f"그날 거래대금이 최근 {v['baseline']}일 평균의 {v['mult']:g}배 이상"], [_hold(H)],
                       title=f"거래대금 {v['mult']:g}배 급증 · {H}일 보유",
                       idea="갑자기 거래가 터진 종목은 다음날도 움직인다는 가정 — 아주 짧게 보유.")
    if c == "FiveDayMomentumStrategy":
        return _common(s, [f"최근 5일 수익률이 그날 전체 상위 {v['top_n']}개 안"], [_hold(H)],
                       title=f"5일 수익률 상위 {v['top_n']}개",
                       idea="최근 많이 오른 종목이 조금 더 오른다는 가정(단기 모멘텀).")
    if c == "RsiReversalStrategy":
        return _common(s, [f"RSI({v['rsi_period']})가 {v['rsi_th']} 미만 — 최근 하락이 상승보다 훨씬 컸던 '과매도' 상태"],
                       [_hold(H)], title=f"RSI {v['rsi_th']} 미만 과매도 반등",
                       idea="많이 떨어진 종목은 곧 반등한다는 가정(역추세).")
    if c == "RsiVolumeStrategy":
        return _common(s, [f"RSI({v['rsi_period']})가 {v['rsi_th']} 미만(과매도)",
                           f"그날 거래대금이 최근 {v['vol_ma_days']}일 평균의 {v['vol_mult']:g}배 초과"], [_hold(H)],
                       title="과매도 + 거래 급증",
                       idea="공포 매도가 거래 폭증으로 '바닥을 찍는' 순간을 노린다(역추세).")
    if c == "VolMaBreakoutStrategy":
        return _common(s, [f"거래량이 최근 {v['ma_period']}일 평균의 {v['vol_mult']:g}배 이상",
                           f"종가가 {v['ma_period']}일 이동평균선 위"], [_hold(H)],
                       title=f"거래량 {v['vol_mult']:g}배 + {v['ma_period']}일선 위",
                       idea="평소보다 훨씬 많은 거래가 붙으면서 추세선 위에 있으면 돈이 몰리는 중으로 본다.")
    if c == "ForeignHighStrategy":
        return _common(s, [f"외국인이 {v['n_days']}일 연속 순매수",
                           f"종가가 직전 {_days(v['high_period'])} 최고가 이상(신고가)"], [_hold(H)],
                       data=["가격·거래대금", "외국인 순매수(일별)"],
                       title=f"외국인 {v['n_days']}일 연속 매수 + {v['high_period']}일 신고가",
                       idea="외국인이 연달아 사는 중에 가격도 신고가를 내면 '수급 + 추세'가 함께 확인된 것으로 본다.")
    if c == "InstForeignStrategy":
        return _common(s, [f"외국인과 기관이 둘 다 {v['n_days']}일 연속 순매수"], [_hold(H)],
                       data=["가격·거래대금", "외국인·기관 순매수(일별)"],
                       title=f"외국인 + 기관 {v['n_days']}일 동반 매수",
                       idea="정보가 많은 두 큰손이 동시에 사 모으는 종목을 따라 산다.")
    if c == "MaRibbonStrategy":
        return _common(s, ["이동평균이 5일 > 20일 > 60일 > 120일 순서로 '정배열'이 됐다",
                           "어제는 정배열이 아니었다(새로 정렬된 날만)" if v.get("new_cross_only") else "정배열 상태"],
                       [_hold(H)], title="이동평균 정배열이 새로 생긴 날",
                       idea="단기·중기·장기 평균이 모두 위로 줄을 서는 순간 = 상승 추세가 자리잡는 시점으로 본다.")
    if c == "SwingGoldenCrossStrategy":
        return _common(s, [f"{v['fast']}일 이동평균이 {v['slow']}일 이동평균을 아래에서 위로 뚫었다(골든크로스)",
                           f"RSI(14)가 {v['rsi_low']}~{v['rsi_high']} 사이(너무 과열·과매도 아님)"], [_hold(H)],
                       title=f"골든크로스 {v['fast']}/{v['slow']}",
                       idea="짧은 평균이 긴 평균을 넘어서는 추세 전환 초입을 노린다.")
    if c == "PullbackMaStrategy":
        return _common(s, [f"60일 이동평균이 {v['ma60_slope_days']}일 전보다 높다(큰 추세는 상승)",
                           "5일 이동평균이 20일 이동평균을 아래에서 위로 뚫었다(잠깐 쉬었다 다시 오름)"], [_hold(H)],
                       title="눌림목 회복",
                       idea="상승 추세 중 잠깐 쉬어 간 뒤 다시 오르기 시작할 때 산다.")
    if c == "Breakout20DStrategy":
        return _common(s, [f"종가가 직전 {_days(v['lookback'])} 최고가를 넘었다",
                           f"거래량이 최근 20일 평균의 {v['vol_mult']:g}배 이상"], [_hold(H)],
                       title=f"{v['lookback']}일 신고가 + 거래량 {v['vol_mult']:g}배",
                       idea="중단기 고점을 거래량과 함께 넘는 돌파를 따라간다.")
    if c == "MacdCrossStrategy":
        b = [f"MACD({v['fast']},{v['slow']})가 시그널선({v['signal']})을 아래에서 위로 뚫었다"]
        # [2026-10-18 발견] require_zero_cross 는 '어제 히스토그램 ≤ 0' 인데, 이는 '어제 MACD ≤ 시그널'
        # (교차의 정의)과 같은 조건이라 아무것도 거르지 않는다 → macd_zx 와 macd_all 결과가 똑같다.
        caution = (["⚠ '0선 아래' 조건이 실제 코드에선 아무것도 거르지 않는다(교차 조건과 같은 식) — "
                    "그래서 macd_all 과 결과가 똑같다. 의도대로라면 'MACD < 0' 이어야 함"]
                   if v.get("require_zero_cross") else [])
        return _common(s, b, [_hold(H)], title="MACD 골든크로스" + (" (0선 조건 표기)" if v.get("require_zero_cross") else ""),
                       idea="단기·장기 평균의 차이(MACD)가 방향을 위로 바꾸는 순간을 추세 전환으로 본다.",
                       caution=caution)
    if c == "BbBreakoutStrategy":
        return _common(s, [f"종가가 볼린저밴드 상단({v['period']}일 평균 + {v['std_mult']:g}×표준편차)을 넘었다",
                           f"거래량이 20일 평균의 {v['vol_mult']:g}배 이상"], [_hold(H)],
                       title="볼린저밴드 상단 돌파",
                       idea="평소 움직임 범위의 위쪽 끝을 거래와 함께 뚫으면 추세가 빨라지는 것으로 본다.")
    if c == "BbSqueezeStrategy":
        return _common(s, [f"볼린저밴드 폭이 최근 {v['squeeze_window']}일 중 좁은 쪽 {v['squeeze_rank']}% 안이었다(조용히 응축)",
                           "오늘 밴드 폭이 어제보다 넓어졌다(터지기 시작)", "종가가 밴드 중심선(20일 평균) 위"], [_hold(H)],
                       title="볼린저밴드 수축 후 확장",
                       idea="오랫동안 좁게 움직이던 주가가 위로 움직이기 시작할 때 큰 추세가 나온다는 가정.")
    if c == "BbSqueezeBreakoutStrategy":
        return _common(s, [f"볼린저밴드 폭이 최근 {v['squeeze_window']}일 중 좁은 쪽 {v['squeeze_rank']}% 안이었다",
                           "오늘 밴드 폭이 어제보다 넓어졌다",
                           f"종가가 밴드 상단({v['bb_period']}일 평균 + {v['std_mult']:g}×표준편차) 위 — 강한 돌파만"],
                       [_hold(H)], title="밴드 수축 후 상단 돌파",
                       idea="수축 뒤 '확실하게' 위로 뚫은 경우만 고르는, bb_squeeze 의 엄격한 버전.")
    if c == "StochasticCrossStrategy":
        return _common(s, [f"스토캐스틱 %K({v['k_period']})가 %D({v['d_period']})를 위로 뚫었다",
                           f"어제 %K가 {v['oversold']} 미만(과매도에서 빠져나옴)"], [_hold(H)],
                       title=f"스토캐스틱 과매도({v['oversold']}) 탈출",
                       idea="최근 가격 범위의 바닥 근처에서 다시 올라오기 시작하는 단기 반등을 노린다.")
    if c == "High52wForeignStrategy":
        return _common(s, [f"오늘 종가가 직전 {_days(v['lookback'])} 최고가를 넘었다(52주 신고가)",
                           f"외국인이 {v['foreign_n']}일 연속 순매수"], [_hold(H)],
                       data=["가격·거래대금", "외국인 순매수(일별)"],
                       title=f"52주 신고가 + 외국인 {v['foreign_n']}일 연속 매수",
                       idea="신고가 돌파를 외국인 매수가 뒷받침할 때만 산다 — 가짜 돌파를 줄이려는 조합.")
    if c == "InstPullbackStrategy":
        return _common(s, [f"외국인과 기관이 {v['n_days']}일 연속 순매수",
                           f"그런데 주가는 약함: 종가 < {v['ma_days']}일 이동평균 또는 5일 전보다 낮음"], [_hold(H)],
                       data=["가격·거래대금", "외국인·기관 순매수(일별)"],
                       title="큰손 매수 중 눌림목",
                       idea="큰손이 사 모으는데 주가가 잠깐 약할 때 따라 사서 반등을 노린다.")
    if c == "High52wShortDecreaseStrategy":
        return _common(s, [f"오늘 종가가 직전 {_days(v['lookback'])} 최고가를 넘었다",
                           f"공매도 거래 비중이 {v['short_lookback']}일 전보다 낮다(공매도가 줄어듦)"], [_hold(H)],
                       data=["가격·거래대금", "코스닥 공매도 거래 비중(2018-06~)"],
                       title="52주 신고가 + 공매도 감소",
                       idea="신고가인데 주가 하락에 베팅하던 공매도가 줄면 위로 더 갈 여지가 있다는 가정.",
                       caution=["'공매도 잔고'가 아니라 '공매도 거래 비중'이다", "2020-03~2021-05 공매도 금지기간엔 신호가 거의 없다"])
    if c == "GcForeignStrategy":
        return _common(s, [f"{v['fast_ma']}일 이동평균이 {v['slow_ma']}일 이동평균을 위로 뚫었다(골든크로스)",
                           f"외국인이 {v['foreign_n']}일 연속 순매수"], [_hold(H)],
                       data=["가격·거래대금", "외국인 순매수(일별)"],
                       title=f"골든크로스 {v['fast_ma']}/{v['slow_ma']} + 외국인 {v['foreign_n']}일 매수",
                       idea="추세 전환 신호를 외국인 매수가 함께 확인해 줄 때 산다.")
    if c == "High52wCreditDecreaseStrategy":
        return _common(s, [f"오늘 종가가 직전 {_days(v['lookback'])} 최고가를 넘었다",
                           f"신용잔고율(빚내서 산 주식 비율)이 {v['credit_lookback']}일 전보다 낮다"], [_hold(H)],
                       data=["가격·거래대금", "종목별 신용잔고(키움, 2021-01~)"],
                       title="52주 신고가 + 신용잔고 감소",
                       idea="빚내서 산 물량(나중에 던질 매물)이 줄어든 신고가 종목이 더 오래 간다는 가정.",
                       caution=["⚠ 신용잔고는 1~2거래일 늦게 공개되는데 백테스트는 당일 값을 썼다 — 실제보다 좋게 나온다",
                                "2일 늦게 보게 하면 2022~2025 CAGR 9.2% → 1.0%, 2026년 −47% (설명서 §56)"])
    if c == "SupplyReversalStrategy":
        b = [f"개인의 최근 {v['accum_days']}일 누적 순매수가 플러스 → 마이너스로 바뀌었다(개인이 팔기 시작)",
             f"외국인{' 그리고' if v['smart_mode'] == 'and' else ' 또는'} 기관의 {v['accum_days']}일 누적이 마이너스 → 플러스로 바뀌었다(큰손이 사기 시작)",
             f"두 변화가 {v['cross_window']}거래일 안에 함께 일어남"]
        if v.get("use_price_gate"):
            b.append("종가가 20일 이동평균 위(추세 확인)")
        return _common(s, b, [_hold(H)], data=["가격·거래대금", "개인·외국인·기관 순매수(일별)"],
                       title=f"수급 주체 교체 ({v['accum_days']}일 누적)",
                       idea="개인 물량을 외국인·기관이 받아 가기 시작하는 '주인 바뀌는 순간'을 잡는다.")
    if c == "SupplyRatioStrategy":
        smart = (f"(외국인+기관 {v['window']}일 순매수 ÷ {v['window']}일 거래대금)이 그날 전체 상위 {v['smart_top_pct']:g}% "
                 "— 거래 규모 대비 큰손 매수가 센 종목")
        ind = f"개인 {v['window']}일 순매도 ÷ 거래대금이 그날 전체 상위 {v['ind_top_pct']:g}% — 개인이 강하게 파는 종목"
        b = {"smart": [smart], "ind": [ind], "both": [smart, ind]}[v["mode"]]
        b.append(f"위 조건이 {v['consec']}일 연속 유지")
        return _common(s, b, [_hold(H)], data=["가격·거래대금", "개인·외국인·기관 순매수(일별)"],
                       title={"smart": "큰손 매수 강도 상위", "ind": "개인 매도 강도 상위",
                              "both": "큰손 매수 + 개인 매도 강도 상위"}[v["mode"]],
                       idea="거래 규모 대비 수급 '세기'로 줄 세워 상위 종목을 산다(0 교차보다 덜 흔들림).",
                       caution=["신호가 하루 수십 건이라 10슬롯에 무엇을 먼저 담느냐(점수순)가 결과를 크게 좌우한다",
                                "신호 전체 평균은 −0.4% 안팎 — 이익은 고르는 순서에서 나왔다(2021~2025)"])
    if c == "SupplyCrossoverStrategy":
        tp = v.get("take_profit_pct")
        return _common(s, [f"기관 또는 외국인의 {v['accum_days']}일 누적 순매수선이 개인 누적선을 아래에서 위로 넘었다"],
                       [f"익절: {tp:g}% 오르면 그날 판다(일중 고가 기준)" if tp else "익절 없음", _hold(H)],
                       data=["가격·거래대금", "개인·외국인·기관 순매수(일별)"],
                       title=f"수급선 역전{' + ' + format(tp, 'g') + '% 익절' if tp else ' (20일 보유)'}",
                       idea="큰손 누적 매수가 개인을 추월하는 순간을 산다.",
                       caution=["작은 익절 + 무손절은 '작게 자주 벌고 가끔 크게 잃는' 구조 — 승률보다 평균·MDD 로 봐야 한다"] if tp else [])
    return None


# 백테스트 공통 규칙(팝업 하단 고정 문구)
COMMON_RULES = ["신호가 난 다음날 아침 시가에 산다", "전략마다 10칸(슬롯)에 돈을 나눠 담는다 — 빈 칸이 없으면 신호를 건너뛴다",
                "같은 날 후보가 여러 개면 전략 점수가 높은 순", "수수료·세금·슬리피지 0.245% 반영",
                "손절·익절·강도 필터는 위에 적힌 것만 — 실제 모의매매 규칙과 다를 수 있다"]


# 대시보드 다른 탭에 나오는 '라이브 이름·옛 이름' → 설명 (2026-10-19, 전 탭 마우스오버 설명용)
def legacy_strategies():
    """trades_history·옛 신호 파일에 남은 이름들(엔진 목록엔 없음) → 같은 규칙의 전략 객체."""
    from strategies.high_with_filters import HighWithFiltersStrategy
    from strategies.high_52w import FiftyTwoWeekHighStrategy
    return {
        "h500_40_MKT": HighWithFiltersStrategy(lookback_days=500, holding_days=40, use_market_filter=True,
                                               use_volume_filter=False, name="h500_40_MKT"),
        "h252_40": FiftyTwoWeekHighStrategy(lookback_days=252, holding_days=40, name="h252_40"),
        "h500_20": FiftyTwoWeekHighStrategy(lookback_days=500, holding_days=20, name="h500_20"),
    }


# 라이브(모의매매) 이름 → 백테스트 엔진 이름
LIVE_ALIAS = {"high_52w_filt": "star_high_52w_20_filt", "high_500d_filt": "star_high_500d_20"}
RETIRED = {"rsi_reversal": "2026-10-18 키움 운용 종료(두 기간 백테스트 모두 최하위권, 설명서 §56)",
           "rsi_vol": "2026-10-18 키움 운용 종료(두 기간 백테스트 모두 최하위권, 설명서 §56)",
           "h500_40_MKT": "옛 학습용 전략(운용 안 함 — 강도·AI 학습 데이터에만 남음)",
           "h252_40": "옛 학습용 전략(운용 안 함 — 강도·AI 학습 데이터에만 남음)",
           "h500_20": "옛 학습용 전략(운용 안 함 — 강도·AI 학습 데이터에만 남음)"}
