# -*- coding: utf-8 -*-
"""
live_config.py — 모의매매 전략 설정(계좌별 전략·슬롯·강도·면제·손절·익절)의 **단일 출처**. (2026-10-21)

왜: 전략 구성이 kiwoom_trader / kis_trader / live_signal / kis_live_signal / 대시보드 / 모니터에 상수로 흩어져
    있어 바꾸려면 코드 6곳을 고쳐야 했다(어긋나면 사고 — 테스트로 동기 강제 중). 사용자가 대시보드에서
    고르게 되면서 이 파일이 db/live_config.json 을 읽어 모두에게 같은 값을 준다.
    파일이 없거나 깨졌으면 **현행 기본값**(아래 DEFAULT) — 설정 기능이 고장 나도 매매는 종전대로.

단위: JSON 의 stop/take 는 **퍼센트**(예 -15, 50). 트레이더용 접근자는 비율(-0.15, 0.50)로 돌려준다.
이름: 설정은 백테스트 엔진 이름(engine)으로 저장, 신호 CSV·원장엔 라이브 이름(live)을 쓴다
      (기존 두 전략만 별칭: star_high_52w_20_filt→high_52w_filt, star_high_500d_20→high_500d_filt).
청산 규칙 누적(exit_rules): 전략을 빼도 그 전략으로 산 보유분은 만기까지 남는다 → 그 보유분의 손절·익절이
      사라지지 않게 지금까지 설정된 적 있는 규칙을 이름별로 계속 보관한다(새 값으로만 덮어씀).
"""
import copy
import json
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SLOTS_TOTAL = 10
MAX_STRATEGIES = 4
ACCOUNTS = ("kiwoom", "kis")
ACCOUNT_LABEL = {"kiwoom": "키움 안C", "kis": "KIS 안D"}
ALIAS = {"star_high_52w_20_filt": "high_52w_filt", "star_high_500d_20": "high_500d_filt"}   # engine → live
LOCK_WINDOW = ("0830", "1540")      # 거래일 이 시간엔 변경 금지(장전 예열·매수·손절·만기매도가 도는 구간)
SIGNAL_TIME = "1825"                # 이 시각 이후(또는 휴장일)에 바꾸면 오늘 신호를 새 설정으로 다시 만든다

DEFAULT = {
    "version": 1,
    "updated": "",
    "note": "현행(2026-10-18 §56) 구성",
    "accounts": {
        "kiwoom": {"min_strength": 5.7, "strategies": [
            {"engine": "star_high_500d_20", "slots": 6, "exempt": True, "stop": None, "take": None, "holding": 20},
            {"engine": "star_high_52w_20_filt", "slots": 4, "exempt": True, "stop": None, "take": 50, "holding": 20},
        ]},
        "kis": {"min_strength": 5.7, "strategies": [
            {"engine": "h52w_for3d_mkt", "slots": 4, "exempt": False, "stop": -15, "take": None, "holding": 20},
            {"engine": "for_high20_mkt", "slots": 4, "exempt": False, "stop": -10, "take": None, "holding": 20},
            {"engine": "gc_for3d", "slots": 2, "exempt": True, "stop": -26, "take": None, "holding": 15},
        ]},
    },
    # 과거 보유분 청산용 누적 규칙(라이브 이름 → {stop, take, holding}) — 운용에서 빠진 전략도 남긴다
    "exit_rules": {
        "rsi_vol": {"stop": None, "take": 20, "holding": 7},
        "rsi_reversal": {"stop": None, "take": None, "holding": 5},
    },
}


def config_path():
    return os.getenv("LIVE_CONFIG_PATH") or os.path.join(HERE, "db", "live_config.json")


def history_path():
    return os.path.join(os.path.dirname(config_path()), "live_config_history.jsonl")


def live_name(engine):
    return ALIAS.get(engine, engine)


def engine_name(live):
    rev = {v: k for k, v in ALIAS.items()}
    return rev.get(live, live)


def default_config():
    return copy.deepcopy(DEFAULT)


def validate(cfg, engine_names=None):
    """→ 오류 문구 목록(비면 통과). engine_names 가 주어지면 전략 이름도 확인."""
    errs = []
    acc = (cfg or {}).get("accounts")
    if not isinstance(acc, dict):
        return ["accounts 가 없습니다"]
    for a in ACCOUNTS:
        A = acc.get(a)
        lab = ACCOUNT_LABEL[a]
        if not isinstance(A, dict):
            errs.append(f"{lab}: 설정이 없습니다")
            continue
        ms = A.get("min_strength")
        if not isinstance(ms, (int, float)) or not 0 <= ms <= 10:
            errs.append(f"{lab}: 강도 기준은 0~10")
        S = A.get("strategies")
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
                errs.append(f"{lab}: {e} 강도 면제 값 오류")
        if tot != SLOTS_TOTAL:
            errs.append(f"{lab}: 슬롯 합이 {tot} — {SLOTS_TOTAL} 이어야 합니다")
    return errs


def load():
    """설정 dict. 파일 없음/깨짐/검증 실패면 기본값(+ '_fallback' 사유)."""
    p = config_path()
    if not os.path.exists(p):
        return default_config()
    try:
        cfg = json.load(open(p, encoding="utf-8"))
    except Exception as e:
        d = default_config()
        d["_fallback"] = f"설정 파일을 읽지 못해 기본값 사용: {type(e).__name__}"
        return d
    errs = validate(cfg)
    if errs:
        d = default_config()
        d["_fallback"] = "설정 검증 실패로 기본값 사용: " + "; ".join(errs[:3])
        return d
    cfg.setdefault("exit_rules", {})
    return cfg


def account(acct, cfg=None):
    """트레이더·신호기용 계좌 뷰 — 라이브 이름 기준.
    → {slots, priority, exempt, min_strength, stop(비율), take(비율), holding, engine, rows}"""
    cfg = cfg or load()
    A = cfg["accounts"][acct]
    rows = A["strategies"]
    out = {"min_strength": float(A["min_strength"]), "rows": rows,
           "slots": {}, "priority": [], "exempt": set(), "stop": {}, "take": {}, "holding": {}, "engine": {}}
    # 누적 규칙 먼저(옛 보유분), 그 위에 현재 규칙
    for ln, r in (cfg.get("exit_rules") or {}).items():
        if r.get("stop") is not None:
            out["stop"][ln] = r["stop"] / 100.0
        if r.get("take") is not None:
            out["take"][ln] = r["take"] / 100.0
        if r.get("holding"):
            out["holding"][ln] = int(r["holding"])
    for s in rows:
        ln = live_name(s["engine"])
        out["slots"][ln] = int(s["slots"])
        out["priority"].append(ln)
        out["engine"][ln] = s["engine"]
        out["holding"][ln] = int(s["holding"])
        if s.get("exempt"):
            out["exempt"].add(ln)
        out["stop"].pop(ln, None)
        out["take"].pop(ln, None)
        if s.get("stop") is not None:
            out["stop"][ln] = s["stop"] / 100.0
        if s.get("take") is not None:
            out["take"][ln] = s["take"] / 100.0
    return out


def all_exempt(cfg=None):
    cfg = cfg or load()
    return set().union(*(account(a, cfg)["exempt"] for a in ACCOUNTS))


def is_locked(now=None):
    """(잠김?, 사유) — 거래일 08:30~15:40 은 매매가 도는 시간이라 변경 금지."""
    now = now or datetime.now()
    try:
        from market_calendar import is_trading_day
        td = is_trading_day(now)
    except Exception:
        td = now.weekday() < 5
    hm = now.strftime("%H%M")
    if td and LOCK_WINDOW[0] <= hm < LOCK_WINDOW[1]:
        return True, f"장중(거래일 {LOCK_WINDOW[0][:2]}:{LOCK_WINDOW[0][2:]}~{LOCK_WINDOW[1][:2]}:{LOCK_WINDOW[1][2:]})엔 바꿀 수 없습니다 — 매매가 도는 중"
    return False, ""


def needs_regen(now=None):
    """저장 후 오늘(최근 거래일) 신호를 새 설정으로 다시 만들어야 하나 — 정기 신호(18:30)가 이미 지났거나 휴장일."""
    now = now or datetime.now()
    try:
        from market_calendar import is_trading_day
        td = is_trading_day(now)
    except Exception:
        td = now.weekday() < 5
    return (not td) or now.strftime("%H%M") >= SIGNAL_TIME


def diff(old, new):
    """사람이 읽는 변경 요약 줄 목록."""
    out = []
    for a in ACCOUNTS:
        lab = ACCOUNT_LABEL[a]
        o, n = old["accounts"][a], new["accounts"][a]
        if o["min_strength"] != n["min_strength"]:
            out.append(f"{lab} 강도 기준 {o['min_strength']:g} → {n['min_strength']:g}")
        om = {s["engine"]: s for s in o["strategies"]}
        nm = {s["engine"]: s for s in n["strategies"]}
        for e in nm:
            if e not in om:
                s = nm[e]
                out.append(f"{lab} + {e} ({s['slots']}슬롯{', 강도 면제' if s['exempt'] else ''}"
                           f"{', 손절 ' + format(s['stop'], 'g') + '%' if s['stop'] is not None else ''}"
                           f"{', 익절 +' + format(s['take'], 'g') + '%' if s['take'] is not None else ''})")
        for e in om:
            if e not in nm:
                out.append(f"{lab} − {e} (보유분은 만기까지 그대로)")
        for e in nm:
            if e in om:
                ch = [f"{k} {_fmt(om[e].get(k))}→{_fmt(nm[e].get(k))}"
                      for k in ("slots", "exempt", "stop", "take") if om[e].get(k) != nm[e].get(k)]
                if ch:
                    out.append(f"{lab} {e}: " + ", ".join(ch))
    return out


def _fmt(v):
    return "없음" if v is None else ("예" if v is True else "아니오" if v is False else f"{v:g}")


def save(new, note="", engine_names=None):
    """검증 → 이전 설정 백업 → 원자 저장 → 이력 1줄. 반환 (저장된 설정, 변경 요약)."""
    errs = validate(new, engine_names)
    if errs:
        raise ValueError("; ".join(errs))
    old = load()
    old.pop("_fallback", None)
    cfg = {"version": 1, "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "note": note or "",
           "accounts": {a: {"min_strength": float(new["accounts"][a]["min_strength"]),
                            "strategies": [{k: s.get(k) for k in ("engine", "slots", "exempt", "stop", "take", "holding")}
                                           for s in new["accounts"][a]["strategies"]]}
                        for a in ACCOUNTS}}
    rules = dict(old.get("exit_rules") or {})
    for a in ACCOUNTS:                       # 지금까지 쓰인 규칙을 누적(옛 보유분 청산용)
        for s in old["accounts"][a]["strategies"] + cfg["accounts"][a]["strategies"]:
            rules[live_name(s["engine"])] = {"stop": s.get("stop"), "take": s.get("take"), "holding": s.get("holding")}
    cfg["exit_rules"] = rules
    changes = diff(old, cfg)
    p = config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        bdir = os.path.join(os.path.dirname(p), "live_config_backups")
        os.makedirs(bdir, exist_ok=True)
        try:                                # 복사(이동 아님) — 아래 쓰기가 실패해도 설정이 사라지지 않게
            import shutil
            shutil.copy2(p, os.path.join(bdir, f"live_config_{datetime.now():%Y%m%d_%H%M%S}.json"))
        except OSError:
            pass
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    try:
        with open(history_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": cfg["updated"], "note": note, "changes": changes}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return cfg, changes
