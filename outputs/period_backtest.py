# -*- coding: utf-8 -*-
"""
period_backtest.py — 기간 지정 백테스트 테스트기 (대시보드 백테스트 탭 '기간 백테스트') — 2026-10-18 신설

무엇을 하나
  선택한 전략들을 지정 기간으로 백테스트한다. **전략마다 10슬롯을 단독 사용**(§50·§52 와 같은 규약),
  엔진 규칙(다음날 시가 진입·보유일 만기·손절/익절/강도필터 없음·비용 0.245%), 일별 MTM 자본곡선.
  ① 실행 전 데이터 충분성 점검 → 부족하면 경고(대시보드 + 텔레그램)
     · 가격 데이터 범위 밖(시작 이전 = 오류 / 종료 이후 = 마지막 데이터일로 자름)
     · 전략별 워밍업(과거 N거래일 필요 — 500일 신고가면 500일)이 기간 시작 전에 없음
     · 보조 데이터(외국인·기관·개인 수급 / 신용잔고 / 공매도 거래비중) 수집 구간 밖
     · 기간이 너무 짧음
  ② 전략별: 매매수·승률·신호 평균·슬롯 실매매 평균·총수익·CAGR·MDD·샤프·시장 대비·연도별 수익
  ③ (선택) 운 검정 — Hansen SPA·StepM(arch) / PBO(CSCV) / Deflated Sharpe — 전략 5개 이상일 때
  진행 상황과 결과는 db/period_bt/{job}.json 에 실시간 기록(대시보드가 3초마다 읽음).

사용
  python period_backtest.py --start 20230101 --end 20251231 --names a,b,c --job JOBID [--luck]
  python period_backtest.py --check --start 20230101 --end 20251231 --names a,b   # 점검만(JSON 출력)
"""
from __future__ import annotations

import argparse
import gc
import glob
import itertools
import json
import math
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
# ※ import 시 os.chdir 하지 않는다 — 대시보드 서버가 전략 목록·워밍업 계산용으로 import 한다.
#   작업 디렉터리 변경은 main()(별도 프로세스)에서만.

import numpy as np
import pandas as pd

OUT_DIR = os.path.join(HERE, "db", "period_bt")
COVERAGE_JSON = os.path.join(OUT_DIR, "coverage.json")
SLOTS = 10
CAPITAL0 = 1e7
MIN_WINDOW = 20          # 이보다 짧으면 실행 거부(거래일)
SMALL_WINDOW = 60        # 이보다 짧으면 경고
MIN_TRADES = 30          # 전략별 완료 매매가 이보다 적으면 '표본 부족' 경고
KEEP_JOBS = 30

# 보조 데이터 요구사항(전략 클래스 → 데이터 키)
_AUX_LABEL = {"foreign": "외국인 수급", "inst": "기관 수급", "individual": "개인 순매수",
              "credit": "신용잔고", "short": "공매도 거래비중"}
_NEEDS = {
    "ForeignHighStrategy": ["foreign"], "High52wForeignStrategy": ["foreign"],
    "GcForeignStrategy": ["foreign"], "InstForeignStrategy": ["foreign", "inst"],
    "InstPullbackStrategy": ["inst"],
    "SupplyReversalStrategy": ["foreign", "inst", "individual"],
    "SupplyRatioStrategy": ["foreign", "inst", "individual"],
    "SupplyCrossoverStrategy": ["foreign", "inst", "individual"],
    "High52wCreditDecreaseStrategy": ["credit"],
    "High52wShortDecreaseStrategy": ["short"],
}


# ── 공용 ─────────────────────────────────────────────────────────────────────
def trading_calendar():
    """macro_data/daily 파일명 기준 거래일 목록(가격 데이터가 실제로 있는 날)."""
    return sorted(os.path.basename(f)[:8] for f in glob.glob(os.path.join(HERE, "macro_data", "daily", "*.csv"))
                  if os.path.basename(f)[:8].isdigit() and len(os.path.basename(f)) == 12)


def strategies_by_name():
    import strategy_engine as E
    return {s.name: s for s in E.ALL_STRATEGIES}, getattr(E, "LIVE_MAP", {})


def warmup_days(s) -> int:
    """신호가 나오려면 기간 시작 전에 필요한 과거 거래일 수(근사·보수적).
    전략 파라미터 중 '창 길이'의 최댓값 + 시장강세 게이트(60일) + 여유 5일."""
    v = vars(s)
    keys = ("lookback", "baseline", "high_period", "slow", "slow_ma", "ma_period", "period",
            "bb_period", "ma_days", "accum_days", "window", "rsi_period", "k_period", "vol_ma_days",
            "n_days")
    w = max([int(v[k]) for k in keys if isinstance(v.get(k), (int, float)) and v.get(k)] or [0])
    if v.get("squeeze_window"):
        w = max(w, int(v["squeeze_window"]) + int(v.get("bb_period") or 20))
    cls = type(s).__name__
    if cls == "PullbackMaStrategy":
        w = max(w, 60 + int(v.get("ma60_slope_days") or 5))
    if cls == "MaRibbonStrategy":
        w = max(w, 120)
    if cls == "FiveDayMomentumStrategy":
        w = max(w, 5)
    if not w:
        w = 260
    if v.get("use_mkt") or v.get("use_market_filter"):
        w += int(v.get("mkt_ma") or 60)
    return w + 5


def aux_needs(s):
    return _NEEDS.get(type(s).__name__, [])


def aux_coverage(cal, df=None, force=False):
    """보조 데이터 실제 수집 구간 {key: [시작, 끝]} — 가격 마지막 날짜가 같으면 캐시 사용."""
    try:
        cached = json.load(open(COVERAGE_JSON, encoding="utf-8"))
        if not force and cached.get("data_max") == cal[-1]:
            return cached["items"]
    except Exception:
        pass
    items = {}
    if df is None:
        from strategies.daily_loader import load_macro_daily
        df = load_macro_daily()
    for key, col in (("foreign", "foreign_net"), ("inst", "inst_net"), ("individual", "individual_net")):
        if col in df.columns:
            f = df[col].notna().groupby(df["date"]).mean()
            ok = f[f >= 0.5]
            items[key] = [str(ok.index.min()), str(ok.index.max())] if len(ok) else None
    sf = sorted(os.path.basename(p)[:8] for p in glob.glob(os.path.join(HERE, "macro_data", "shortfund_kosdaq", "*.csv")))
    items["short"] = [sf[0], sf[-1]] if sf else None
    try:
        import sqlite3
        from strategies.high52w_credit_decrease import _default_stock_db
        p = _default_stock_db()
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        a, b = con.execute("SELECT min(date), max(date) FROM credit_balance").fetchone()
        con.close()
        items["credit"] = [str(a).replace("-", "")[:8], str(b).replace("-", "")[:8]] if a else None
    except Exception:
        items["credit"] = None
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(COVERAGE_JSON, "w", encoding="utf-8") as f:
        json.dump({"data_max": cal[-1], "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
                   "items": items}, f, ensure_ascii=False)
    return items


def check_data(start, end, names, cal, cov, luck=False, strat_map=None):
    """데이터 충분성 점검 → (경고 목록, 실제 종료일, 실행 가능 여부).
    경고 = {"level": error|warn|info, "strategy": 이름|"", "msg": 문장}"""
    W = []
    add = lambda lv, st, m: W.append({"level": lv, "strategy": st, "msg": m})
    if not (re.fullmatch(r"\d{8}", str(start)) and re.fullmatch(r"\d{8}", str(end))):
        add("error", "", "날짜 형식은 YYYYMMDD 여야 합니다")
        return W, end, False
    if start >= end:
        add("error", "", f"시작일({start})이 종료일({end})보다 앞서야 합니다")
        return W, end, False
    if not cal:
        add("error", "", "가격 데이터(macro_data/daily)가 없습니다")
        return W, end, False
    if start < cal[0]:
        add("error", "", f"가격 데이터는 {cal[0]} 부터 있습니다 — 시작일을 그 이후로 정하세요")
        return W, end, False
    eff_end = end
    if end > cal[-1]:
        eff_end = cal[-1]
        add("warn", "", f"가격 데이터는 {cal[-1]} 까지입니다 — 종료일을 {cal[-1]} 로 줄여서 계산합니다")
    win = [d for d in cal if start <= d <= eff_end]
    if len(win) < MIN_WINDOW:
        add("error", "", f"기간 안 거래일이 {len(win)}일뿐입니다(최소 {MIN_WINDOW}일)")
        return W, eff_end, False
    if len(win) < SMALL_WINDOW:
        add("warn", "", f"기간 안 거래일 {len(win)}일 — 짧아서 결과가 우연에 크게 좌우됩니다")
    if not names:
        add("error", "", "전략을 하나 이상 고르세요")
        return W, eff_end, False
    if strat_map is None:
        strat_map, _ = strategies_by_name()
    unknown = [n for n in names if n not in strat_map]
    if unknown:
        add("error", "", f"알 수 없는 전략: {', '.join(unknown)}")
        return W, eff_end, False
    i0 = cal.index(win[0])
    for n in names:
        s = strat_map[n]
        w = warmup_days(s)
        if i0 < w:
            first_ok = cal[min(w, len(cal) - 1)]
            add("warn", n, f"과거 {w}거래일이 필요한데 가격 데이터가 {cal[0]} 부터라 "
                           f"{first_ok} 이전엔 신호가 나올 수 없습니다 — 그 구간 성과는 '현금'으로 계산됨")
        for k in aux_needs(s):
            c = (cov or {}).get(k)
            lab = _AUX_LABEL[k]
            if not c:
                add("warn", n, f"{lab} 데이터가 없습니다 — 신호가 나오지 않을 수 있습니다")
                W[-1]["collect"] = {"aux": k, "start": win[0], "end": win[-1]}
                continue
            # [2026-10-18] 기준은 '기간의 첫 거래일'(win[0]) — 종전 start(달력일) 비교는
            # 1/1(휴장)처럼 거래일이 아닌 시작일에서 데이터가 첫 거래일부터 있어도 오경보.
            if win[0] < c[0]:
                gap = [d for d in win if d < c[0]]
                add("warn", n, f"{lab} 데이터는 {c[0]} 부터입니다 — 그 이전 {len(gap)}거래일은 신호 없음")
                W[-1]["collect"] = {"aux": k, "start": gap[0], "end": gap[-1]}
            if eff_end > c[1]:
                gap = [d for d in win if d > c[1]]
                if len(gap) > 5:
                    add("warn", n, f"{lab} 데이터가 {c[1]} 에서 멈춰 있습니다 — 이후 {len(gap)}거래일은 신호 없음"
                                   f"(수집 중단 의심)")
                    W[-1]["collect"] = {"aux": k, "start": gap[0], "end": gap[-1]}
    if luck:
        if len(names) < 5:
            add("info", "", "운 검정은 전략 5개 이상일 때만 의미가 있어 생략합니다")
        elif len(win) < 80:
            add("info", "", "운 검정은 거래일 80일 이상일 때만 수행합니다 — 생략")
    ok = not any(x["level"] == "error" for x in W)
    return W, eff_end, ok


# ── 시뮬 ─────────────────────────────────────────────────────────────────────
def equity_curve(trades, price_map, cal, slots=SLOTS, capital=CAPITAL0):
    """capital_simulator.simulate_capital(MTM) 과 같은 규칙으로 **일별** 자본을 돌려준다.
    (같은 날 후보는 score 큰 순, 예산 = 현금/남은 빈 슬롯, 청산은 exit_date 도달 시 net_pct 적용,
     기간 마지막 날 미청산 포지션은 그날 종가로 평가.) 반환: (equity Series, 실제 진입 net 목록)"""
    eq = pd.Series(float(capital), index=cal, dtype=float)
    if not trades:
        return eq, []
    T = pd.DataFrame([{"e": str(t.entry_date), "x": str(t.exit_date), "c": str(t.code),
                       "p": float(t.entry_price), "n": float(t.net_pct),
                       "s": float(getattr(t, "score", 0) or 0)} for t in trades]).sort_values("e")
    by = {d: g.sort_values("s", ascending=False) for d, g in T.groupby("e")}
    cash, pos, taken = float(capital), [], []
    for d in cal:
        keep = []
        for p in pos:
            if p["x"] <= d:
                cash += p["v"] * (1 + p["n"] / 100)
            else:
                keep.append(p)
        pos = keep
        g = by.get(d)
        if g is not None:
            for r in g.itertuples():
                left = slots - len(pos)
                if left <= 0:
                    break
                v = cash / left
                if v <= 0:
                    break
                pos.append({"c": r.c, "x": r.x, "v": v, "p": r.p, "n": r.n})
                taken.append(r.n)
                cash -= v
        inv = 0.0
        for p in pos:
            px = price_map.get((p["c"], d))
            inv += p["v"] * (px / p["p"] if px and p["p"] > 0 else 1.0)
        eq[d] = cash + inv
    return eq, taken


def curve_stats(eq):
    r = eq.pct_change().fillna(0)
    T = len(eq)
    tot = (eq.iloc[-1] / eq.iloc[0] - 1) * 100 if eq.iloc[0] else 0.0
    years = max(T / 252, 1 / 252)
    cagr = ((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1) * 100 if eq.iloc[-1] > 0 else -100.0
    mdd = float(((eq - eq.cummax()) / eq.cummax() * 100).min())
    sh = float(r.mean() / r.std() * math.sqrt(252)) if r.std() > 0 else 0.0
    yearly, prev = {}, float(eq.iloc[0])
    for y, g in eq.groupby(eq.index.str[:4]):
        yearly[y] = round((float(g.iloc[-1]) / prev - 1) * 100, 2)
        prev = float(g.iloc[-1])
    return {"total_ret": round(tot, 2), "cagr": round(cagr, 2), "mdd": round(mdd, 2),
            "sharpe": round(sh, 2), "yearly": yearly}


# ── 운 검정 ──────────────────────────────────────────────────────────────────
def luck_test(R: pd.DataFrame, mkt: pd.Series):
    """R: 일수익(%) T×N. SPA·StepM(현금/시장), PBO(CSCV), DSR(시도수=N, 잡음분산 1/T 완화판)."""
    from scipy.stats import norm, skew, kurtosis
    out = {"n": int(R.shape[1]), "T": int(R.shape[0])}
    try:
        from arch.bootstrap import SPA, StepM
        for bname, b in (("cash", np.zeros(len(R))), ("market", mkt.values)):
            spa = SPA(-b, -R.values, block_size=20, reps=2000, seed=7)
            spa.compute()
            st = StepM(-b, -R.values, size=0.05, block_size=20, reps=2000, seed=7)
            st.compute()
            out[f"spa_{bname}"] = round(float(spa.pvalues["consistent"]), 3)
            out[f"stepm_{bname}"] = [str(R.columns[i]) for i in st.superior_models]
    except Exception as e:
        out["spa_error"] = f"{type(e).__name__}: {str(e)[:80]} (arch 설치 확인)"
    T, N = R.shape
    S = 16 if T >= 160 else 8
    blocks = np.array_split(np.arange(T), S)
    M = R.values
    lam, oos_neg = [], 0
    for comb in itertools.combinations(range(S), S // 2):
        a = M[np.concatenate([blocks[c] for c in comb])]
        o = M[np.concatenate([blocks[c] for c in range(S) if c not in comb])]
        si = a.mean(0) / (a.std(0) + 1e-12)
        so = o.mean(0) / (o.std(0) + 1e-12)
        b = int(np.argmax(si))
        w = ((so < so[b]).sum() + 1) / (N + 1)
        lam.append(math.log(w / (1 - w)))
        oos_neg += so[b] < 0
    out["pbo"] = round(float(np.mean(np.array(lam) <= 0)), 3)
    out["pbo_blocks"] = S
    out["is_best_oos_neg"] = round(oos_neg / len(lam), 3)
    sr = R.mean() / R.std().replace(0, np.nan)
    best = str(sr.idxmax())
    x = R[best].values / 100
    srd = float(x.mean() / x.std()) if x.std() > 0 else 0.0
    ge = 0.5772156649
    sr0 = math.sqrt(1 / T) * ((1 - ge) * norm.ppf(1 - 1 / max(N, 2)) + ge * norm.ppf(1 - 1 / (max(N, 2) * math.e)))
    den = math.sqrt(max(1e-12, 1 - skew(x) * srd + (kurtosis(x, fisher=False) - 1) / 4 * srd ** 2))
    out.update(best=best, best_sharpe=round(srd * math.sqrt(252), 2),
               luck_max_sharpe=round(sr0 * math.sqrt(252), 2),
               dsr=round(float(norm.cdf((srd - sr0) * math.sqrt(T - 1) / den)), 3))
    return out


# ── 작업(JSON) ───────────────────────────────────────────────────────────────
def _job_path(job):
    return os.path.join(OUT_DIR, f"{job}.json")


def _write(job, state):
    os.makedirs(OUT_DIR, exist_ok=True)
    p = _job_path(job)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, p)


def _cleanup():
    jobs = sorted(glob.glob(os.path.join(OUT_DIR, "pbt_*.json")), key=os.path.getmtime)
    for p in jobs[:-KEEP_JOBS]:
        try:
            os.remove(p)
        except Exception:
            pass


def _notify(text):
    try:
        import notifier
        notifier.safe_send(text)
    except Exception:
        pass


def run_job(job, start, end, names, luck):
    t0 = time.time()
    state = {"job": job, "status": "running", "params": {"start": start, "end": end, "names": names,
             "luck": luck}, "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "progress": [0, len(names)], "current": "", "warnings": [], "results": [],
             "market": None, "luck": None}
    _write(job, state)
    cal = trading_calendar()
    smap, live = strategies_by_name()
    from strategies.daily_loader import load_macro_daily
    state["current"] = "가격 데이터 로딩"
    _write(job, state)
    df = load_macro_daily()
    df["date"] = df["date"].astype(str)
    cov = aux_coverage(cal, df)
    warns, eff_end, ok = check_data(start, end, names, cal, cov, luck, smap)
    state["warnings"] = warns
    state["params"]["eff_end"] = eff_end
    if warns:
        _notify(f"⚠ [기간 백테스트] {start}~{end} 데이터 점검 경고 {len(warns)}건\n" +
                "\n".join(f"  · {w['strategy'] + ': ' if w['strategy'] else ''}{w['msg']}" for w in warns[:6]) +
                ("\n  …" if len(warns) > 6 else ""))
    if not ok:
        state.update(status="error", finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     error="데이터 점검 실패 — 경고 참고")
        _write(job, state)
        return
    win = [d for d in cal if start <= d <= eff_end]
    i0 = cal.index(win[0])
    need = max(warmup_days(smap[n]) for n in names)
    # 종료일 뒤 45거래일까지 남긴다 — 엔진은 청산가가 있어야 매매를 만들므로, 종료일에서 자르면
    # 기간 막바지 진입분이 통째로 사라져 마지막 몇 주가 '현금'으로 계산된다. 기간 안 진입만 집계하고,
    # 종료일에 미청산이면 그날 종가로 평가(자본곡선은 종료일에서 끝남).
    i1 = cal.index(win[-1])
    df = df[(df["date"] >= cal[max(0, i0 - need - 5)]) &
            (df["date"] <= cal[min(len(cal) - 1, i1 + 45)])].reset_index(drop=True)
    m = df[df["date"].isin(win)].groupby("date")["change_pct"].mean().reindex(win).fillna(0)
    meq = (1 + m / 100).cumprod() * CAPITAL0
    state["market"] = {"name": "시장(코스닥 유니버스 동일가중)", **curve_stats(meq)}
    from capital_simulator import build_price_lookup
    import strategy_engine as E
    R = {}
    for k, n in enumerate(names, 1):
        state["current"] = n
        state["progress"] = [k - 1, len(names)]
        _write(job, state)
        s = smap[n]
        row = {"strategy": n, "live": live.get(n, ""), "warmup": warmup_days(s)}
        try:
            trs = s.backtest(df.copy(), E.DEFAULT_COSTS) or []
            gc.collect()
            tw = [t for t in trs if win[0] <= str(t.entry_date) <= eff_end]
            done = [t for t in tw if str(t.exit_date) <= eff_end]
            pm = {}
            if tw:
                codes = {str(t.code) for t in tw}
                pm, _ = build_price_lookup(df[df["code"].astype(str).isin(codes) & df["date"].isin(win)])
            eq, taken = equity_curve(tw, pm, win)
            del pm
            nets = [t.net_pct for t in done]
            row.update(n_trades=len(tw), n_done=len(done), n_open=len(tw) - len(done),
                       win=round(sum(x > 0 for x in nets) / len(nets) * 100, 1) if nets else None,
                       avg_net=round(float(np.mean(nets)), 2) if nets else None,
                       slot_n=len(taken), slot_avg=round(float(np.mean(taken)), 2) if taken else None,
                       **curve_stats(eq))
            row["vs_mkt"] = round(row["cagr"] - state["market"]["cagr"], 2)
            if len(done) < MIN_TRADES:
                state["warnings"].append({"level": "warn", "strategy": n,
                                          "msg": f"기간 내 완료 매매 {len(done)}건 — 표본 부족(결과 신뢰도 낮음)"})
            R[n] = eq.pct_change().fillna(0) * 100
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        state["results"].append(row)
        gc.collect()
    state["progress"] = [len(names), len(names)]
    if luck and len(R) >= 5 and len(win) >= 80:
        state["current"] = "운 검정"
        _write(job, state)
        try:
            state["luck"] = luck_test(pd.DataFrame(R), m)
        except Exception as e:
            state["luck"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    state.update(status="done", current="", finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 elapsed_sec=round(time.time() - t0))
    _write(job, state)
    _cleanup()
    okr = sorted([r for r in state["results"] if r.get("cagr") is not None], key=lambda r: -r["cagr"])
    lines = [f"  {r['strategy']}: CAGR {r['cagr']:+.1f}% / MDD {r['mdd']:.1f}%" for r in okr[:3]]
    lk = state.get("luck") or {}
    _notify(f"📊 [기간 백테스트 완료] {start}~{eff_end} · {len(names)}전략 · {state['elapsed_sec']}초\n"
            f"  시장 CAGR {state['market']['cagr']:+.1f}%\n" + "\n".join(lines) +
            (f"\n  운 검정: SPA(현금) p={lk.get('spa_cash')} · PBO {lk.get('pbo')} · DSR {lk.get('dsr')}"
             if lk and "pbo" in lk else "") +
            (f"\n  ⚠ 경고 {len(state['warnings'])}건 — 대시보드 확인" if state["warnings"] else ""))


def main():
    os.chdir(HERE)                       # 전략·로더가 상대경로(./macro_data …)를 쓴다 — 별도 프로세스에서만
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--names", default="")
    ap.add_argument("--job", default=None)
    ap.add_argument("--luck", action="store_true")
    ap.add_argument("--check", action="store_true", help="데이터 점검만 하고 JSON 출력")
    a = ap.parse_args()
    names = [n.strip() for n in a.names.split(",") if n.strip()]
    if a.check:
        cal = trading_calendar()
        cov = aux_coverage(cal)
        smap, _ = strategies_by_name()
        warns, eff_end, ok = check_data(a.start, a.end, names, cal, cov, a.luck, smap)
        win = [d for d in cal if a.start <= d <= eff_end]
        print("PBT_CHECK " + json.dumps({"ok": ok, "warnings": warns, "eff_end": eff_end,
                                         "n_days": len(win), "data": [cal[0], cal[-1]] if cal else None,
                                         "coverage": cov}, ensure_ascii=False))
        return
    job = a.job or datetime.now().strftime("pbt_%Y%m%d_%H%M%S")
    try:
        run_job(job, a.start, a.end, names, a.luck)
    except Exception as e:
        try:
            st = json.load(open(_job_path(job), encoding="utf-8"))
        except Exception:
            st = {"job": job}
        st.update(status="error", error=f"{type(e).__name__}: {str(e)[:200]}",
                  finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        _write(job, st)
        _notify(f"🚨 [기간 백테스트] 실행 실패 — {type(e).__name__}: {str(e)[:120]}")
        raise


if __name__ == "__main__":
    main()
