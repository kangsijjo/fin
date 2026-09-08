# -*- coding: utf-8 -*-
"""
kospi_beta_control.py - KOSPI 수급선 크로스오버의 '표본외 +9% CAGR'이 신호의 알파인지,
그냥 20일 롱보유 = 시장 베타인지 가르는 대조검정. (2026-09-08)

왜 필요한가
  notp 스윕에서 순수 크로스오버(무익절·무손절·20일 보유)가 표본내 -0.83% / 표본외 +1.01%,
  CAGR +9.4% 로 나왔다. 그런데 20일 롱보유는 본질적으로 KOSPI 롱 = 베타 노출이라,
  2024-2026 대형주 랠리를 탄 것일 수 있다. '신호가 무작위 진입을 이기는가?'를 봐야 알파다.

방법 (거래건과 무관, 종목-일 단위 전방수익률 직접 계산 - 빠르고 결정적)
  · 각 종목-일 t 의 20일 보유 순수익 = (close[t+20] / open[t+1] - 1)*100 - 비용(0.43%).
    진입=익일시가, 청산=20세션 후 종가. 크로스오버·유니버스 둘 다 동일 규칙 → 오프바이원 상쇄.
  · 크로스오버군 = signal=True. 유니버스군(베타) = 유동성(거래대금>=10억) 통과한 모든 종목-일.
  · 연도별 + 표본내(<20240908)/표본외(>=) 로 평균수익률 비교. 차이 = 신호의 타이밍 알파.
  · 추가: 표본외를 무작위로 유니버스에서 '같은 건수·같은 연분포'로 뽑은 매칭 대조도 1회.

읽는 법
  크로스오버 평균 ≈ 유니버스 평균 이면 +9% 는 베타(신호 무의미).
  크로스오버 평균 >> 유니버스 평균 이어야 타이밍 엣지 존재.
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from strategies.daily_loader import load_macro_daily
from strategies.supply_reversal import SupplyCrossoverStrategy

HOLD = 20
COST = 0.43           # DEFAULT_COSTS total_pct 와 동일(왕복 수수료+세금+슬리피지)
MIN_TV = 1_000_000_000
OOS = "20240908"


def fwd_ret(df):
    """종목별 20일 보유 순수익률(%) 컬럼 추가. 진입 익일시가, 청산 t+20 종가."""
    g = df.groupby("code", group_keys=False)
    nxt_open = g["open"].shift(-1)
    exit_close = g["close"].shift(-HOLD)
    # 익일시가/청산종가가 0·음수(거래정지·데이터결측)면 NaN → 무한대 방지, 평균서 제외
    nxt_open = nxt_open.where(nxt_open > 0)
    exit_close = exit_close.where(exit_close > 0)
    df["fret"] = (exit_close / nxt_open - 1.0) * 100.0 - COST
    return df


def bucket_stats(sub, label):
    n = len(sub)
    if n == 0:
        return f"{label:22s} n=0"
    return (f"{label:22s} n {n:7,} | 평균 {sub['fret'].mean():+.3f}% "
            f"| 중앙 {sub['fret'].median():+.2f}% | 승률 {100*(sub['fret']>0).mean():.1f}%")


def main():
    print("[control] KOSPI 로드 ...")
    df = load_macro_daily(start_date="20210101", data_dir="macro_data/daily_kospi").reset_index(drop=True)
    strat = SupplyCrossoverStrategy(take_profit_pct=None)   # 순수 크로스오버 신호만
    df = strat.signal_df(df)
    df = fwd_ret(df)

    df["year"] = df["date"].str[:4]
    df["oos"] = np.where(df["date"] >= OOS, "OOS", "IS")

    liquid = df["trading_value"] >= MIN_TV
    valid = df["fret"].notna() & liquid & (df["open"] > 0)
    xo = valid & (df["signal"] == True)   # noqa: E712

    print(f"[control] 유효 종목-일 {int(valid.sum()):,} | 크로스오버 신호 {int(xo.sum()):,}\n")

    print("==== 표본내/표본외: 크로스오버 vs 유니버스(베타) ====")
    for seg in ("IS", "OOS"):
        m = df["oos"] == seg
        u = df[valid & m]
        x = df[xo & m]
        print(bucket_stats(u, f"[{seg}] 유니버스(베타)"))
        print(bucket_stats(x, f"[{seg}] 크로스오버"))
        if len(u) and len(x):
            print(f"    → 알파(크로스오버-베타) = {x['fret'].mean()-u['fret'].mean():+.3f}%p")
        print()

    print("==== 연도별 (크로스오버 / 유니버스 / 알파) ====")
    for y in sorted(df["year"].unique()):
        m = df["year"] == y
        u = df[valid & m]; x = df[xo & m]
        if len(x) == 0 or len(u) == 0:
            continue
        a = x["fret"].mean() - u["fret"].mean()
        print(f"  {y}  크로스오버 {x['fret'].mean():+.3f}% (n {len(x):5,}) | "
              f"유니버스 {u['fret'].mean():+.3f}% | 알파 {a:+.3f}%p")

    # 매칭 무작위 대조: 표본외에서 크로스오버와 같은 연분포·같은 건수로 유니버스 무작위 추출 x30
    print("\n==== 표본외 매칭 무작위 대조(같은 건수·연분포, 30회 평균) ====")
    rng = np.random.default_rng(42)
    oos_mask = df["oos"] == "OOS"
    xo_oos = df[xo & oos_mask]
    xo_mean = xo_oos["fret"].mean()
    pool = df[valid & oos_mask]
    per_year_n = xo_oos.groupby("year").size().to_dict()
    means = []
    for _ in range(30):
        parts = []
        for y, k in per_year_n.items():
            cand = pool[pool["year"] == y]
            if len(cand) >= k:
                parts.append(cand.sample(n=k, random_state=int(rng.integers(1e9))))
        if parts:
            means.append(pd.concat(parts)["fret"].mean())
    means = np.array(means)
    print(f"  크로스오버 표본외 평균 {xo_mean:+.3f}%")
    print(f"  무작위 매칭 평균 {means.mean():+.3f}% (표준편차 {means.std():.3f}, "
          f"범위 {means.min():+.3f}~{means.max():+.3f})")
    pctl = 100.0 * (means < xo_mean).mean()
    print(f"  → 크로스오버가 무작위 분포의 {pctl:.0f}%ile. "
          f"{'베타 이상 아님(엣지 의심)' if pctl < 90 else '베타를 유의하게 상회'}")


if __name__ == "__main__":
    main()
