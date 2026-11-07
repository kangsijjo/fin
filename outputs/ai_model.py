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

[§97 2026-11-05] 검증된 방법 차용(샌드박스 워크포워드 2021~2026, 59전략 — 같은 날 상위 2개 효과 %p)
  ⑦ 가격·거래 피처 20개 추가(ai_features — Gu·Kelly·Xiu 2020: 모멘텀·유동성·변동성·규모 / Qlib Alpha158 일부).
  ⑧ 모델 2종 평균: 회귀(같은 달 순위, ②) + 분류(이익 확률 net_pct > 0). 각자 그 전략 과거 분위로 바꿔 평균 →
     다시 그 전략 과거 분위로 0~10(5 = 보통 그대로). 피처만 넣으면 개선이 2021~22년에 몰려 최근 3년은 현행과 같았고,
     둘을 평균하면 6년 모두 현행보다 좋았다(전체 1.10 → 1.42, 2024~26 1.26 → 1.55).
  시험했지만 뺀 것: 순위학습(LTR pairwise) 1.01 · 표본 고유성 가중 0.95 · 분류만(메타라벨링) 1.12 · 수급 규모 정규화(효과 없음).

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

import ai_features as AF  # noqa: E402  [§97] 가격·거래 피처(학습·라이브 같은 함수)

MODEL_DIR = os.path.join(HERE, "db", "ai_model")
SINCE = "20261102"          # AI 가상매매가 이 신호일부터 score_ai 로 순위·기준(그 전은 옛 ai_prob 기록 그대로)
VERSION = 3                 # 2: 뉴스 감성 제외(라이브 결측) / 3: [§97] 가격 피처 + 회귀·분류 평균 — 바뀌면 워크포워드 캐시도 새로 학습
PARAMS = dict(n_estimators=400, learning_rate=0.03, max_depth=3, subsample=0.7, colsample_bytree=0.7,
              min_child_weight=50, reg_lambda=10.0, tree_method="hist", random_state=42)
SEEDS = (42, 43, 44)        # 무작위 표본추출(subsample) 운을 줄이려고 씨앗 여러 개 모델 평균 — 씨앗 하나면 상위 2개 효과가
                            # 0.8~1.1%p 로 흔들렸다. [§97] 회귀·분류 각 3개(=모델 6개, 예전 회귀 5개보다 학습 1.2배) —
                            # 검증은 각 2개로 했으니 이보다 더 평균된 상태.
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
    """강도 v2 피처(뉴스 감성 제외) + [§97] 가격·거래 피처 20개."""
    from strength_model import _feats
    return [f for f in _feats()[0] if f not in EXCLUDE] + list(AF.PRICE_FEATS)


# ── 학습표 ───────────────────────────────────────────────────────────────────

def available_engines():
    import strength_model as SM
    d = os.path.dirname(SM.train_path("x"))
    return sorted(os.path.basename(p)[:-len("_train.pkl")] for p in glob.glob(os.path.join(d, "*_train.pkl")))


def _load_daily():
    from strategies.daily_loader import load_macro_daily
    return load_macro_daily()


def load_tables(engines=None, extra=None, daily=None, pan=None, price=True):
    """전략별 학습표 → 하나로(engine 열). extra = {엔진: DataFrame} (백테스트가 지금 만든 표, 파일보다 우선).
    [§97] price=True 면 가격 피처 20개를 (code, 신호일)로 붙인다 — pan = 전체 일봉으로 미리 계산한 ai_features.panel,
    daily = 잘리지 않은 전체 일봉(둘 다 없으면 여기서 읽음 — 250거래일 창이라 잘린 일봉을 주면 앞쪽 해 값이 비거나 달라진다).
    실패하면 예외(가격 피처 없이 학습하면 라이브와 어긋남)."""
    T = _load_tables(engines, extra)
    if price and len(T):
        if pan is None and daily is None:
            daily = _load_daily()
        AF.attach(T, daily, pan=pan)
    return T


def _load_tables(engines=None, extra=None):
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
    """[§97] float32 — XGBoost 가 안에서 어차피 float32 로 바꾸므로 예측은 같고, 메모리만 절반(가격 피처로 열이 늘어서)."""
    X = pd.DataFrame({f: pd.to_numeric(D[f], errors="coerce") if f in D.columns else np.nan for f in feats},
                     index=D.index).astype(np.float32)
    eng = D["engine"].to_numpy() if "engine" in D.columns else np.array([None] * len(D))
    dm = pd.DataFrame({f"e_{e}": (eng == e).astype(np.float32) for e in engines}, index=D.index)
    return pd.concat([X, dm], axis=1)


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
    """씨앗만 다른 XGBRegressor 여러 개의 평균 + [§97] XGBClassifier(이익 확률) 여러 개의 평균(없으면 회귀만 — 옛 모델)."""

    def __init__(self, models, cls=None):
        self.models = list(models)
        self.cls = list(cls or [])

    def predict(self, X):
        """회귀(같은 달 순위) 예측 평균."""
        return np.mean([m.predict(X) for m in self.models], axis=0)

    def predict_win(self, X):
        """분류(net_pct > 0) 확률 평균 — 분류 모델이 없으면 None."""
        if not self.cls:
            return None
        return np.mean([m.predict_proba(X)[:, 1] for m in self.cls], axis=0)

    @property
    def feature_importances_(self):
        return np.mean([m.feature_importances_ for m in self.models + self.cls], axis=0)


def _qs(v):
    """예측값 → 분위표(QN 점)."""
    return np.quantile(v, np.linspace(0, 1, QN)).round(6).tolist()


def _avgq(raw, win, ref, ref_c):
    """[§97] 회귀·분류 예측을 각자 그 전략 과거 분위(0~1)로 바꿔 평균."""
    u = np.linspace(0, 1, len(ref))
    uc = np.linspace(0, 1, len(ref_c))
    return (np.interp(np.asarray(raw, float), np.asarray(ref, float), u)
            + np.interp(np.asarray(win, float), np.asarray(ref_c, float), uc)) / 2


def fit(T, feats=None):
    """학습표 → (Ensemble, meta) 또는 (None, 이유). meta: feats·engines·n·ref(전략별 회귀 예측 분위)·
    [§97] ref_c(분류 확률 분위)·ref_a(두 분위 평균의 분위 → 최종 0~10)·importance."""
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
    yc = (Tt["net_pct"].to_numpy(float) > 0).astype(int)
    ms, cs = [], []
    for sd in SEEDS:
        r = xgb.XGBRegressor(**dict(PARAMS, random_state=sd))
        r.fit(Xt, y, sample_weight=w)
        ms.append(r)
    if 0 < yc.sum() < len(yc):                  # 이익·손실이 다 있어야 분류를 배운다(실데이터는 늘 그렇다)
        for sd in SEEDS:
            c = xgb.XGBClassifier(**dict(PARAMS, random_state=sd))
            c.fit(Xt, yc, sample_weight=w)
            cs.append(c)
    m = Ensemble(ms, cs)
    eng = T["engine"].to_numpy()
    ref, ref_c, ref_a = {}, {}, {}
    for e in engines:                           # 전략마다 따로 예측(전체 230만 건 설계행렬을 한 번에 만들지 않게 — 메모리)
        Xe = _design(T[eng == e], feats, engines)
        pe, we = m.predict(Xe), m.predict_win(Xe)
        ref[e] = _qs(pe)
        if we is not None:
            ref_c[e] = _qs(we)
            ref_a[e] = _qs(_avgq(pe, we, ref[e], ref_c[e]))
    imp = {c: round(float(v), 4) for c, v in zip(Xt.columns, m.feature_importances_)}
    meta = {"version": VERSION, "feats": feats, "engines": engines, "n": {e: int(cnt[e]) for e in engines},
            "n_total": int(len(T)), "ref": ref, "importance": imp, "params": PARAMS, "seeds": list(SEEDS),
            "data_max": str(T["entry_date"].max()), "price_version": AF.VERSION,
            "ensemble": "회귀+분류" if cs else "회귀"}
    if cs:
        meta["ref_c"], meta["ref_a"] = ref_c, ref_a
    return m, meta


def to_score(raw, ref):
    """예측값 → 그 전략 과거 예측 분위 기준 0~10(5 = 보통)."""
    ref = np.asarray(ref, float)
    return np.interp(np.asarray(raw, float), ref, np.linspace(0, 10, len(ref)))


def score_frame(D, model, meta, engine=None):
    """D(피처 표, engine 열 또는 engine 인자) → 0~10 점수 배열. 모델에 없는 전략 = NaN.
    [§97] 분류 모델·분위표(ref_c·ref_a)가 있으면 두 모델 평균, 없으면(옛 모델) 회귀만 — 척도는 둘 다 0~10·5 = 보통."""
    D = D.copy()
    if engine is not None:
        D["engine"] = engine
    out = np.full(len(D), np.nan)
    if model is None or not meta or len(D) == 0:
        return out
    X = _design(D, meta["feats"], meta["engines"])
    raw = model.predict(X)
    ra, rc = meta.get("ref_a") or {}, meta.get("ref_c") or {}
    win = model.predict_win(X) if ra else None
    eng = D["engine"].to_numpy()
    for e in set(eng):
        if e in meta["ref"]:
            mk = eng == e
            if win is not None and e in ra and e in rc:
                out[mk] = to_score(_avgq(raw[mk], win[mk], meta["ref"][e], rc[e]), ra[e])
            else:
                out[mk] = to_score(raw[mk], meta["ref"][e])
    return out


# ── 저장·불러오기 ─────────────────────────────────────────────────────────────

def _part(model_name, i, kind="s"):
    return model_name.replace(".json", f".{kind}{i}.json")


def _save(model, meta, model_name="model.json", meta_name="meta.json"):
    """씨앗 모델들(회귀 model.s0.json … · [§97] 분류 model.c0.json …) → 메타(meta.json, 맨 마지막 — 메타가 있으면 모델이 다 써진 것)."""
    os.makedirs(model_dir(), exist_ok=True)
    meta = dict(meta, parts=[_part(model_name, i) for i in range(len(model.models))],
                parts_cls=[_part(model_name, i, "c") for i in range(len(model.cls))])
    for kind, ms in (("s", model.models), ("c", model.cls)):
        for i, m in enumerate(ms):
            tmp = _p(_part(model_name, i, kind).replace(".json", ".tmp.json"))
            m.save_model(tmp)
            os.replace(tmp, _p(_part(model_name, i, kind)))
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
    ms, cs = [], []
    for nm in meta.get("parts") or []:
        m = xgb.XGBRegressor()
        m.load_model(_p(nm))
        ms.append(m)
    for nm in meta.get("parts_cls") or []:           # [§97] 옛 모델(메타에 없음)은 회귀만
        c = xgb.XGBClassifier()
        c.load_model(_p(nm))
        cs.append(c)
    if not ms:
        return None, None
    return Ensemble(ms, cs), meta


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
    txt = "|".join(f"{r.engine}:{r.n}:{r.mx}" for r in s.itertuples()) + f"|v{VERSION}|{json.dumps(PARAMS, sort_keys=True)}|{SEEDS}|{MAX_ENGINE_N}|{','.join(feature_list())}|pf{AF.VERSION}"
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
    """기간 백테스트용: X(그 전략 기간 안 매매 피처)의 해 Y 행은 Y 이전 매매로만 학습한 풀링 모델로 채점 → (점수, 해별 정보).
    [§97] X 에 가격 피처가 있어야 한다(ai_features.attach) — 없으면 조용히 결측으로 채점하지 않고 멈춘다."""
    miss = [f for f in AF.PRICE_FEATS if f not in X.columns]
    if miss and len(X):
        raise ValueError(f"X 에 가격 피처 없음({len(miss)}개) — ai_features.attach(X, 일봉) 먼저")
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
    """모의계좌 두 계좌 + [§96] 키움 본계좌 + 가상매매(지금·예약) 설정의 전략."""
    out = set()
    try:
        import live_config as LC
        cfg = LC.load()
        out |= {s["engine"] for a in LC.ALL_ACCOUNTS for s in cfg["accounts"][a]["strategies"]}
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


def build_missing(engines, log=print, daily=None):
    """설정 전략 중 학습표가 없는 것만 만든다(엔진 백테스트 + 피처, 전략당 1~3분). daily = 이미 읽은 전체 일봉(없으면 읽음)."""
    import strength_model as SM
    miss = [e for e in engines if not os.path.exists(SM.train_path(e))]
    if not miss:
        return []
    df = daily if daily is not None else _load_daily()
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
    try:                                        # [§97] 학습표 만들기·가격 피처가 같은 일봉을 한 번만 읽는다
        daily = _load_daily()
    except Exception as e:
        return {"ok": False, "why": f"일봉 읽기 실패: {type(e).__name__}: {str(e)[:120]}"}
    built = build_missing(sorted(set(configured_engines()) | set(extra_engines or [])), log=log, daily=daily)
    try:
        T = load_tables(daily=daily)
    except Exception as e:                      # 가격 피처 없이 학습하면 라이브(피처 있음)와 어긋난다 → 학습 안 함(지금 모델 유지)
        return {"ok": False, "why": f"학습표·가격 피처 준비 실패: {type(e).__name__}: {str(e)[:120]}"}
    del daily
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
