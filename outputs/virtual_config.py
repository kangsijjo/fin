# -*- coding: utf-8 -*-
"""
virtual_config.py — AI·강도 '가상매매'(ai_paper_trader) 전략 설정. (2026-10-22, 설명서 §68)

왜: 강도매매 탭의 두 가상 포트폴리오(AI 확률순 / 강도순)는 지금까지 '모의계좌 두 계좌의 신호 전부'를
    10칸 공유로 담는 고정 규칙이었다. 사용자가 모의계좌처럼 전략·슬롯·기준·면제·손절·익절을 고를 수 있게 한다.
    실계좌(키움·KIS)는 건드리지 않는다 — 설정 파일도 따로(db/virtual_config.json), 트레이더는 이 파일을 읽지 않는다.

적용 시점(사용자 결정): **다음 거래일 신호부터**. 가상매매는 매일 2026-06-30 부터 장부를 다시 계산하므로,
    설정을 날짜별 이력으로 남기고 그날 유효한 설정으로 그날 신호를 처리한다 → 바꾸기 전 기록은 그대로.
    · 거래일 18:25 전에 저장 → 오늘 신호부터 / 그 뒤·휴장일 → 다음 거래일 신호부터
    · 이미 산 종목은 산 날의 규칙(보유일·손절·익절)으로 끝까지 간다.
    · 첫 저장 때 '종전 규칙'(모의계좌 신호 전부·10칸 공유) 항목을 이력 맨 앞에 둔다. 종전 규칙 구간의 강도 기준·면제는
      **그날 모의계좌가 실제로 쓰던 값**(live_rules_timeline — live_config 백업으로 복원)이라 나중에 바꿔도 과거가 안 바뀐다.

포트폴리오 설정: {"min_score": 숫자|None, "strength_version": v1|v2, "strategies": [{engine, slots, exempt, stop, take, holding, min_score}]}
    전략 행의 min_score(2026-10-23 §71) = 그 전략만의 기준(비우면 포트폴리오 기준).
    ai       : min_score = AI 확률 기준(0~1, 비우면 기준 없음). 순서는 AI 확률 높은 순. 면제 = 기준과 상관없이 후보.
    strength : min_score = 강도 기준(0~10, 비우면 기준 없음). 순서는 강도(v1 score_ic / v2 score_v2) 높은 순.
"""
import copy
import json
import os
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PORTFOLIOS = ("ai", "strength")
LABEL = {"ai": "AI 가상매매", "strength": "강도 가상매매"}
SLOTS_TOTAL = 10
MAX_STRATEGIES = 6
CUTOFF = "1825"            # 거래일 이 시각 전 저장 → 오늘 신호부터 적용
LEGACY_DATE = "00000000"


def config_path():
    return os.getenv("VIRTUAL_CONFIG_PATH") or os.path.join(HERE, "db", "virtual_config.json")


def _is_trading(d):
    try:
        from market_calendar import is_trading_day
        return is_trading_day(d)
    except Exception:
        return d.weekday() < 5


def effective_date(now=None):
    """저장하면 어느 신호일부터 적용되나(YYYYMMDD)."""
    now = now or datetime.now()
    d = now.date()
    if _is_trading(d) and now.strftime("%H%M") < CUTOFF:
        return d.strftime("%Y%m%d")
    d += timedelta(days=1)
    while not _is_trading(d):
        d += timedelta(days=1)
    return d.strftime("%Y%m%d")


def load():
    """{"version":1, "history":[...]} — 파일 없음/깨짐이면 history 빈 목록(= 종전 규칙)."""
    p = config_path()
    if not os.path.exists(p):
        return {"version": 1, "history": []}
    try:
        cfg = json.load(open(p, encoding="utf-8"))
        h = cfg.get("history")
        if not isinstance(h, list):
            raise ValueError("history 없음")
        bad = [e for e in h if not e.get("legacy") and validate(e.get("portfolios"))]
        if bad:
            raise ValueError("검증 실패 항목 있음")
        return cfg
    except Exception as e:
        return {"version": 1, "history": [], "_fallback": f"가상매매 설정 파일 오류로 종전 규칙 사용: {type(e).__name__}"}


def entry_for(history, date):
    """그 신호일에 유효한 이력 항목(없으면 None = 종전 규칙)."""
    cur = None
    for e in sorted(history or [], key=lambda x: x["effective"]):
        if e["effective"] <= str(date):
            cur = e
    return cur


def legacy_snapshot():
    """종전 규칙의 현재 값(강도 기준·면제) — 첫 저장 때 고정."""
    import live_config as LC
    cfg = LC.load()
    return {"effective": LEGACY_DATE, "legacy": True, "saved": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "legacy_min_strength": float(LC.account("kiwoom", cfg)["min_strength"]),
            "legacy_exempt": sorted(LC.all_exempt(cfg))}


def live_rules_timeline():
    """모의계좌 설정이 언제 무엇이었나 → [(이 시각까지 유효 'YYYYMMDDHHMMSS'|None=지금까지, 키움 강도 기준, 면제 목록)].
    live_config 는 저장할 때마다 직전 설정을 live_config_backups/live_config_YYYYMMDD_HHMMSS.json 으로 복사해 둔다
    → 그 백업들이 '그 시각 전까지의 설정'. 종전 규칙 구간의 강도 가상매매가 그날 실제로 쓰인 기준을 따르게 한다
    (종전엔 지금 값으로 과거 전체를 다시 계산해, 키움 기준을 바꾸면 과거 성과가 통째로 바뀌었다)."""
    import glob
    import live_config as LC
    out = []
    bdir = os.path.join(os.path.dirname(LC.config_path()), "live_config_backups")
    for f in sorted(glob.glob(os.path.join(bdir, "live_config_*.json"))):
        ts = os.path.basename(f)[len("live_config_"):-len(".json")].replace("_", "")
        try:
            cfg = json.load(open(f, encoding="utf-8"))
            if LC.validate(cfg):
                continue
            cfg.setdefault("exit_rules", {})
            out.append((ts, float(LC.account("kiwoom", cfg)["min_strength"]), sorted(LC.all_exempt(cfg)), _by(LC, cfg)))
        except Exception:
            continue
    cur = LC.load()
    out.append((None, float(LC.account("kiwoom", cur)["min_strength"]), sorted(LC.all_exempt(cur)), _by(LC, cur)))
    return out


def _by(LC, cfg):
    """[§71] 그 설정의 전략별 강도 기준(라이브 이름 → 기준). 두 계좌에 같은 전략이 있으면 키움 값."""
    by = {}
    for a in ("kis", "kiwoom"):
        by.update(LC.account(a, cfg)["min_strength_by"])
    return by


def legacy_rules(timeline, signal_date, signal_time="183000"):
    """그 신호일(정기 신호 18:30) 에 모의계좌가 쓰던 (키움 강도 기준, 면제)."""
    t = str(signal_date) + signal_time
    for row in timeline:
        if row[0] is None or row[0] > t:
            return row[1], row[2]
    return timeline[-1][1], timeline[-1][2]


def legacy_by(timeline, signal_date, signal_time="183000"):
    """[§71] 그 신호일의 전략별 강도 기준(없으면 빈 dict → 계좌 기본값)."""
    t = str(signal_date) + signal_time
    for row in timeline:
        if row[0] is None or row[0] > t:
            return row[3] if len(row) > 3 else {}
    return timeline[-1][3] if len(timeline[-1]) > 3 else {}


def default_portfolios():
    """설정이 없을 때 편집기 초안 — 지금 모의계좌 두 계좌의 전략(최대 6개)을 슬롯 비율대로 10칸에 나눔."""
    import live_config as LC
    cfg = LC.load()
    rows = []
    for a in LC.ACCOUNTS:
        for s in cfg["accounts"][a]["strategies"]:
            if all(r["engine"] != s["engine"] for r in rows):
                rows.append({"engine": s["engine"], "slots": int(s["slots"]), "exempt": bool(s.get("exempt")),
                             "stop": None, "take": None, "holding": int(s["holding"])})
    rows = rows[:MAX_STRATEGIES]
    tot = sum(r["slots"] for r in rows) or 1
    for r in rows:
        r["slots"] = max(1, int(r["slots"] * SLOTS_TOTAL / tot))
    i = 0
    while sum(r["slots"] for r in rows) < SLOTS_TOTAL:          # 반올림 손실분은 앞에서부터 채움
        rows[i % len(rows)]["slots"] += 1
        i += 1
    while sum(r["slots"] for r in rows) > SLOTS_TOTAL:
        j = max(range(len(rows)), key=lambda k: rows[k]["slots"])
        rows[j]["slots"] -= 1
    ms = float(LC.account("kiwoom", cfg)["min_strength"])
    return {"ai": {"min_score": None, "strength_version": "v2", "strategies": copy.deepcopy(rows)},
            "strength": {"min_score": ms, "strength_version": "v2", "strategies": copy.deepcopy(rows)}}


def validate(ports, engine_names=None):
    errs = []
    if not isinstance(ports, dict):
        return ["portfolios 가 없습니다"]
    for p in PORTFOLIOS:
        P, lab = ports.get(p), LABEL[p]
        if not isinstance(P, dict):
            errs.append(f"{lab}: 설정이 없습니다")
            continue
        ms = P.get("min_score")
        hi = 1 if p == "ai" else 10
        if ms is not None and (not isinstance(ms, (int, float)) or isinstance(ms, bool) or not 0 <= ms <= hi):
            errs.append(f"{lab}: 기준은 0~{hi} 또는 비움")
        if P.get("strength_version", "v2") not in ("v1", "v2"):
            errs.append(f"{lab}: 강도 방식은 v1/v2")
        if P.get("slot_mode", "slots") not in ("slots", "pool"):
            errs.append(f"{lab}: 칸 방식은 slots/pool")
        S = P.get("strategies")
        if not isinstance(S, list) or not 1 <= len(S) <= MAX_STRATEGIES:
            errs.append(f"{lab}: 전략은 1~{MAX_STRATEGIES}개")
            continue
        seen, tot = set(), 0
        for s in S:
            e = s.get("engine")
            if not isinstance(e, str) or not e:
                errs.append(f"{lab}: 전략 이름이 비었습니다")
                continue
            if engine_names is not None and e not in engine_names:
                errs.append(f"{lab}: 알 수 없는 전략 {e}")
            if e in seen:
                errs.append(f"{lab}: {e} 가 두 번 들어 있습니다")
            seen.add(e)
            n = s.get("slots")
            if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                errs.append(f"{lab}: {e} 슬롯은 1 이상의 정수")
            else:
                tot += n
            st, tk = s.get("stop"), s.get("take")
            if st is not None and (not isinstance(st, (int, float)) or not -50 <= st <= -1):
                errs.append(f"{lab}: {e} 손절은 -1 ~ -50(%) 또는 비움")
            if tk is not None and (not isinstance(tk, (int, float)) or not 1 <= tk <= 300):
                errs.append(f"{lab}: {e} 익절은 1 ~ 300(%) 또는 비움")
            h = s.get("holding")
            if not isinstance(h, int) or isinstance(h, bool) or not 1 <= h <= 120:
                errs.append(f"{lab}: {e} 보유일 1~120")
            if not isinstance(s.get("exempt"), bool):
                errs.append(f"{lab}: {e} 면제 값 오류")
            sm = s.get("min_score")
            if sm is not None and (not isinstance(sm, (int, float)) or isinstance(sm, bool) or not 0 <= sm <= hi):
                errs.append(f"{lab}: {e} 전략별 기준은 0~{hi} 또는 비움")
        if tot != SLOTS_TOTAL and P.get("slot_mode", "slots") == "slots":   # [§75] 칸 없음이면 전략별 칸 수는 안 씀
            errs.append(f"{lab}: 슬롯 합이 {tot} — {SLOTS_TOTAL} 이어야 합니다")
    return errs


def _fmt(v):
    return "없음" if v is None else ("예" if v is True else "아니오" if v is False else f"{v:g}")


def describe_legacy(e):
    return (f"모의계좌 신호 전부 · 10칸 공유 · 손절/익절 없음 · AI 기준 없음 · 강도 기준 {e.get('legacy_min_strength', '-')}"
            f" · 면제 {', '.join(e.get('legacy_exempt') or []) or '없음'}")


def diff(old, new):
    """old: 이력 항목(None/legacy 가능) 의 portfolios, new: portfolios → 사람이 읽는 요약."""
    out = []
    for p in PORTFOLIOS:
        lab, n = LABEL[p], new[p]
        o = (old or {}).get(p) if old else None
        if o is None:
            out.append(f"{lab}: 종전 규칙 → " + ", ".join(
                f"{s['engine']}({s['slots']}칸{', 면제' if s['exempt'] else ''}"
                f"{', 기준 ' + format(s['min_score'], 'g') if s.get('min_score') is not None else ''})" for s in n["strategies"])
                + f" · 기준 {_fmt(n.get('min_score'))}" + (f" · 강도 {n.get('strength_version')}" if p == "strength" else ""))
            continue
        if o.get("min_score") != n.get("min_score"):
            out.append(f"{lab} 기준 {_fmt(o.get('min_score'))} → {_fmt(n.get('min_score'))}")
        if o.get("slot_mode", "slots") != n.get("slot_mode", "slots"):
            _m = {"slots": "전략별 칸", "pool": "칸 없음(전체 10종목)"}
            out.append(f"{lab} 칸 방식 {_m[o.get('slot_mode', 'slots')]} → {_m[n.get('slot_mode', 'slots')]}")
        if p == "strength" and o.get("strength_version", "v2") != n.get("strength_version", "v2"):
            out.append(f"{lab} 강도 방식 {o.get('strength_version', 'v2')} → {n.get('strength_version', 'v2')}")
        om = {s["engine"]: s for s in o["strategies"]}
        nm = {s["engine"]: s for s in n["strategies"]}
        for e in nm:
            if e not in om:
                out.append(f"{lab} + {e} ({nm[e]['slots']}칸)")
        for e in om:
            if e not in nm:
                out.append(f"{lab} − {e} (산 것은 만기까지)")
        for e in nm:
            if e in om:
                ch = [f"{ {'min_score': '기준'}.get(k, k)} {_fmt(om[e].get(k))}→{_fmt(nm[e].get(k))}"
                      for k in ("slots", "exempt", "stop", "take", "min_score") if om[e].get(k) != nm[e].get(k)]
                if ch:
                    out.append(f"{lab} {e}: " + ", ".join(ch))
    return out


def clean(ports, holding_map=None):
    """요청 값 → 저장 형식(보유일은 엔진 값)."""
    out = {}
    for p in PORTFOLIOS:
        P = (ports or {}).get(p) or {}
        ms = P.get("min_score")
        ms = None if ms in (None, "") else float(ms)
        rows = []
        for x in (P.get("strategies") or [])[:MAX_STRATEGIES + 1]:
            def num(v):
                return None if v in (None, "") else float(v)
            e = str(x.get("engine", ""))
            rows.append({"engine": e, "slots": int(x.get("slots") or 0), "exempt": bool(x.get("exempt")),
                         "stop": num(x.get("stop")), "take": num(x.get("take")), "min_score": num(x.get("min_score")),
                         "holding": int((holding_map or {}).get(e, x.get("holding") or 20))})
        out[p] = {"min_score": ms, "strength_version": str(P.get("strength_version") or "v2"),
                  "slot_mode": str(P.get("slot_mode") or "slots"), "strategies": rows}
    return out


def save(ports, note="", engine_names=None, now=None):
    """검증 → 이력에 '적용일' 항목 추가(같은 적용일이면 교체) → 원자 저장. → (설정, 변경 요약, 적용일)"""
    errs = validate(ports, engine_names)
    if errs:
        raise ValueError("; ".join(errs))
    now = now or datetime.now()
    cfg = load()
    cfg.pop("_fallback", None)
    hist = list(cfg.get("history") or [])
    if not hist:
        hist.append(legacy_snapshot())
    eff = effective_date(now)
    prev = entry_for(hist, eff)                       # 이번 저장이 없었으면 그날 적용됐을 설정(예약분 포함)
    changes = diff(None if (prev is None or prev.get("legacy")) else prev["portfolios"], ports)
    hist = [e for e in hist if e["effective"] != eff and not (e["effective"] > eff)]   # 같은 날·더 뒤 예약분 교체
    hist.append({"effective": eff, "saved": now.strftime("%Y-%m-%d %H:%M:%S"), "note": note or "",
                 "portfolios": copy.deepcopy(ports)})
    cfg = {"version": 1, "updated": now.strftime("%Y-%m-%d %H:%M:%S"), "history": hist}
    p = config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        try:
            import shutil
            bdir = os.path.join(os.path.dirname(p), "virtual_config_backups")
            os.makedirs(bdir, exist_ok=True)
            shutil.copy2(p, os.path.join(bdir, f"virtual_config_{now:%Y%m%d_%H%M%S}.json"))
        except OSError:
            pass
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return cfg, changes, eff


def state(today=None):
    """화면용: 포트폴리오별 (지금 적용 중 항목, 예약 항목)."""
    today = today or datetime.now().strftime("%Y%m%d")
    hist = load().get("history") or []
    active = entry_for(hist, today)
    pending = [e for e in hist if e["effective"] > today]
    return active, (sorted(pending, key=lambda e: e["effective"])[-1] if pending else None)


def strategy_usage(today=None):
    """[§79] 전략(엔진 이름)이 지금 어디에 쓰이는지 → {엔진: [{place, label, kind, slots, live}]}.
    place: kiwoom|kis(모의계좌, 실제 주문) / ai|strength(가상매매, 주문 없음). 가상은 지금 적용 중 설정(예약분이 있으면 예약분 우선)."""
    import live_config as LC
    out = {}
    cfg = LC.load()
    for a, lab in (("kiwoom", "키움"), ("kis", "KIS")):
        A = cfg["accounts"][a]
        pool = A.get("slot_mode") == "pool"
        for s in A["strategies"]:
            out.setdefault(s["engine"], []).append({"place": a, "label": lab, "kind": "mock",
                                                    "slots": "공유" if pool else int(s["slots"]),
                                                    "live": LC.live_name(s["engine"])})
    active, pending = state(today)
    e = pending or active
    if e and not e.get("legacy"):
        for p in PORTFOLIOS:
            P = e["portfolios"][p]
            pool = P.get("slot_mode") == "pool"
            for s in P["strategies"]:
                out.setdefault(s["engine"], []).append({"place": p, "label": LABEL[p], "kind": "virtual",
                                                        "slots": "공유" if pool else int(s["slots"]),
                                                        "live": LC.live_name(s["engine"]),
                                                        "pending": e is pending})
    return out


def usage_text(uses, kind=None):
    """사람이 읽는 한 줄: '키움 3칸 · KIS 2칸' (kind 로 mock/virtual 만 고를 수 있음)."""
    return " · ".join(f"{u['label']} {u['slots']}{'칸' if isinstance(u['slots'], int) else ''}"
                      for u in uses if kind is None or u["kind"] == kind)


def needed_engines(today=None):
    """가상 신호를 만들어야 할 엔진 이름 — 지금 적용 중·예약 설정의 전략 전부(모의계좌 전략도 포함, 호출측에서 뺌)."""
    active, pending = state(today)
    out = []
    for e in (active, pending):
        if e and not e.get("legacy"):
            for p in PORTFOLIOS:
                for s in e["portfolios"][p]["strategies"]:
                    if s["engine"] not in out:
                        out.append(s["engine"])
    return out
