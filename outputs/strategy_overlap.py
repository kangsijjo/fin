# -*- coding: utf-8 -*-
"""
strategy_overlap.py — 전략끼리 '같은 종목을 사는 정도' 표(겹침 경고용). (2026-10-23, 설명서 §72)

왜: 모의계좌(키움·KIS)와 AI·강도 가상매매에서 전략을 고를 때, 서로 거의 같은 신호를 내는 전략을 같이 고르면
    (예: h52w_for3d_mkt 신호의 100% 가 for_high20_mkt 에도 나옴, 키움 52주 신고가가 KIS h52w_for3d_mkt 의 87%)
    다른 전략을 고른 것 같아도 실제로는 같은 종목을 겹쳐 산다. 대시보드 설정 화면이 이 표로 경고한다.

두 가지 겹침(방향 있음 — 'A 의 매매 중 몇 %가 B 와 겹치나')
  same : 같은 날 같은 종목으로 진입하는 신호 비율
  held : A 가 산 종목을 B 도 그 기간에 들고 있는 비율(계좌가 다르면 같은 종목을 동시에 들고 있게 됨)
기간: 최근 3년(약 750거래일), §64 새 계산 규칙의 백테스트 매매(진입 기준). 강도·슬롯 무관(신호 자체의 겹침).

사용
  python strategy_overlap.py                 # 전 전략 계산(이미 계산한 전략은 건너뜀) + 표 저장
  python strategy_overlap.py --fresh         # 캐시가 있어도 전부 새로 계산해 덮어씀(주간 파이프라인 [8])
  python strategy_overlap.py --names a,b     # 그 전략만 계산(메모리 작은 환경에서 나눠 돌릴 때)
  python strategy_overlap.py --matrix        # 계산된 전략들로 표만 다시 만듦
출력: db/strategy_overlap.json  (캐시: db/strategy_overlap_sets/*.pkl)
"""
import argparse
import gc
import glob
import json
import os
import pickle
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT = os.path.join(HERE, "db", "strategy_overlap.json")
SETS = os.path.join(HERE, "db", "strategy_overlap_sets")
WINDOW_DAYS = 750
KEEP_PCT = 10.0            # 이 % 미만 겹침은 표에 안 남김(파일 크기)


def _window():
    import period_backtest as P
    cal = P.trading_calendar()
    end_i = len(cal) - 1
    return cal, cal[max(0, end_i - WINDOW_DAYS)], cal[end_i]


def compute(names, log=print, fresh=False):
    """전략별 매매(종목·진입·청산) → 캐시. 전략마다 필요한 과거만 잘라 메모리를 아낀다."""
    import period_backtest as P
    import strategy_engine as E
    from strategies.daily_loader import load_macro_daily
    cal, start, end = _window()
    smap = {s.name: s for s in E.ALL_STRATEGIES}
    os.makedirs(SETS, exist_ok=True)
    todo = [n for n in names if n in smap and (fresh or not os.path.exists(os.path.join(SETS, f"{n}.pkl")))]
    if not todo:
        return
    df = load_macro_daily()
    df["date"] = df["date"].astype(str)
    i0 = cal.index(start)
    for n in todo:
        t0 = time.time()
        s = smap[n]
        lo = cal[max(0, i0 - P.warmup_days(s) - 5)]
        try:
            trs = s.backtest(df[df["date"] >= lo].reset_index(drop=True), E.DEFAULT_COSTS) or []
        except Exception as e:
            log(f"  [{n}] 실패: {type(e).__name__}: {str(e)[:120]}")
            continue
        rows = sorted({(str(t.code).zfill(6), int(str(t.entry_date)), int(str(t.exit_date)))
                       for t in trs if start <= str(t.entry_date) <= end})
        if not rows:                               # 데이터를 못 읽은 경우가 대부분 — 빈 결과로 캐시하지 않음
            log(f"  [{n}] 매매 0건 — 캐시하지 않음(보조 데이터 확인)")
            continue
        with open(os.path.join(SETS, f"{n}.pkl"), "wb") as f:
            pickle.dump({"start": start, "end": end, "rows": rows}, f)
        log(f"  {n:<26} 매매 {len(rows):>6}건 ({time.time() - t0:.0f}초)")
        del trs
        gc.collect()


def _held_pct(A, B):
    """A 매매 중 같은 종목을 B 가 겹치는 기간에 들고 있는 비율(%). A,B = {code: (entries ndarray, exits ndarray)}."""
    import numpy as np
    hit = tot = 0
    for code, (ea, xa) in A.items():
        tot += len(ea)
        b = B.get(code)
        if b is None:
            continue
        eb, xb_pm = b                               # B: 진입 정렬 + 청산 누적최대
        i = np.searchsorted(eb, xa, side="right") - 1   # A 청산일까지 진입한 B 중 마지막
        ok = i >= 0
        hit += int(np.count_nonzero(ok & (xb_pm[np.where(ok, i, 0)] >= ea)))
    return hit / tot * 100 if tot else 0.0


def build_matrix(log=print):
    import numpy as np
    data = {}
    for f in sorted(glob.glob(os.path.join(SETS, "*.pkl"))):
        with open(f, "rb") as fh:
            data[os.path.basename(f)[:-4]] = pickle.load(fh)
    if not data:
        log("계산된 전략 없음")
        return None
    sets, A, B = {}, {}, {}
    for n, d in data.items():
        rows = d["rows"]
        sets[n] = {(c, e) for c, e, _ in rows}
        g = {}
        for c, e, x in rows:
            g.setdefault(c, ([], []))
            g[c][0].append(e)
            g[c][1].append(x)
        A[n] = {c: (np.array(e), np.array(x)) for c, (e, x) in g.items()}
        bb = {}
        for c, (e, x) in A[n].items():
            o = np.argsort(e, kind="stable")
            bb[c] = (e[o], np.maximum.accumulate(x[o]))
        B[n] = bb
    names = sorted(data)
    same, held = {}, {}
    t0 = time.time()
    for a in names:
        for b in names:
            if a == b or not sets[a]:
                continue
            s = len(sets[a] & sets[b]) / len(sets[a]) * 100
            if s >= KEEP_PCT:
                same.setdefault(a, {})[b] = round(s, 1)
            h = _held_pct(A[a], B[b])
            if h >= KEEP_PCT:
                held.setdefault(a, {})[b] = round(h, 1)
    any_d = next(iter(data.values()))
    out = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "window": [any_d["start"], any_d["end"]],
           "n": {n: len(data[n]["rows"]) for n in names}, "same": same, "held": held,
           "note": "same=같은 날 같은 종목 진입 비율, held=A 가 산 종목을 B 도 그 기간에 보유한 비율(행 A 기준 %)"}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    log(f"[saved] {OUT} — 전략 {len(names)}개, 표 계산 {time.time() - t0:.0f}초")
    return out


PLACES = {"kiwoom": ("키움", "mock"), "kis": ("KIS", "mock"), "ai": ("AI 가상", "virtual"), "strength": ("강도 가상", "virtual")}
SAME_WARN = 50.0          # 같은 날 같은 종목 신호가 이 % 이상이면 '겹침'
SAME_STRONG = 80.0        # 이 % 이상이면 사실상 같은 전략


def warnings(places, matrix=None):
    """설정 화면용 겹침 경고. places = {"kiwoom": [엔진…], "kis": […], "ai": […], "strength": […]}
    → [{"level": warn|info, "places": [..], "strategies": [a, b], "msg": 문장}] (중요한 것부터)
    · 같은 전략이 키움·KIS 둘 다 → warn(두 계좌가 같은 종목을 동시에 삼)
    · 다른 전략이라도 같은 날 같은 종목 신호가 50%↑ → 계좌가 다르면 warn, 같은 계좌·가상이면 info
    · 모의계좌 전략이 가상매매에도 있으면 info(가상은 계좌 신호를 그대로 씀 — 비교 실험이면 정상)"""
    matrix = matrix if matrix is not None else (load() or {})
    same = matrix.get("same") or {}
    held = matrix.get("held") or {}
    out, seen, dup, both = [], set(), {}, []
    mock_set = set(places.get("kiwoom") or []) | set(places.get("kis") or [])

    def pct(a, b):
        return max(same.get(a, {}).get(b, 0.0), same.get(b, {}).get(a, 0.0))

    items = [(p, e) for p in PLACES for e in (places.get(p) or [])]
    for i, (p1, a) in enumerate(items):
        for p2, b in items[i + 1:]:
            l1, t1 = PLACES[p1]
            l2, t2 = PLACES[p2]
            key = tuple(sorted([(p1, a), (p2, b)]))
            if key in seen:
                continue
            seen.add(key)
            if a == b:
                if p1 == p2:
                    continue
                if t1 == t2 == "mock":
                    out.append({"level": "warn", "places": [p1, p2], "strategies": [a, b],
                                "msg": f"{a} 를 {l1}·{l2} 두 계좌에서 모두 운용 — 같은 날 같은 종목을 두 계좌가 동시에 삽니다(위험 2배)"})
                elif "mock" in (t1, t2):
                    vp = p2 if t1 == "mock" else p1
                    dup.setdefault(vp, []).append(a)          # 가상 포트폴리오별로 한 줄로 모음
                else:
                    both.append(a)
                continue
            s_ab, s_ba = same.get(a, {}).get(b, 0.0), same.get(b, {}).get(a, 0.0)
            pmax = max(s_ab, s_ba)
            if pmax < SAME_WARN:
                continue
            sub = (f"{a} 신호의 {s_ab:.0f}%가 {b} 에도 나옴" if s_ab >= s_ba else f"{b} 신호의 {s_ba:.0f}%가 {a} 에도 나옴")
            hb = max(held.get(a, {}).get(b, 0.0), held.get(b, {}).get(a, 0.0))
            if t1 == t2 == "mock" and p1 != p2:
                out.append({"level": "warn", "places": [p1, p2], "strategies": [a, b],
                            "msg": f"{l1} {a} ↔ {l2} {b}: {sub} — 두 계좌가 같은 종목을 자주 동시에 삽니다"
                                   + (f"(보유 기간 겹침 {hb:.0f}%)" if hb else "")})
            elif p1 == p2:
                out.append({"level": "warn" if pmax >= SAME_STRONG else "info", "places": [p1, p2], "strategies": [a, b],
                            "msg": f"{l1} 안 {a} ↔ {b}: {sub}"
                                   + (" — 사실상 같은 종목군" if pmax >= SAME_STRONG else " — 일부 겹침")
                                   + ("(위 순서 전략이 먼저 칸을 채움)" if t1 == "mock"
                                      else "(순위 높은 신호부터 담고 같은 종목은 한 번만 — 칸만 나눠 쓰는 셈)")})
            elif "mock" in (t1, t2):
                v_eng = b if t1 == "mock" else a
                if v_eng in mock_set:                      # 그 가상 전략은 이미 계좌에 있음 — 계좌 쪽 경고로 충분
                    continue
                out.append({"level": "info", "places": [p1, p2], "strategies": [a, b],
                            "msg": f"{l1} {a} ↔ {l2} {b}: {sub} — 가상매매가 계좌와 거의 같은 종목을 봄(새 정보가 적음)"})
    for vp, names in dup.items():
        names = sorted(set(names))
        out.append({"level": "info", "places": [vp], "strategies": names,
                    "msg": f"{PLACES[vp][0]}: 모의계좌와 같은 전략 {len(names)}개({', '.join(names)}) — 가상매매는 계좌 신호를 "
                           f"그대로 쓰므로 '고르는 방식'(AI 순·강도 순) 비교라면 정상"})
    if both:
        both = sorted(set(both))
        out.append({"level": "info", "places": ["ai", "strength"], "strategies": both,
                    "msg": f"AI·강도 가상 둘 다에 있는 전략 {len(both)}개({', '.join(both)}) — 같은 신호를 AI 순 vs 강도 순으로 비교(정상)"})
    out.sort(key=lambda w: (w["level"] != "warn", -len(w["msg"])))
    return out


def load():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", default="")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--matrix", action="store_true")
    a = ap.parse_args()
    if a.matrix:
        build_matrix()
        return 0
    import strategy_engine as E
    names = [x for x in a.names.split(",") if x] or [s.name for s in E.ALL_STRATEGIES]
    compute(names, fresh=a.fresh)
    if not a.names:
        build_matrix()
    return 0


if __name__ == "__main__":
    sys.exit(main())
