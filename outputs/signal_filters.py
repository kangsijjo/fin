# -*- coding: utf-8 -*-
"""
signal_filters.py — 신호 공통 거르개. (2026-10-31, 설명서 §91)

상한가 마감 신호 제외(skip_limitup)
  왜: §90 에서 '상한가 다음날 매수'가 −92%(10칸 CAGR)로 무너진 것을 보고 기존 전략을 다시 보니, 신호일에 **상한가로 마감**
      (등락률 ≥ +29.5%)한 종목의 신호는 거의 모든 전략에서 크게 나빴다(가중 평균 −4.24% vs 나머지 −0.04%, 중앙값 −8~−16%).
      다만 빼고 돌린 10칸 성적은 전략마다 갈려(좋아짐: 52주·h52w_for3d·for_high20·macd / 나빠짐: 500d_40·cred_dec·stoch)
      전략 공통 강제가 아니라 **전략별 선택**(설정 칸 skip_limitup) + 토요일 탐색이 해마다 검증해 권장/유지를 알려 준다.
  판정 = 신호일 일봉의 등락률(KRX 기준가 대비, change_pct) ≥ LIMIT_UP_PCT. 다음날 시가가 상한가인 경우는
      이미 '못 산 것'으로 공통 처리(_swing_base.UPPER_LIMIT_SKIP_PCT)되니 이것과는 별개.
쓰는 곳: kiwoom_trader / kis_trader cmd_buy(신호 읽은 직후), ai_paper_trader(가상 포트 전략 행), setting_sweep(탐색 축),
         period_backtest(--skip-limitup, 모든 줄).
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
LIMIT_UP_PCT = 29.5

_CACHE = {}


def limitup_codes(signal_date, base=None):
    """그 신호일에 상한가로 마감한 종목코드 집합 — macro_data/daily/{YYYYMMDD}.csv 의 change_pct 로. 파일이 없으면 None."""
    d = str(signal_date).replace("-", "")[:8]
    base = base or HERE
    key = (base, d)
    if key in _CACHE:
        return _CACHE[key]
    p = os.path.join(base, "macro_data", "daily", f"{d}.csv")
    if not os.path.exists(p):
        return None
    try:
        import pandas as pd
        df = pd.read_csv(p, dtype={"code": str}, encoding="utf-8-sig")
        col = "change_pct" if "change_pct" in df.columns else ("등락률" if "등락률" in df.columns else None)
        if col is None:
            return None
        chg = pd.to_numeric(df[col], errors="coerce")
        out = set(df.loc[chg >= LIMIT_UP_PCT, "code"].astype(str).str.zfill(6))
    except Exception:
        return None
    _CACHE[key] = out
    return out


def drop_limitup(sigs, skip_strategies, log=print, label=""):
    """신호 목록(dict: code·strategy·signal_date)에서 skip_strategies(라이브 이름)에 속하고 신호일 상한가 마감인 것을 뺀다.
    → (남긴 목록, 뺀 목록). 그날 일봉 파일이 없으면 빼지 않고 남긴다(fail-open — 거르개 고장이 매수 전면 중단이 되지 않게)."""
    skip = set(skip_strategies or [])
    if not skip or not sigs:
        return list(sigs or []), []
    kept, dropped, missing = [], [], set()
    for s in sigs:
        st = str(s.get("strategy", ""))
        if st not in skip:
            kept.append(s)
            continue
        d = str(s.get("signal_date", "")).replace("-", "")[:8]
        lu = limitup_codes(d)
        if lu is None:
            missing.add(d)
            kept.append(s)
            continue
        if str(s.get("code", "")).zfill(6) in lu:
            dropped.append(s)
        else:
            kept.append(s)
    if dropped:
        log(f"[{label or 'filter'}] 상한가 마감 신호 제외 {len(dropped)}건(전략 설정): "
            + ", ".join(f"{s.get('code')}({s.get('strategy')})" for s in dropped[:10]))
    if missing:
        log(f"[{label or 'filter'}][warn] 신호일 일봉 없음({', '.join(sorted(missing))}) — 상한가 거르개 건너뜀(그대로 진행)")
    return kept, dropped


def limitup_entry_keys(df):
    """백테스트용: {(code, 진입일)} — 그 종목의 진입일 직전 거래일(=신호일) 등락률 ≥ LIMIT_UP_PCT 인 진입.
    df: code·date·change_pct 가 있는 일봉 표. 한 번 만들어 여러 전략에 같이 쓴다."""
    import pandas as pd
    d = df[["code", "date", "change_pct"]].copy()
    d["code"] = d["code"].astype(str).str.zfill(6)
    d["date"] = d["date"].astype(str)
    d = d.sort_values(["code", "date"])
    d["_next"] = d.groupby("code", sort=False)["date"].shift(-1)
    m = d.dropna(subset=["_next"])
    lu = pd.to_numeric(m["change_pct"], errors="coerce") >= LIMIT_UP_PCT
    return set(zip(m.loc[lu, "code"], m.loc[lu, "_next"]))


def limitup_flags(df, codes, entry_dates, keys=None):
    """매매(code, 진입일)마다 신호일 상한가 마감 여부 리스트. keys: limitup_entry_keys(df) 를 이미 만들었으면 넘김."""
    keys = limitup_entry_keys(df) if keys is None else keys
    return [(str(c).zfill(6), str(e)) in keys for c, e in zip(codes, entry_dates)]
