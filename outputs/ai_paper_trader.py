"""
ai_paper_trader.py — AI/강도 '가상매매 실행' (순수 장부 시뮬 — 브로커/모의계좌 미사용)

매일 18:31 신호 생성 직후(run_kis_signal.bat 훅) 실행. 신호 로그에서 전체 이력을
매번 결정론적으로 재계산(strategy_lab 방식 — 상태파일 없음 = 드리프트/복구 걱정 없음).

두 포트폴리오(각 10슬롯 · 시작 1,000만 · 2026-06-30~):
  ai       : 당일 신호를 ai_prob(meta_model_v4 big-win 확률) 내림차순으로 슬롯 채움 (필터 없음)
  strength : 동일 신호를 score_ic(IC 강도) 내림차순 + **5.7 미만 스킵**(라이브 키움 매수필터와
             동일 — 슬롯 1순위여도 임계 미만이면 미진입, 2026-07-03 사용자 확인 반영.
             2026-07-17 잣대 교체에 맞춰 6.0→5.7 재매핑 — kiwoom_trader.MIN_STRENGTH_SCORE 와 동기)
→ 실제 모의계좌(균등배분·거래대금순)와 3자 비교해 'AI/강도 선별이 돈이 되는가'를 실측.

규약(paper_audit 과 동일 — 검증 패널과 비교 가능):
  진입 = 신호일 종가(entry_price_close), 청산 = 진입 다음 거래일부터 holding일째 종가,
  비용 = 왕복 0.33%(슬리피지 0.15×2 + 수수료 0.015×2). 익절/손절 없음(선별 효과만 분리).
  예산 = 남은현금/남은빈슬롯(라이브와 동일 균등분배), 동일종목 중복보유 스킵.

출력: db/ai_paper_result.json (대시보드 강도매매 탭 '가상매매 실행' 패널이 읽음)
수동 실행: python ai_paper_trader.py
"""

import bisect
import glob
import json
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import pandas as pd  # noqa: E402

START    = "20260630"        # ai_prob 기록 시작일(차원버그 수정 이후) — 두 포트폴리오 공통
CAPITAL0 = 10_000_000
SLOTS    = 10
COST_PCT = 0.33              # 왕복
OUT_JSON = "./db/ai_paper_result.json"
STRENGTH_LOG = "./db/signal_strength_log.csv"


def load_signals():
    """키움+KIS 신호(START 이후) + 강도로그(score_ic/ai_prob) 병합."""
    frames = []
    for f in ("paper_signals.csv", "kis_paper_signals.csv"):
        if os.path.exists(f):
            d = pd.read_csv(f, dtype={"code": str})
            frames.append(d)
    if not frames:
        return pd.DataFrame()
    s = pd.concat(frames, ignore_index=True)
    s["signal_date"] = s["signal_date"].astype(str).str.replace("-", "")
    s = s[s["signal_date"] >= START].copy()
    s["code"] = s["code"].astype(str).str.zfill(6)
    s["holding_days"] = pd.to_numeric(s["holding_days"], errors="coerce").fillna(20).astype(int)
    s["entry_price_close"] = pd.to_numeric(s["entry_price_close"], errors="coerce")
    s = s[s["entry_price_close"] > 0].dropna(subset=["entry_price_close"])
    try:
        sl = pd.read_csv(STRENGTH_LOG, dtype={"code": str})
        sl["signal_date"] = sl["signal_date"].astype(str).str.replace("-", "")
        sl["code"] = sl["code"].astype(str).str.zfill(6)
        sl = sl[["signal_date", "code", "strategy", "score_ic", "ai_prob"]] \
            .drop_duplicates(["signal_date", "code", "strategy"], keep="last")
        s = s.merge(sl, on=["signal_date", "code", "strategy"], how="left")
    except Exception as e:
        print(f"[warn] 강도로그 병합 실패: {e}")
        s["score_ic"] = None
        s["ai_prob"] = None
    s["score_ic"] = pd.to_numeric(s["score_ic"], errors="coerce")
    s["ai_prob"] = pd.to_numeric(s["ai_prob"], errors="coerce")
    return s


def load_prices():
    """거래일 달력(전체) + START 이후 종가맵 {date: {code: close}}."""
    files = sorted(glob.glob("./macro_data/daily/*.csv"))
    cal_all = [os.path.basename(f)[:-4] for f in files
               if os.path.basename(f)[:-4].isdigit() and len(os.path.basename(f)[:-4]) == 8]
    cal = [d for d in cal_all if d >= START]
    closes = {}
    for d in cal:
        try:
            df = pd.read_csv(f"./macro_data/daily/{d}.csv", dtype={"code": str},
                             encoding="utf-8-sig")
            df["code"] = df["code"].astype(str).str.zfill(6)
            col = "close" if "close" in df.columns else "종가"
            closes[d] = dict(zip(df["code"], pd.to_numeric(df[col], errors="coerce")))
        except Exception:
            closes[d] = {}
    return cal_all, cal, closes


def _px(closes, d, code):
    v = closes.get(d, {}).get(code)
    return float(v) if v is not None and v == v and v > 0 else None


def simulate(sig, cal_all, cal, closes, rank_col, min_score=None):
    """rank_col(ai_prob | score_ic) 내림차순 슬롯 채움 시뮬 → 결과 dict.

    min_score: 이 값 미만 신호는 슬롯이 비어도 미진입(현금 보유) — 라이브 필터 재현용.
    """
    idx = {d: i for i, d in enumerate(cal_all)}
    cash = float(CAPITAL0)
    positions = {}     # code -> {name,strategy,entry_date,entry_px,invest,exit_idx,score}
    trades = []
    equity = []
    skipped_full = 0
    by_date = {d: g for d, g in sig.groupby("signal_date")}

    for d in cal:
        cur_i = idx[d]
        # 1) 만기 청산 — 청산 예정일 도달분(청산일 종가, 없으면 재개 후 첫 종가)
        # [2026-07-12] 탐색창 exit_idx+5 고정 → 오늘까지로 확장: 6거래일 이상 거래정지
        # 종목이 어떤 재계산에서도 같은 창만 훑어 영구 미청산(슬롯 좀비 + equity 원금
        # 고정)되던 것. 이제 거래 재개 첫 종가에 청산된다.
        for code in list(positions):
            p = positions[code]
            if cur_i < p["exit_idx"]:
                continue
            x = None
            for j in range(p["exit_idx"], min(cur_i + 1, len(cal_all))):
                x = _px(closes, cal_all[j], code)
                if x:
                    exit_d = cal_all[j]
                    break
            if not x:
                continue   # 아직 재개 종가 없음(거래정지 지속/상폐) — 재개 시 자동 청산
            net = (x / p["entry_px"] - 1) * 100 - COST_PCT
            proceeds = p["invest"] * (1 + net / 100)
            cash += proceeds
            trades.append({"code": code, "name": p["name"], "strategy": p["strategy"],
                           "entry_date": p["entry_date"], "exit_date": exit_d,
                           "net_pct": round(net, 2), "pnl": int(proceeds - p["invest"]),
                           "score": p["score"]})
            del positions[code]

        # 2) 신규 진입 — 당일 신호 rank_col 내림차순(라이브처럼 남은현금/남은빈슬롯 균등)
        g = by_date.get(d)
        if g is not None:
            if min_score is not None:
                # 라이브 필터 재현(2026-07-12 수정): 키움은 '기록 없는' 신호를 통과시킨다
                # (fail-open — 로거 장애가 매수 전면 중단으로 번지지 않게). 기존 시뮬은
                # notna 로 무기록을 전부 제외(fail-closed)해 방향이 라이브와 반대였음.
                # 무기록은 통과시키되 정렬 맨 뒤(NaN last) — 라이브의 tv순 폴백 근사.
                cand = g[(g[rank_col].isna()) | (g[rank_col] >= min_score)]
                cand = cand.sort_values(rank_col, ascending=False, na_position="last")
            else:
                # ai 포트는 ai_prob 자체가 비교 대상 지표라 무기록 제외(기존 유지)
                cand = g[g[rank_col].notna()].sort_values(rank_col, ascending=False)
            for _, r in cand.iterrows():
                free = SLOTS - len(positions)
                if free <= 0:
                    skipped_full += 1
                    continue
                code = r["code"]
                if code in positions:
                    continue
                invest = cash / free
                if invest < 10_000:
                    continue
                ep = float(r["entry_price_close"])
                ei = bisect.bisect_right(cal_all, d)             # 진입 다음 거래일 인덱스
                _sc = float(r[rank_col]) if pd.notna(r[rank_col]) else None   # 무기록 NaN 가드
                positions[code] = {
                    "name": str(r.get("name", "")), "strategy": str(r.get("strategy", "")),
                    "entry_date": d, "entry_px": ep, "invest": invest,
                    "exit_idx": ei + int(r["holding_days"]) - 1,
                    "score": round(_sc, 4) if _sc is not None else None,
                }
                cash -= invest

        # 3) 일별 MTM equity
        mtm = cash
        for code, p in positions.items():
            x = _px(closes, d, code)
            mtm += p["invest"] * (x / p["entry_px"] if x else 1.0)
        equity.append({"date": d, "equity": int(mtm)})

    last_d = cal[-1] if cal else START
    pos_out = []
    for code, p in positions.items():
        x = _px(closes, last_d, code)
        pnl_pct = round((x / p["entry_px"] - 1) * 100, 2) if x else 0.0
        pos_out.append({"code": code, "name": p["name"], "strategy": p["strategy"],
                        "entry_date": p["entry_date"], "score": p["score"],
                        "pnl_pct": pnl_pct})
    cur_eq = equity[-1]["equity"] if equity else CAPITAL0
    done = pd.DataFrame(trades)

    # 전략별 집계(2026-07-13) — 완료 거래 기준 건수/승률/평균순%/실현손익합
    by_strategy = []
    if len(done):
        for strat, g in done.groupby("strategy"):
            by_strategy.append({
                "strategy": str(strat),
                "n": int(len(g)),
                "win_pct": round(float((g["net_pct"] > 0).mean() * 100), 1),
                "avg_net": round(float(g["net_pct"].mean()), 2),
                "pnl": int(g["pnl"].sum()),
            })
        by_strategy.sort(key=lambda r: -r["pnl"])

    return {
        "equity": cur_eq,
        "ret_pct": round((cur_eq / CAPITAL0 - 1) * 100, 2),
        "cash": int(cash),
        "n_pos": len(positions),
        "n_trades": len(trades),
        "win_pct": (round(float((done["net_pct"] > 0).mean() * 100), 1) if len(done) else None),
        "avg_net": (round(float(done["net_pct"].mean()), 2) if len(done) else None),
        "skipped_full": skipped_full,
        "positions": sorted(pos_out, key=lambda r: -(r["score"] if r["score"] is not None else -999)),
        "recent_trades": trades[-10:][::-1],
        # 완료 매매내역 전체(최근순) — 대시보드 드롭다운용(2026-07-13). 표본이 작아 전량 포함.
        "trades": trades[::-1],
        "by_strategy": by_strategy,
        "equity_curve": equity[-60:],
    }


def main():
    sig = load_signals()
    if sig.empty:
        print("[ai_paper] 신호 없음 — 종료")
        return
    cal_all, cal, closes = load_prices()
    if not cal:
        print("[ai_paper] 가격 데이터 없음 — 종료")
        return
    print(f"[ai_paper] 신호 {len(sig)}건({START}~) / 거래일 {len(cal)}일 시뮬")
    out = {
        "updated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "start": START, "capital0": CAPITAL0, "slots": SLOTS, "cost_pct": COST_PCT,
        "portfolios": {
            "ai":       simulate(sig, cal_all, cal, closes, "ai_prob"),
            # min_score 는 kiwoom_trader.MIN_STRENGTH_SCORE 와 동기 유지(라이브 룰 쌍둥이)
            "strength": simulate(sig, cal_all, cal, closes, "score_ic", min_score=5.7),
        },
    }
    os.makedirs("./db", exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    for k, p in out["portfolios"].items():
        print(f"  [{k:8s}] 자산 {p['equity']:,} ({p['ret_pct']:+.2f}%)  "
              f"보유 {p['n_pos']}  완료 {p['n_trades']}  "
              f"승률 {p['win_pct'] if p['win_pct'] is not None else '-'}%")
    print(f"[saved] {OUT_JSON}")


if __name__ == "__main__":
    main()
