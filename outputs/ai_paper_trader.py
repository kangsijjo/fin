"""
ai_paper_trader.py — AI/강도 '가상매매 실행' (순수 장부 시뮬 — 브로커/모의계좌 미사용)

매일 18:31 신호 생성 직후(run_kis_signal.bat 훅) 실행. 신호 로그에서 전체 이력을
매번 결정론적으로 재계산(strategy_lab 방식 — 상태파일 없음 = 드리프트/복구 걱정 없음).

두 포트폴리오(각 10슬롯 · 시작 1,000만 · 2026-06-30~):
  ai       : 당일 신호를 AI 점수 내림차순으로 슬롯 채움 — [§85] 2026-11-02(ai_model.SINCE) 신호일부터 개선 AI
             score_ai(0~10, 같은 전략·같은 달 안 수익 순위 학습), 그 전은 옛 ai_prob(meta_model_v4 대박 확률) 기록 그대로
  strength : 동일 신호를 score_ic(IC 강도) 내림차순 + **5.7 미만 스킵**(라이브 키움 매수필터와
             동일 — 슬롯 1순위여도 임계 미만이면 미진입, 2026-07-03 사용자 확인 반영.
             2026-08-09 양 계좌 5.7 (8월 매매정지 실측 후 재조정) — kiwoom/kis MIN_STRENGTH_SCORE 와 동기)
→ 실제 모의계좌(균등배분·거래대금순)와 3자 비교해 'AI/강도 선별이 돈이 되는가'를 실측.

규약(paper_audit 과 동일 — 검증 패널과 비교 가능):
  진입 = 신호일 종가(entry_price_close), 청산 = 진입 다음 거래일부터 holding일째 종가,
  비용 = 왕복 0.33%(슬리피지 0.15×2 + 수수료 0.015×2). 익절/손절 없음(선별 효과만 분리).
  예산 = 남은현금/남은빈슬롯(라이브와 동일 균등분배), 동일종목 중복보유 스킵.

[2026-10-22 §68] 전략 설정(virtual_config) — 강도매매 탭에서 두 포트폴리오마다 전략·칸·기준·면제·손절·익절을 고름.
  설정은 '적용 신호일'별 이력 → 그날 유효한 설정으로 그날 신호를 처리(바꾸기 전 기록은 종전 규칙 그대로).
  설정 항목이 있는 날: 고른 전략 신호만 · 전략별 칸 상한 · 기준(AI 확률/강도) 미만 제외(면제 전략은 통과)
  · 손절/익절은 매일 종가로 판정해 그 종가에 청산(장부 시뮬은 종가만 씀) · 보유일은 산 날의 설정.
  모의계좌가 안 쓰는 전략의 신호는 virtual_signal.py 가 db/virtual_signals.csv 에 만든다.

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
# [2026-10-15] 라이브 강도 필터 면제 전략 — kis_trader.STRENGTH_EXEMPT ∪ kiwoom_trader.STRENGTH_EXEMPT
# 와 동기(테스트로 고정). strength 포트는 '라이브 룰 쌍둥이'라 면제도 똑같이 재현한다(정렬은 강도순, 임계 미적용).
# [2026-10-18] 키움 신고가 2종(high_500d_filt·high_52w_filt) 면제 추가(설명서 §56).
# [2026-10-21] live_config 단일 출처(대시보드 '모의매매 전략 설정') — 기본은 {"gc_for3d", "high_500d_filt", "high_52w_filt"}
import live_config as _LC
STRENGTH_EXEMPT = _LC.all_exempt()
STRENGTH_LOG = "./db/signal_strength_log.csv"
try:                                    # [§85] AI 포트가 개선 AI 점수(score_ai)로 바뀌는 신호일
    from ai_model import SINCE as AI_SINCE
except Exception:
    AI_SINCE = "20261102"


def load_signals():
    """키움+KIS 신호(START 이후) + 강도로그(score_ic/ai_prob) 병합."""
    frames = []
    for f in ("paper_signals.csv", "kis_paper_signals.csv", "db/virtual_signals.csv"):
        if os.path.exists(f):
            d = pd.read_csv(f, dtype={"code": str})
            frames.append(d)
    if not frames:
        return pd.DataFrame()
    s = pd.concat(frames, ignore_index=True)
    s["signal_date"] = s["signal_date"].astype(str).str.replace("-", "")
    s = s[s["signal_date"] >= START].copy()
    s["code"] = s["code"].astype(str).str.zfill(6)
    s = s.drop_duplicates(["signal_date", "code", "strategy"], keep="first")   # 가상 신호와 계좌 신호가 겹치면 계좌 것
    s["holding_days"] = pd.to_numeric(s["holding_days"], errors="coerce").fillna(20).astype(int)
    s["entry_price_close"] = pd.to_numeric(s["entry_price_close"], errors="coerce")
    s = s[s["entry_price_close"] > 0].dropna(subset=["entry_price_close"])
    try:
        sl = pd.read_csv(STRENGTH_LOG, dtype={"code": str})
        sl["signal_date"] = sl["signal_date"].astype(str).str.replace("-", "")
        sl["code"] = sl["code"].astype(str).str.zfill(6)
        for c in ("score_v2", "score_ai"):
            if c not in sl.columns:
                sl[c] = None
        sl = sl[["signal_date", "code", "strategy", "score_ic", "ai_prob", "score_v2", "score_ai"]] \
            .drop_duplicates(["signal_date", "code", "strategy"], keep="last")
        s = s.merge(sl, on=["signal_date", "code", "strategy"], how="left")
    except Exception as e:
        print(f"[warn] 강도로그 병합 실패: {e}")
        s["score_ic"] = None
        s["ai_prob"] = None
        s["score_v2"] = None
        s["score_ai"] = None
    for c in ("score_ic", "ai_prob", "score_v2", "score_ai"):
        s[c] = pd.to_numeric(s[c], errors="coerce")
    # [§91] 신호일 상한가 마감 여부 — 설정에서 그 전략의 '상한가 마감 신호 제외'를 켠 포트는 담지 않음(_enter_configured)
    try:
        import signal_filters as _SF
        _lu = {d: (_SF.limitup_codes(d) or set()) for d in s["signal_date"].unique()}
        s["limitup"] = [c in _lu.get(d, set()) for c, d in zip(s["code"], s["signal_date"])]
    except Exception:
        s["limitup"] = False
    # [§85] AI 포트 순위 값: AI_SINCE 신호일부터 개선 AI 점수(0~10), 그 전은 옛 AI 확률(ai_prob) 그대로 — 과거 장부가 안 바뀐다.
    #   (과거 신호를 새 모델로 다시 채점하면 그 시기 매매가 학습에 들어 있어 미래를 본 셈이 된다)
    s["ai_score"] = s["score_ai"].where(s["signal_date"] >= AI_SINCE, s["ai_prob"])
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


def _port_cfg(history, port, d):
    """그 신호일의 포트폴리오 설정 → (None=종전 규칙, legacy 항목) 또는 (설정 dict, None)."""
    if not history:
        return None, None
    import virtual_config as VC
    e = VC.entry_for(history, d)
    if e is None or e.get("legacy"):
        return None, e
    return dict(e["portfolios"][port], _effective=e.get("effective", "")), None


def simulate(sig, cal_all, cal, closes, rank_col, min_score=None, history=None, port=None, legacy_timeline=None):
    """rank_col(ai_prob | score_ic) 내림차순 슬롯 채움 시뮬 → 결과 dict.

    min_score: 이 값 미만 신호는 슬롯이 비어도 미진입(현금 보유) — 라이브 필터 재현용(종전 규칙).
    history/port: 가상매매 설정 이력(virtual_config) — 설정이 있는 신호일은 그 설정(전략·칸·기준·면제·손절·익절)으로.
    legacy_timeline: 종전 규칙 구간의 강도 기준·면제를 '그날 모의계좌가 쓰던 값'으로(virtual_config.live_rules_timeline).
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
            # [2026-10-22] 설정의 손절·익절 — 산 다음 거래일부터 매일 종가로 판정, 그 종가에 청산
            if cur_i < p["exit_idx"] and (p.get("stop") is not None or p.get("take") is not None) \
                    and d > p["entry_date"]:
                x = _px(closes, d, code)
                if x:
                    r_ = x / p["entry_px"] - 1
                    why = ("손절" if p.get("stop") is not None and r_ <= p["stop"] / 100 else
                           "익절" if p.get("take") is not None and r_ >= p["take"] / 100 else None)
                    if why:
                        net = r_ * 100 - COST_PCT
                        proceeds = p["invest"] * (1 + net / 100)
                        cash += proceeds
                        trades.append({"code": code, "name": p["name"], "strategy": p["strategy"],
                                       "entry_date": p["entry_date"], "exit_date": d,
                                       "net_pct": round(net, 2), "pnl": int(proceeds - p["invest"]),
                                       "score": p["score"], "reason": why})
                        del positions[code]
                        continue
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
                           "score": p["score"], "reason": "만기"})
            del positions[code]

        # 2) 신규 진입 — 당일 신호 rank_col 내림차순(라이브처럼 남은현금/남은빈슬롯 균등)
        g = by_date.get(d)
        pc, legacy_e = _port_cfg(history, port, d)
        if g is not None and pc is not None:
            cash_box = [cash]
            _enter_configured(g, pc, port, d, cal_all, positions, cash_box)
            cash = cash_box[0]
            g = None
        if g is not None:
            ms_ = min_score
            ex_ = STRENGTH_EXEMPT
            by_ = {}
            if legacy_timeline:                          # 그날 모의계좌가 실제로 쓰던 기준·면제(§68)
                import virtual_config as VC
                ms_t, ex_t = VC.legacy_rules(legacy_timeline, d)
                ms_ = ms_t if min_score is not None else None
                ex_ = set(ex_t)
                by_ = VC.legacy_by(legacy_timeline, d) if min_score is not None else {}
            elif legacy_e is not None:                   # 설정 이력의 '종전 규칙'(첫 저장 때 고정한 값)
                ms_ = legacy_e.get("legacy_min_strength") if min_score is not None else None
                ex_ = set(legacy_e.get("legacy_exempt") or [])
            if ms_ is not None:
                # 라이브 필터 재현(2026-07-12 수정): 키움은 '기록 없는' 신호를 통과시킨다
                # (fail-open — 로거 장애가 매수 전면 중단으로 번지지 않게). 기존 시뮬은
                # notna 로 무기록을 전부 제외(fail-closed)해 방향이 라이브와 반대였음.
                # 무기록은 통과시키되 정렬 맨 뒤(NaN last) — 라이브의 tv순 폴백 근사.
                th_ = g["strategy"].map(lambda x: by_.get(x, ms_))          # [§71] 그날 계좌의 전략별 기준
                cand = g[(g[rank_col].isna()) | (g[rank_col] >= th_)
                         | (g["strategy"].isin(ex_))]
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
    # [2026-10-09] 중앙값·최대기여일·그날 제외 평균 추가(§44). AI×rsi_reversal 이
    # 평균 +15.94% 로 보였는데 17건 중 7건이 7/29(폭락 바닥) 하루 진입분이었고 그날
    # 제외 시 평균이 크게 낮았다 — 평균만 보면 '하루 이벤트'를 '전략 실력'으로 오독한다.
    by_strategy = []
    if len(done):
        for strat, g in done.groupby("strategy"):
            day = g.groupby("entry_date")["pnl"].sum()
            top_day = str(day.abs().idxmax())
            abs_sum = float(day.abs().sum())
            rest = g[g["entry_date"] != top_day]
            by_strategy.append({
                "strategy": str(strat),
                "n": int(len(g)),
                "win_pct": round(float((g["net_pct"] > 0).mean() * 100), 1),
                "avg_net": round(float(g["net_pct"].mean()), 2),
                "med_net": round(float(g["net_pct"].median()), 2),
                "pnl": int(g["pnl"].sum()),
                "top_day": top_day,                                   # 손익 기여(절대값) 최대 진입일
                "top_day_n": int((g["entry_date"] == top_day).sum()),
                "top_day_pnl": int(day[top_day]),
                "top_day_share": round(abs(float(day[top_day])) / abs_sum * 100, 1) if abs_sum else None,
                "avg_ex_top": round(float(rest["net_pct"].mean()), 2) if len(rest) else None,
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


def _enter_configured(g, pc, port, d, cal_all, positions, cash_box):
    """설정 항목이 있는 날의 진입 — 고른 전략만, 기준(면제 통과), 순위 높은 순, 전략별 칸 상한·전체 10칸."""
    import live_config as LC
    rows = {LC.live_name(s["engine"]): s for s in pc["strategies"]}
    c = g[g["strategy"].isin(rows)].copy()
    if c.empty:
        return
    if port == "ai":
        c["_rk"] = c["ai_score"] if "ai_score" in c.columns else c["ai_prob"]   # [§85] AI_SINCE 부터 개선 AI 점수(0~10), 그 전 옛 AI 확률
    elif pc.get("strength_version", "v2") == "v2":      # [§82] 기본 v2
        c["_rk"] = c["score_v2"].where(c["score_v2"].notna(), c["score_ic"])   # v2 모델 없으면 v1 점수(라이브와 같음)
    else:
        c["_rk"] = c["score_ic"]
    ex = c["strategy"].map(lambda x: bool(rows[x].get("exempt")))
    thr = pc.get("min_score")
    # [§85] AI_SINCE 전에 저장한 AI 기준은 옛 확률 척도(0~1) — 새 점수(0~10)에 대면 뜻이 달라지므로 그 설정이 유지되는 동안은 기준 없음
    stale_ai = port == "ai" and d >= AI_SINCE and str(pc.get("_effective", "")) < AI_SINCE
    if stale_ai:
        thr = None
    # [§71] 전략별 기준(행 min_score) — 비우면 포트폴리오 기준, 그것도 비우면 기준 없음
    th = pd.to_numeric(c["strategy"].map(
        lambda x: thr if stale_ai or rows[x].get("min_score") is None else rows[x].get("min_score")), errors="coerce")
    above = c["_rk"].notna() & (th.isna() | (c["_rk"] >= th.fillna(-1e9)))
    if port == "ai":        # AI 확률이 없으면 비교 불가 → 제외(면제 전략만 통과)
        ok = (c["_rk"].notna() & above) | ex
    else:                   # 강도는 종전처럼 무기록 통과(정렬 맨 뒤)
        ok = c["_rk"].isna() | above | ex
    if "limitup" in c.columns:   # [§91] 전략 행에서 '상한가 마감 신호 제외'를 켰으면 그 전략의 상한가 마감일 신호는 뺀다
        skip_lu = c["strategy"].map(lambda x: bool(rows[x].get("skip_limitup"))) & c["limitup"].fillna(False).astype(bool)
        ok = ok & ~skip_lu
    pool = pc.get("slot_mode") == "pool"
    if pool and port != "ai":    # [§75] 칸 없음: 전략마다 점수 척도가 달라 '그 전략 안 순위(백분위)'로 비교
        import strength_model as SM
        ver = pc.get("strength_version", "v2")
        c["_rk"] = [SM.rank_pct(rows[st]["engine"], st, v if v == v else None, ver)
                    for st, v in zip(c["strategy"], c["_rk"])]
        c["_rk"] = pd.to_numeric(c["_rk"], errors="coerce")
    c = c[ok].sort_values("_rk", ascending=False, na_position="last")
    for _, r in c.iterrows():
        free = SLOTS - len(positions)
        if free <= 0:
            break
        code, st = r["code"], r["strategy"]
        if code in positions:
            continue
        row = rows[st]
        if not pool and sum(1 for p in positions.values() if p["strategy"] == st) >= int(row["slots"]):
            continue
        invest = cash_box[0] / free
        if invest < 10_000:
            continue
        ei = bisect.bisect_right(cal_all, d)
        _sc = float(r["_rk"]) if pd.notna(r["_rk"]) else None
        positions[code] = {"name": str(r.get("name", "")), "strategy": st, "entry_date": d,
                           "entry_px": float(r["entry_price_close"]), "invest": invest,
                           "exit_idx": ei + int(row["holding"]) - 1,
                           "score": round(_sc, 4) if _sc is not None else None,
                           "stop": row.get("stop"), "take": row.get("take")}
        cash_box[0] -= invest


def _config_view(history):
    """결과 JSON 에 넣을 설정 요약(포트폴리오별 지금 적용 중·예약)."""
    import virtual_config as VC
    if not history:
        return None
    today = pd.Timestamp.now().strftime("%Y%m%d")
    act = VC.entry_for(history, today)
    pend = sorted([e for e in history if e["effective"] > today], key=lambda e: e["effective"])
    return {"active_effective": act["effective"] if act else None, "active_legacy": bool(act and act.get("legacy")),
            "pending_effective": pend[-1]["effective"] if pend else None}


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
    import virtual_config as VC
    hist = VC.load().get("history") or None          # 없으면 종전 규칙(설정 기능 고장 시에도 종전대로)
    try:
        tl = VC.live_rules_timeline()
    except Exception as e:
        print(f"[warn] 모의계좌 설정 이력 복원 실패 — 지금 값으로 과거 계산: {e}")
        tl = None
    out = {
        "updated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "start": START, "capital0": CAPITAL0, "slots": SLOTS, "cost_pct": COST_PCT,
        "config": _config_view(hist),
        "portfolios": {
            "ai":       simulate(sig, cal_all, cal, closes, "ai_score", history=hist, port="ai", legacy_timeline=tl),
            # min_score 는 kiwoom_trader.MIN_STRENGTH_SCORE 와 동기 유지(라이브 룰 쌍둥이) — 설정 이력이 생기면 '종전 규칙' 구간만
            "strength": simulate(sig, cal_all, cal, closes, "score_ic",
                                 min_score=_LC.account("kiwoom")["min_strength"], history=hist, port="strength",
                                 legacy_timeline=tl),
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
