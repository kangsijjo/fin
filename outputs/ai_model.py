# -*- coding: utf-8 -*-
"""
ai_model.py — 개선 AI(설명서 §84 연구 → §85 적용): '같은 전략·같은 달 안 수익 순위'를 배우는 풀링 XGBoost.

왜(옛 AI = meta_model_v4 의 문제, §84)
  · 목표가 '순수익 10% 이상(대박)'이라 변동성 큰 종목을 찾는 잣대가 됐다 — 같은 날 후보 고르기 실력 없음(dayIC 0.006).
  · AUC 0.606 중 0.597 이 '전략 이름' 효과, 지금 쓰는 전략 대부분이 학습에 없음, 최종 모델이 최근 20개월을 안 씀.
새 방식
  ① 학습표 = 강도 v2 와 같은 전략별 엔진 매매 + 신호일 피처(db/strength_v2/{엔진}_train.pkl, 피처 정의 = 라이브).
  ② 목표 = 그 매매가 '같은 전략·같은 달' 매매 중 몇 등이었나(백분위 − 0.5) → 그달 시장이 좋았는지·전략 평균은 지운다.
  ③ 시장 공통 지표 5개 제외(같은 날 후보끼리는 같은 값)·라이브에 없는 뉴스 감성 제외(EXCLUDE), 전략 더미 포함,
     전략마다 표본 가중 균등(큰 전략 독차지 방지). 결측은 결측 그대로(XGBoost 가 처리 — 0 으로 채우지 않는다).
  ④ 강한 규제(깊이 3·잎 최소 50·λ10·나무 400) — 설정 민감도 8변형 모두 v2 보다 나았음(§84.2).
  ⑤ 점수 = 그 전략 과거 매매 예측값 분위(0~10, 5 = 그 전략 보통) → 강도 v2 와 같은 척도라 기준 칸을 그대로 쓴다.
  ⑥ 매주 전체 데이터로 다시 학습 + 워크포워드 검증(해 Y 는 Y 이전에 청산 끝난 매매로만 학습)을 함께 저장(AI 탭).
쓰는 곳: strength_logger(score_ai 기록) → AI 가상매매 순위(SINCE 신호일부터) · 기간 백테스트 [강도 AI].
모의계좌 매매에는 아직 안 씀(사용자 결정: 11월 가상매매로 v2 와 비교 후 12월 전 결정).

저장: db/ai_model/model.json(XGBoost) · meta.json(피처·전략·분위·검증) · registry.csv(학습 기록) · wf/{Y}.*(워크포워드 캐시)
사용: python ai_model.py --train [--no-validate]     /  python ai_model.py   (상태)
"""
import argparse
import glob
import hashlib
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MODEL_DIR = os.path.join(HERE, "db", "ai_model")
SINCE = "20261102"          # AI 가상매매가 이 신호일부터 score_ai 로 순위·기준(그 전은 옛 ai_prob 기록 그대로)
VERSION = 2                 # 2: 뉴스 감성 제외(라이브 결측) — 바뀌면 워크포워드 캐시도 새로 학습
PARAMS = dict(n_estimators=400, learning_rate=0.03, max_depth=3, subsample=0.7, colsample_bytree=0.7,
              min_child_weight=50, reg_lambda=10.0, tree_method="hist", random_state=42)
SEEDS = (42, 43, 44, 45, 46)  # 무작위 표본추출(subsample) 운을 줄이려고 씨앗 5개 모델 평균 — 씨앗 하나면 상위 2개 효과가
                              # 0.8~1.1%p 로 흔들렸다(같은 표본, 행 순서만 달라도). 평균하면 안정.
MIN_ENGINE_N = 300          # 학습 매매가 이보다 적은 전략은 빼고 학습(그 전략 점수 = None → v2/v1 로 대체)
MIN_TOTAL_N = 2000          # 전체가 이보다 적으면 그 해(워크포워드)는 채점 안 함
MAX_ENGINE_N = 20000        # [§86] 전략당 학습 표본 상한(무작위 고정 추출) — 백테스트한 전략 전부(59개·230만 건)를 배워도
                            # 학습이 몇 분 안에 끝나게. 전략마다 가중이 같아 이보다 많아도 얻는 게 적다. 점수 분위는 전체로 잰다.
QN = 1001
VALIDATE_FROM = 2021
TOP_K = 2


def model_dir():
    return os.getenv("AI_MODEL_DIR") or MODEL_DIR


def _p(name):
    return os.path.join(model_dir(), name)


# 라이브에서 계산되지 않는 지표 — factor_scorer 가 뉴스 감성을 만들지 않아 라이브 100% 결측(학습표는 27% 값 있음).
# 넣으면 학습·라이브가 어긋난다. 라이브 결측 흉내 시험(§85)에서 빼는 쪽이 상위 2개 효과 1.12 → 1.32%p.
EXCLUDE = ("news_sent_7d",)


def feature_list():
    from strength_model import _feats
    return [f for f in _feats()[0] if f not in EXCLUDE]


# ── 학습표 ───────────────────────────────────────────────────────────────────

def available_engines():
    import strength_model as SM
    d = os.path.dirname(SM.train_path("x"))
    return sorted(os.path.basename(p)[:-len("_train.pkl")] for p in glob.glob(os.path.join(d, "*_train.pkl")))


def load_tables(engines=None, extra=None):
    """전략별 학습표 → 하나로(engine 열). extra = {엔진: DataFrame} (백테스트가 지금 만든 표, 파일보다 우선)."""
    import strength_model as SM
    parts, extra = [], dict(extra or {})
    for e in sorted(set(engines or available_engines()) | set(extra)):
        T = extra.get(e)
        if T is None:
            try:
                T = pd.read_pickle(SM.train_path(e))
            except Exception:
                continue
        if T is None or len(T) == 0:
            continue
        T = T.copy()
        T["engine"] = e
        parts.append(T)
    if not parts:
        return pd.DataFrame()
    T = pd.concat(parts, ignore_index=True)
    T = T[T["net_pct"].notna() & T["exit_date"].notna()].copy()
    for c in ("entry_date", "exit_date", "date"):
        T[c] = T[c].astype(str)
    T["ym"] = T["entry_date"].str[:6]
    T["year"] = T["entry_date"].str[:4]
    T = T.replace([np.inf, -np.inf], np.nan).reset_index(drop=True)
    T["y_rank"] = T.groupby(["engine", "ym"])["net_pct"].rank(pct=True) - 0.5
    return T


def _design(D, feats, engines):
    X = pd.DataFrame({f: pd.to_numeric(D[f], errors="coerce") if f in D.columns else np.nan for f in feats},
                     index=D.index).astype(float)
    eng = D["engine"].to_numpy() if "engine" in D.columns else np.array([None] * len(D))
    for e in engines:
        X[f"e_{e}"] = (eng == e).astype(float)
    return X


def _weights(D):
    n = D.groupby("engine")["engine"].transform("size").astype(float)
    w = 1.0 / n
    return (w / w.mean()).to_numpy()


def _cap(T):
    """전략마다 MAX_ENGINE_N 건까지만(넘으면 고정 씨앗 무작위 추출, 순서 유지)."""
    big = T["engine"].value_counts()
    big = big[big > MAX_ENGINE_N].index
    if not len(big):
        return T
    keep = [T[~T["engine"].isin(big)]]
    for e in big:
        keep.append(T[T["engine"] == e].sample(MAX_ENGINE_N, random_state=0))
    return pd.concat(keep).sort_index()


class Ensemble:
    """씨앗만 다른 XGBRegressor 여러 개의 평균."""

    def __init__(self, models):
        self.models = list(models)

    def predict(self, X):
        return np.mean([m.predict(X) for m in self.models], axis=0)

    @property
    def feature_importances_(self):
        return np.mean([m.feature_importances_ for m in self.models], axis=0)


def fit(T, feats=None):
    """학습표 → (Ensemble, meta) 또는 (None, 이유). meta: feats·engines·n·ref(전략별 예측 분위)·importance."""
    import xgboost as xgb
    feats = feats or feature_list()
    if T is None or len(T) == 0:
        return None, "학습표 없음"
    cnt = T["engine"].value_counts()
    engines = sorted(e for e, n in cnt.items() if n >= MIN_ENGINE_N)
    T = T[T["engine"].isin(engines)]
    if len(T) < MIN_TOTAL_N or not engines:
        return None, f"학습 매매 {len(T)}건 < {MIN_TOTAL_N}"
    Tt = _cap(T)
    Xt = _design(Tt, feats, engines)
    y, w = Tt["y_rank"].to_numpy(float), _weights(Tt)
    ms = []
    for sd in SEEDS:
        r = xgb.XGBRegressor(**dict(PARAMS, random_state=sd))
        r.fit(Xt, y, sample_weight=w)
        ms.append(r)
    m = Ensemble(ms)
    X = _design(T, feats, engines)
    pred = m.predict(X)
    ref = {}
    for e in engines:
        v = pred[(T["engine"] == e).to_numpy()]
        ref[e] = np.quantile(v, np.linspace(0, 1, QN)).round(6).tolist()
    imp = {c: round(float(v), 4) for c, v in zip(Xt.columns, m.feature_importances_)}
    meta = {"version": VERSION, "feats": feats, "engines": engines, "n": {e: int(cnt[e]) for e in engines},
            "n_total": int(len(T)), "ref": ref, "importance": imp, "params": PARAMS, "seeds": list(SEEDS),
            "data_max": str(T["entry_date"].max())}
    return m, meta


def to_score(raw, ref):
    """예측값 → 그 전략 과거 예측 분위 기준 0~10(5 = 보통)."""
    ref = np.asarray(ref, float)
    return np.interp(np.asarray(raw, float), ref, np.linspace(0, 10, len(ref)))


def score_frame(D, model, meta, engine=None):
    """D(피처 표, engine 열 또는 engine 인자) → 0~10 점수 배열. 모델에 없는 전략 = NaN."""
    D = D.copy()
    if engine is not None:
        D["engine"] = engine
    out = np.full(len(D), np.nan)
    if model is None or not meta or len(D) == 0:
        return out
    X = _design(D, meta["feats"], meta["engines"])
    raw = model.predict(X)
    eng = D["engine"].to_numpy()
    for e in set(eng):
        if e in meta["ref"]:
            mk = eng == e
            out[mk] = to_score(raw[mk], meta["ref"][e])
    return out


# ── 저장·불러오기 ─────────────────────────────────────────────────────────────

def _part(model_name, i):
    return model_name.replace(".json", f".s{i}.json")


def _save(model, meta, model_name="model.json", meta_name="meta.json"):
    """씨앗 모델들(model.s0.json …) → 메타(meta.json, 맨 마지막 — 메타가 있으면 모델이 다 써진 것)."""
    os.makedirs(model_dir(), exist_ok=True)
    meta = dict(meta, parts=[_part(model_name, i) for i in range(len(model.models))])
    for i, m in enumerate(model.models):
        tmp = _p(_part(model_name, i).replace(".json", ".tmp.json"))
        m.save_model(tmp)
        os.replace(tmp, _p(_part(model_name, i)))
    tmpm = _p(meta_name + ".tmp")
    with open(tmpm, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmpm, _p(meta_name))


def _load(model_name="model.json", meta_name="meta.json"):
    import xgboost as xgb
    tp = _p(meta_name)
    if not os.path.exists(tp):
        return None, None
    meta = json.load(open(tp, encoding="utf-8"))
    ms = []
    for nm in meta.get("parts") or []:
        m = xgb.XGBRegressor()
        m.load_model(_p(nm))
        ms.append(m)
    if not ms:
        return None, None
    return Ensemble(ms), meta


_CACHE = {}


def load():
    """지금 모델(파일이 바뀌면 다시 읽음) → (model, meta) 또는 (None, None)."""
    try:
        key = os.path.getmtime(_p("meta.json"))
    except OSError:
        return None, None
    c = _CACHE.get("live")
    if c and c[0] == key:
        return c[1], c[2]
    try:
        m, meta = _load()
    except Exception:
        return None, None
    _CACHE["live"] = (key, m, meta)
    return m, meta


def meta_info():
    """대시보드용 메타(분위·중요도 등 큰 값 포함 그대로) — 없으면 None."""
    try:
        return json.load(open(_p("meta.json"), encoding="utf-8"))
    except Exception:
        return None


def score(engine, feats):
    """라이브 1건: 엔진 이름 + 피처 dict(factor_scorer.build_feature_vec) → 0~10 또는 None(모델·전략 없음)."""
    try:
        m, meta = load()
        if m is None or engine not in (meta.get("ref") or {}):
            return None
        row = {f: (np.nan if feats.get(f) is None else feats.get(f)) for f in meta["feats"]}
        D = pd.DataFrame([row])
        v = score_frame(D, m, meta, engine=engine)[0]
        return None if v != v else round(float(v), 2)
    except Exception:
        return None


# ── 워크포워드(검증·기간 백테스트) ──────────────────────────────────────────────

def _signature(T):
    s = T.groupby("engine").agg(n=("net_pct", "size"), mx=("exit_date", "max")).reset_index()
    txt = "|".join(f"{r.engine}:{r.n}:{r.mx}" for r in s.itertuples()) + f"|v{VERSION}|{json.dumps(PARAMS, sort_keys=True)}|{SEEDS}|{MAX_ENGINE_N}|{','.join(feature_list())}"
    return hashlib.sha1(txt.encode("utf-8")).hexdigest()[:16]


def wf_model(T, Y, sig=None, log=None):
    """해 Y 채점용 모델 — Y년 1월 1일 전에 청산이 끝난 매매로만 학습. 같은 표본이면 캐시(wf/{Y}.*) 재사용."""
    sig = sig or _signature(T)
    mn, tn = f"wf_{Y}.json", f"wf_{Y}.meta.json"
    try:
        m, meta = _load(mn, tn)
        if m is not None and meta.get("sig") == sig:
            return m, meta
    except Exception:
        pass
    tr = T[T["exit_date"] < f"{Y}0101"]
    m, meta = fit(tr)
    if m is None:
        return None, {"skipped": True, "why": meta, "n_train": int(len(tr))}
    meta["sig"], meta["Y"] = sig, int(Y)
    try:
        _save(m, meta, mn, tn)
    except Exception as e:
        if log:
            log(f"[ai_model] 워크포워드 캐시 저장 실패(무시): {e}")
    return m, meta


def walk_forward(T, X, years, engine, log=print):
    """기간 백테스트용: X(그 전략 기간 안 매매 피처)의 해 Y 행은 Y 이전 매매로만 학습한 풀링 모델로 채점 → (점수, 해별 정보)."""
    out = np.full(len(X), np.nan)
    info = {}
    yrs = np.asarray(years)
    if T is None or len(T) == 0:
        return out, {Y: {"skipped": True, "n_train": 0, "n_ic": 0, "top": []} for Y in sorted(set(yrs))}
    sig = _signature(T)
    for Y in sorted(set(yrs)):
        m, meta = wf_model(T, Y, sig, log)
        if m is None or engine not in (meta.get("ref") or {}):
            info[Y] = {"skipped": True, "n_train": int(meta.get("n_total") or meta.get("n_train") or 0), "n_ic": 0, "top": []}
            continue
        mk = yrs == Y
        out[mk] = score_frame(X[mk].reset_index(drop=True), m, meta, engine=engine)
        top = sorted(((k, v) for k, v in meta["importance"].items() if not k.startswith("e_")), key=lambda kv: -kv[1])[:3]
        info[Y] = {"skipped": False, "n_train": int(meta["n_total"]), "n_ic": len(meta["feats"]),
                   "top": [f"{k} {v * 100:.0f}%" for k, v in top]}
    return out, info


def _lift_rows(D, cols, k=TOP_K):
    """전략·날짜마다(후보 3개↑) 점수 상위 k개 평균 − 그날 후보 평균, 그리고 그날 순위상관. [§86] 벡터화
    (전략 59개 검증에서 날짜 묶음 수만 개를 파이썬으로 돌면 수 분 — 결과는 _lift_rows_slow 와 같음, 테스트로 고정)."""
    D = D.reset_index(drop=True)
    n = D.groupby(["engine", "date"], sort=False)["net_pct"].transform("size")
    D = D[n >= 3].reset_index(drop=True)
    if D.empty:
        return pd.DataFrame()
    grp = [D["engine"], D["date"]]
    net = D["net_pct"].astype(float)
    out = D.groupby(["engine", "date"], sort=False).agg(Y=("Y", "first"), _mu=("net_pct", "mean"))
    rnet = net.groupby(grp).rank()
    for c in cols:
        sc = D[c].astype(float)
        has = sc.notna().groupby(grp).any()
        order = sc.fillna(-np.inf).groupby(grp).rank(method="first", ascending=False)
        top = net.where(order <= k).groupby(grp).mean()
        out[c] = (top - out["_mu"]).where(has)
        rs = sc.groupby(grp).rank()
        ok = rs.notna()
        a_, b_ = rs.where(ok), rnet.where(ok)
        da = a_ - a_.groupby(grp).transform("mean")
        db = b_ - b_.groupby(grp).transform("mean")
        cov = (da * db).groupby(grp).sum()
        va, vb = (da ** 2).groupby(grp).sum(), (db ** 2).groupby(grp).sum()
        cnt = ok.groupby(grp).sum()
        ic = cov / np.sqrt(va * vb)
        out[c + "_ic"] = ic.where((cnt >= 3) & (va > 0) & (vb > 0))
    out = out.drop(columns="_mu").reset_index()
    out["Y"] = out["Y"].astype(int)
    return out


def _lift_rows_slow(D, cols, k=TOP_K):
    """(검산용) 예전 루프판 — 결과 비교 테스트에만 씀."""
    rows = []
    for (e, d), g in D.groupby(["engine", "date"], sort=False):
        if len(g) < 3:
            continue
        net = g["net_pct"].to_numpy(float)
        mu = net.mean()
        rnet = pd.Series(net).rank().to_numpy()
        r = {"engine": e, "date": d, "Y": int(g["Y"].iloc[0])}
        for c in cols:
            s = g[c].to_numpy(float)
            if np.isnan(s).all():
                r[c], r[c + "_ic"] = np.nan, np.nan
                continue
            s2 = np.where(np.isnan(s), -np.inf, s)
            r[c] = net[np.argsort(-s2, kind="stable")[:k]].mean() - mu
            rs = pd.Series(s).rank().to_numpy()
            ok = ~np.isnan(rs)
            r[c + "_ic"] = float(np.corrcoef(rs[ok], rnet[ok])[0, 1]) if ok.sum() >= 3 and np.nanstd(rs[ok]) > 0 else np.nan
        rows.append(r)
    return pd.DataFrame(rows)


def validate(T, log=print):
    """해마다(VALIDATE_FROM~) 'Y 이전 청산분으로 학습 → Y 채점'으로 AI 와 강도 v2 를 같은 표본에서 비교."""
    import strength_model as SM
    if T is None or len(T) == 0:
        return None
    sig = _signature(T)
    last = int(T["year"].max())
    parts = []
    for Y in range(VALIDATE_FROM, last + 1):
        te = T[T["year"] == str(Y)].copy()
        if te.empty:
            continue
        m, meta = wf_model(T, Y, sig, log)
        if m is None:
            continue
        te["ai"] = score_frame(te, m, meta)
        tr = T[T["exit_date"] < f"{Y}0101"]
        te["v2"] = np.nan
        for e in te["engine"].unique():
            mm = SM.fit(tr[tr["engine"] == e])
            if mm:
                mk = (te["engine"] == e).to_numpy()
                te.loc[mk, "v2"] = SM.score_frame(te[mk], mm)
        te["Y"] = Y
        parts.append(te[["engine", "date", "Y", "net_pct", "ai", "v2"]])
        log(f"[ai_model] 검증 {Y}: 학습 {meta['n_total']:,} / 채점 {len(te):,}")
    if not parts:
        return None
    D = _lift_rows(pd.concat(parts, ignore_index=True), ["ai", "v2"])
    if D.empty:
        return None
    eng = D.groupby("engine").agg(ai=("ai", "mean"), v2=("v2", "mean"), ai_ic=("ai_ic", "mean"),
                                  v2_ic=("v2_ic", "mean"), n=("ai", "size"))
    yr = D.groupby(["Y", "engine"])[["ai", "v2"]].mean().groupby("Y").mean()
    r2 = lambda v: None if v != v else round(float(v), 3)   # noqa: E731
    return {
        "k": TOP_K, "from": VALIDATE_FROM, "to": last, "n_cells": int(len(D)),
        "overall": {"ai": r2(eng["ai"].mean()), "v2": r2(eng["v2"].mean()),
                    "ai_ic": r2(eng["ai_ic"].mean()), "v2_ic": r2(eng["v2_ic"].mean()),
                    "engines_better": f"{int((eng['ai'] > eng['v2']).sum())}/{len(eng)}",
                    "years_better": f"{int((yr['ai'] > yr['v2']).sum())}/{len(yr)}"},
        "years": [{"Y": int(y), "ai": r2(r["ai"]), "v2": r2(r["v2"])} for y, r in yr.iterrows()],
        "engines": [{"engine": e, "ai": r2(r["ai"]), "v2": r2(r["v2"]), "ai_ic": r2(r["ai_ic"]),
                     "v2_ic": r2(r["v2_ic"]), "n": int(r["n"])} for e, r in eng.iterrows()],
    }


# ── 주간 학습 ────────────────────────────────────────────────────────────────

def configured_engines():
    """모의계좌 두 계좌 + 가상매매(지금·예약) 설정의 전략."""
    out = set()
    try:
        import live_config as LC
        cfg = LC.load()
        out |= {s["engine"] for a in LC.ACCOUNTS for s in cfg["accounts"][a]["strategies"]}
    except Exception:
        pass
    try:
        import virtual_config as VC
        for e in VC.state():
            if e and not e.get("legacy"):
                for p in VC.PORTFOLIOS:
                    out |= {s["engine"] for s in e["portfolios"][p]["strategies"]}
    except Exception:
        pass
    return sorted(out)


def build_missing(engines, log=print):
    """설정 전략 중 학습표가 없는 것만 만든다(엔진 백테스트 + 피처, 전략당 1~3분)."""
    import strength_model as SM
    miss = [e for e in engines if not os.path.exists(SM.train_path(e))]
    if not miss:
        return []
    from strategies.daily_loader import load_macro_daily
    df = load_macro_daily()
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str).str.zfill(6)
    done = []
    for e in miss:
        try:
            T = SM.build_train_table(e, df, log=log)
            T.to_pickle(SM.train_path(e) + ".tmp")
            os.replace(SM.train_path(e) + ".tmp", SM.train_path(e))
            done.append(e)
        except Exception as ex:
            log(f"[ai_model] {e} 학습표 생성 실패: {type(ex).__name__}: {str(ex)[:120]}")
    return done


LOCK_HOURS = 3


def _acquire():
    """학습 중복 방지(일요일 파이프라인 · 설정 저장 · 백테스트 뒤 학습이 겹칠 수 있음). 3시간 넘은 잠금은 낡은 것으로 보고 넘김."""
    os.makedirs(model_dir(), exist_ok=True)
    lk = _p("train.lock")
    try:
        if os.path.exists(lk) and (datetime.now().timestamp() - os.path.getmtime(lk)) < LOCK_HOURS * 3600:
            return False
        with open(lk, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {datetime.now():%Y-%m-%d %H:%M:%S}")
        return True
    except Exception:
        return True


def _release():
    try:
        os.remove(_p("train.lock"))     # 이 학습이 만든 잠금 표시 파일(데이터 아님)
    except Exception:
        pass


def train(do_validate=True, log=print, extra_engines=None):
    if not _acquire():
        return {"ok": False, "why": "다른 AI 학습이 진행 중(잠금 3시간 이내) — 이번은 건너뜀", "busy": True}
    try:
        return _train(do_validate, log, extra_engines)
    finally:
        _release()


def learn(names, log=print):
    """[§86] 기간 백테스트 뒤: 그 전략들 학습표가 없으면 만들어 저장하고, AI 모델에 없는 전략이 생기면 다시 학습."""
    import strength_model as SM
    names = [n for n in dict.fromkeys(names or []) if n]
    meta = meta_info() or {}
    have = set(meta.get("engines") or [])
    miss_tbl = [n for n in names if not os.path.exists(SM.train_path(n))]
    miss_model = [n for n in names if n not in have]
    if not miss_tbl and not miss_model:
        return {"ok": True, "skipped": True, "why": "백테스트한 전략이 모두 이미 학습됨"}
    res = train(do_validate=True, log=log, extra_engines=names)
    res["learned"] = sorted(set(miss_model) & set((meta_info() or {}).get("engines") or []))
    return res


def _train(do_validate=True, log=print, extra_engines=None):
    built = build_missing(sorted(set(configured_engines()) | set(extra_engines or [])), log=log)
    T = load_tables()
    m, meta = fit(T)
    if m is None:
        return {"ok": False, "why": meta}
    meta["trained"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    meta["built_tables"] = built
    if do_validate:
        try:
            meta["validation"] = validate(T, log=log)
        except Exception as e:
            meta["validation"] = None
            log(f"[ai_model] 검증 실패(모델은 저장): {type(e).__name__}: {str(e)[:150]}")
    _save(m, meta)
    v = (meta.get("validation") or {}).get("overall") or {}
    row = {"trained": meta["trained"], "data_max": meta["data_max"], "n_engines": len(meta["engines"]),
           "n_train": meta["n_total"], "top2_ai": v.get("ai", ""), "top2_v2": v.get("v2", ""),
           "dayic_ai": v.get("ai_ic", ""), "dayic_v2": v.get("v2_ic", ""),
           "engines_better": v.get("engines_better", ""), "years_better": v.get("years_better", "")}
    rp = _p("registry.csv")
    new = not os.path.exists(rp) or os.path.getsize(rp) == 0
    pd.DataFrame([row]).to_csv(rp, mode="a", header=new, index=False, encoding="utf-8-sig")
    return {"ok": True, "n": meta["n_total"], "engines": len(meta["engines"]), "validation": v, "built": built}


def status():
    meta = meta_info()
    if not meta:
        return {"model": None}
    return {"model": {"trained": meta.get("trained"), "data_max": meta.get("data_max"), "n": meta.get("n_total"),
                      "engines": meta.get("engines"), "validation": (meta.get("validation") or {}).get("overall")}}


def main():
    os.chdir(HERE)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--learn", default="", help="[§86] 기간 백테스트한 전략(쉼표) — 학습표 저장 + 모델에 없으면 재학습")
    a = ap.parse_args()
    if a.learn:
        res = learn([x for x in a.learn.split(",") if x])
        print(json.dumps(res, ensure_ascii=False))
        if not res.get("skipped") and not res.get("busy"):
            try:
                from notifier import safe_send
                v = res.get("validation") or {}
                safe_send("🤖 [AI 학습 — 백테스트한 전략 반영] " + (
                    f"✅ {res.get('engines')}전략 {res.get('n', 0):,}건(새로 배운 전략 {len(res.get('learned') or [])}개, 학습표 새로 {len(res.get('built') or [])}개)"
                    + (f" · 검증 AI {v.get('ai')} vs v2 {v.get('v2')}%p" if v else "") if res.get("ok") else f"⚠ 실패: {res.get('why')}"))
            except Exception:
                pass
        return 0 if res.get("ok") or res.get("busy") else 1
    if not a.train:
        print(json.dumps(status(), ensure_ascii=False, indent=1))
        return 0
    res = train(do_validate=not a.no_validate)
    print(json.dumps(res, ensure_ascii=False))
    if res.get("busy"):
        return 0
    try:
        from notifier import safe_send
        v = res.get("validation") or {}
        safe_send("🤖 [AI 모델 학습] " + (f"✅ {res['engines']}전략 {res['n']:,}건"
                  + (f" · 검증(같은 날 상위 2개) AI {v.get('ai')} vs v2 {v.get('v2')}%p, 전략 {v.get('engines_better')} · 해 {v.get('years_better')}"
                     if v else "") if res.get("ok") else f"⚠ 실패: {res.get('why')}"))
    except Exception:
        pass
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
