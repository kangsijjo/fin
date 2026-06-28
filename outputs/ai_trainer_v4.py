"""
AI 학습 v4 — big-win 메타라벨링 + purged 시계열 분리 + 사이징 시뮬.

v2/v3 의 한계 교정:
  1. 라벨: 승패(>0) → big-win (net >= +10%). 이 전략은 승률 ~50% 에
     소수의 큰 승리가 수익을 만드는 구조 — 대박 구분이 실용적.
  2. Purged split: train 매매 중 exit_date 가 test 시작일을 넘는 것 제거
     (40일 보유 겹침으로 인한 누수 차단) + 40영업일 embargo.
  3. 풀링 표본 (trades_history_v3.csv) + 외부 피처 (뉴스 감성·매크로).
  4. 용도: 매수 거부권이 아니라 사이징 — 확률 분위별 평균 수익 출력으로
     "상위 확률에 더 큰 비중" 룰의 근거를 측정.

주 1회 (금요일) 실행 권장 — run_paper.bat 이 자동 처리.
실행: python ai_trainer_v4.py
"""

import os
import sys
import numpy as np
import pandas as pd

try:
    import xgboost as xgb
    _has_xgb = True
    print("[engine] XGBoost")
except Exception:
    from sklearn.ensemble import GradientBoostingClassifier
    _has_xgb = False
    print("[engine] sklearn GradientBoosting")

from sklearn.metrics import roc_auc_score

AI_DIR = "./ai_data"
MODEL_PATH = f"{AI_DIR}/meta_model_v4"
TRADES_PATH = "./trades_history_v3.csv"

BIG_WIN_PCT = 10.0
EMBARGO_BDAYS = 40

FEATURES = [
    # 종목 (pykrx CSV)
    "rsi14", "atr_pct", "vol_ratio", "tv_ratio", "for_5d", "ins_5d", "mcap_class",
    # 신호 강도/유동성
    "score_tv",
    # 뉴스 (stock.db news)
    "news_sent_7d", "news_cnt_7d",
    # 매크로 레짐 (stock.db macro_indicators + indicators.csv)
    "vix", "vix_chg_5d", "sox_ret_5d", "usdkrw_chg_5d", "kospi_ret_20d",
    # 신용잔고 (stock.db credit_balance 2022~ / kiwoom_backfill 우선)
    "crd_remn_rt", "crd_remn_chg_5d",
    # 수급 DB (stock.db supply_demand 2015~) — pykrx for_5d/ins_5d 보완
    "for_net5_db", "ins_net5_db",
    # 기술지표 DB (stock.db korea_indicators — 사전 계산)
    "rsi_db", "macd_hist_db", "bb_pct_db",
    # 키움 프로그램매매 (kiwoom_backfill merge 후 존재 — 없으면 자동 제외)
    "prm_net_5d_ratio",
]
CAT = {"strategy"}  # one-hot

# [개선] 지표 조합 탐색 — 개별 22피처를 2^22 로 켜고끄면 과적합 → '의미 그룹' 단위 토글.
#   CORE 는 항상 포함(기본 가격·기술·유동성), 나머지 그룹만 Optuna 가 on/off.
#   strat_* one-hot 도 항상 포함. purged-CV 사이징 스프레드로 평가 + 간결성 페널티로 과적합 억제.
CORE_FEATURES = ["rsi14", "atr_pct", "vol_ratio", "tv_ratio", "score_tv", "mcap_class"]
FEATURE_GROUPS = {
    "supply_pykrx": ["for_5d", "ins_5d"],
    "news":         ["news_sent_7d", "news_cnt_7d"],
    "macro":        ["vix", "vix_chg_5d", "sox_ret_5d", "usdkrw_chg_5d", "kospi_ret_20d"],
    "credit":       ["crd_remn_rt", "crd_remn_chg_5d"],
    "supply_db":    ["for_net5_db", "ins_net5_db"],
    "tech_db":      ["rsi_db", "macd_hist_db", "bb_pct_db"],
    "program":      ["prm_net_5d_ratio"],
}


def main():
    if not os.path.exists(TRADES_PATH):
        print(f"[error] {TRADES_PATH} 없음 — make_trades_history_v3.py 먼저")
        sys.exit(1)

    df = pd.read_csv(TRADES_PATH, dtype={"code": str, "date": str,
                                         "entry_date": str, "exit_date": str})
    df = df.sort_values("date").reset_index(drop=True)
    df["label"] = (df["net_pct"] >= BIG_WIN_PCT).astype(int)

    # 전략 one-hot
    feats = [c for c in FEATURES if c in df.columns]
    X_all = df[feats].copy()
    for s in sorted(df["strategy"].unique()):
        X_all[f"strat_{s}"] = (df["strategy"] == s).astype(int)

    # inf (0-나눗셈 파생) → NaN. 결측: XGB 는 NaN 자체 처리, sklearn 은 중앙값 대체
    X_all = X_all.replace([np.inf, -np.inf], np.nan)
    if not _has_xgb:
        X_all = X_all.fillna(X_all.median(numeric_only=True))

    # ---- Purged time-series split (80/20) ----
    # 미청산(보유 중) 매매는 exit_date=NaN → 라벨도 없고 누수 위험 → 먼저 제거
    n_before = len(df)
    df = df[df["exit_date"].notna()].copy()
    if n_before - len(df):
        print(f"[filter] 미청산(exit_date NaN) {n_before - len(df):,}건 제외")
    X_all = X_all.loc[df.index]   # feature 행도 동기화

    dates = sorted(df["date"].unique())
    cut_date = dates[int(len(dates) * 0.8)]
    test_mask = df["date"] > cut_date
    # train: 신호일이 cut 이전이면서 exit 도 test 구간을 침범하지 않는 매매만
    train_mask = (df["date"] <= cut_date) & (df["exit_date"] < cut_date)
    # embargo: cut 직전 40영업일 신호 제외 (피처 자기상관 잔여 누수 방지)
    if len(dates) > EMBARGO_BDAYS:
        embargo_start = dates[max(0, dates.index(cut_date) - EMBARGO_BDAYS)]
        train_mask &= df["date"] < embargo_start

    tr, te = df[train_mask], df[test_mask]
    Xtr, ytr = X_all[train_mask], df.loc[train_mask, "label"]
    Xte, yte = X_all[test_mask], df.loc[test_mask, "label"]
    print(f"[split] train {len(tr):,} ({tr['date'].min()}~{tr['date'].max()}) | "
          f"test {len(te):,} ({te['date'].min()}~{te['date'].max()}) | "
          f"purge+embargo 제외 {len(df)-len(tr)-len(te):,}")
    print(f"[label] big-win(≥{BIG_WIN_PCT}%) 비율: train {ytr.mean()*100:.1f}% / test {yte.mean()*100:.1f}%")

    if len(tr) < 200 or ytr.sum() < 30:
        print("[abort] 표본 부족 — 데이터 백필 후 재시도 (python pykrx_collector.py 8)")
        sys.exit(1)

    # 지표조합 탐색이 채울 최종 피처(폴백/비-XGB 경로는 전체 사용)
    final_cols = list(X_all.columns)
    sel_groups = []      # 선택된 지표그룹(레지스트리 기록용; 폴백 경로는 빈 리스트)
    best_value = None    # CV 목적함수 best(레지스트리 기록용)

    # ---- [업데이트] Optuna AutoML: 하이퍼파라미터 + 지표조합(그룹 on/off) 동시 탐색 ----
    if _has_xgb:
        try:
            import optuna
            optuna.logging.set_verbosity(optuna.logging.WARNING) # 로그 최소화

            from optuna.samplers import TPESampler

            # [개선 B] 목적함수 = 단일분할 AUC → purged 시계열 CV 의 '사이징 스프레드'.
            #   이 모델 용도는 분류정확도(AUC)가 아니라 '확률 상위에 더 큰 비중' 사이징이므로,
            #   상위-하위 분위 실수익 스프레드를 직접 최적화한다. embargo+청산겹침 제거로 누수 차단.
            #   표본이 적으면 단일분할 AUC 로 자동 폴백.
            meta_tr = tr[["date", "exit_date", "net_pct"]].reset_index(drop=True)
            X_arr = Xtr.reset_index(drop=True)
            y_arr = ytr.reset_index(drop=True)
            tr_dates = sorted(meta_tr["date"].unique())
            K = 3
            use_cv = len(tr_dates) >= (K + 1) * 10

            # 지표조합 탐색용 — 코어/그룹/전략 컬럼(실제 존재하는 것만)
            strat_cols = [c for c in X_arr.columns if c.startswith("strat_")]
            avail_core = [c for c in CORE_FEATURES if c in X_arr.columns]
            group_avail = {g: [c for c in cols if c in X_arr.columns]
                           for g, cols in FEATURE_GROUPS.items()}
            usable_groups = [g for g, cols in group_avail.items() if cols]

            def sel_cols(enabled):
                cols = list(avail_core)
                for g in enabled:
                    cols += group_avail[g]
                cols += strat_cols
                seen, out = set(), []
                for c in cols:
                    if c not in seen:
                        seen.add(c)
                        out.append(c)
                return out if len(out) >= 3 else list(X_arr.columns)

            fold_bounds = []
            for _k in range(1, K + 1):
                _vs = tr_dates[int(len(tr_dates) * _k / (K + 1))]
                _ve = tr_dates[int(len(tr_dates) * (_k + 1) / (K + 1))] if _k < K else None
                fold_bounds.append((_vs, _ve))

            def _spread_on_fold(params, cols, val_start, val_end):
                gi = dates.index(val_start) if val_start in dates else None
                emb = dates[max(0, gi - EMBARGO_BDAYS)] if gi is not None else val_start
                trm = (meta_tr["date"] < emb) & (meta_tr["exit_date"] < val_start)
                if val_end is None:
                    vlm = (meta_tr["date"] >= val_start)
                else:
                    vlm = (meta_tr["date"] >= val_start) & (meta_tr["date"] < val_end)
                if trm.sum() < 100 or vlm.sum() < 30 or int(y_arr[trm].sum()) < 10:
                    return None
                Xc = X_arr[cols]
                m = xgb.XGBClassifier(**params)
                m.fit(Xc[trm.values], y_arr[trm.values])
                p = m.predict_proba(Xc[vlm.values])[:, 1]
                net = meta_tr.loc[vlm, "net_pct"].values
                try:
                    q = pd.qcut(p, 5, labels=False, duplicates="drop")
                except Exception:
                    return None
                dq = pd.DataFrame({"q": q, "net": net}).groupby("q")["net"].mean()
                if len(dq) < 2:
                    return None
                return float(dq.iloc[-1] - dq.iloc[0])

            def objective(trial):
                params = {
                    "n_estimators": trial.suggest_int("n_estimators", 100, 400, step=100),
                    "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.1),
                    "max_depth": trial.suggest_int("max_depth", 3, 6),
                    "subsample": trial.suggest_float("subsample", 0.6, 0.9),
                    "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 0.9),
                    "min_child_weight": trial.suggest_int("min_child_weight", 1, 7),
                    "random_state": 42,
                    "eval_metric": "logloss",
                }
                # 지표조합: 그룹별 on/off (코어는 항상 포함)
                enabled = [g for g in usable_groups
                           if trial.suggest_categorical(f"grp_{g}", [False, True])]
                cols = sel_cols(enabled)
                penalty = 0.02 * len(enabled)   # 간결성: 동률이면 적은 그룹 선호(과적합 억제)
                if use_cv:
                    sp = []
                    for vs, ve in fold_bounds:
                        s = _spread_on_fold(params, cols, vs, ve)
                        if s is not None:
                            sp.append(s)
                    return (float(np.mean(sp)) - penalty) if sp else -999.0
                # 폴백: 단일분할 AUC
                vi = int(len(X_arr) * 0.8)
                Xc = X_arr[cols]
                m = xgb.XGBClassifier(**params)
                m.fit(Xc.iloc[:vi], y_arr.iloc[:vi])
                pv = m.predict_proba(Xc.iloc[vi:])[:, 1]
                try:
                    return roc_auc_score(y_arr.iloc[vi:], pv) - penalty * 0.01
                except Exception:
                    return 0.5

            metric_name = "분위 스프레드%(purged CV)" if use_cv else "AUC(단일분할 폴백)"
            print(f"\n[AutoML] 하이퍼파라미터+지표조합 탐색 40회 — 목적함수: {metric_name}")
            study = optuna.create_study(direction="maximize", sampler=TPESampler(seed=42))
            study.optimize(objective, n_trials=40)
            best_all = study.best_params

            # 하이퍼파라미터와 그룹토글 분리 → 최종 피처셋 확정
            hp = {k: v for k, v in best_all.items() if not k.startswith("grp_")}
            enabled = [g for g in usable_groups if best_all.get(f"grp_{g}", False)]
            final_cols = sel_cols(enabled)
            sel_groups = enabled                 # 레지스트리용
            best_value = float(study.best_value)  # 레지스트리용
            print(f"[AutoML] 완료! best_value={study.best_value:+.3f}")
            print(f"  하이퍼파라미터: {hp}")
            print(f"  선택 지표그룹({len(enabled)}/{len(usable_groups)}): {enabled or '코어만'}")
            print(f"  최종 피처 {len(final_cols)}개 (코어 {len(avail_core)} + 그룹 + 전략 {len(strat_cols)})")

            hp["random_state"] = 42
            hp["eval_metric"] = "logloss"
            model = xgb.XGBClassifier(**hp)

        except ImportError:
            print("[warn] optuna 미설치. 기본 파라미터로 학습합니다.")
            model = xgb.XGBClassifier(
                n_estimators=300, learning_rate=0.04, max_depth=4,
                subsample=0.8, colsample_bytree=0.8, min_child_weight=5,
                random_state=42, eval_metric="logloss",
            )
    else:
        # sklearn GradientBoosting (XGBoost 없을 때 대비)
        model = GradientBoostingClassifier(
            n_estimators=300, learning_rate=0.04, max_depth=4,
            subsample=0.8, random_state=42)
            
    # 최종 모델 학습 — 탐색이 고른 지표조합(final_cols)으로
    model.fit(Xtr[final_cols], ytr)

    prob = model.predict_proba(Xte[final_cols])[:, 1]
    try:
        auc = roc_auc_score(yte, prob)
    except Exception:
        auc = float("nan")
    print(f"\n[test] AUC(big-win): {auc:.3f}  (0.5=무정보)")

    # ---- 확률 분위별 실수익 (사이징 근거) ----
    te2 = te.copy()
    te2["prob"] = prob
    te2["q"] = pd.qcut(te2["prob"], 5, labels=False, duplicates="drop")
    print("\n[사이징 근거] 확률 5분위별 test 실적:")
    g = te2.groupby("q").agg(n=("net_pct", "size"), avg_net=("net_pct", "mean"),
                             bigwin=("label", "mean"))
    for q, r in g.iterrows():
        print(f"  Q{int(q)+1}: n={int(r['n']):4d}  avg_net={r['avg_net']:+7.2f}%  "
              f"big-win율={r['bigwin']*100:5.1f}%")
    top = g["avg_net"].iloc[-1] if len(g) else float("nan")
    bot = g["avg_net"].iloc[0] if len(g) else float("nan")
    print(f"  → 상위-하위 분위 스프레드: {top - bot:+.2f}%p "
          f"({'유의미 — 사이징에 활용 가능' if top - bot > 3 else '약함 — 표시 전용 유지 권장'})")

    if hasattr(model, "feature_importances_"):
        imp = sorted(zip(final_cols, model.feature_importances_), key=lambda x: -x[1])[:10]
        print("\n[Feature Importance Top10]")
        for c, v in imp:
            print(f"  {c}: {v*100:.1f}%")

    os.makedirs(AI_DIR, exist_ok=True)
    if _has_xgb:
        model.save_model(MODEL_PATH + ".json")
    else:
        import joblib
        joblib.dump(model, MODEL_PATH + ".pkl")
    pd.Series(final_cols).to_csv(MODEL_PATH + ".features.csv", index=False)
    # ---- AI 예측 누적 저장 (버전별 append — 모델 진화 추적) ----
    # 기존: 매 학습마다 덮어쓰기 → 진화 이력 소실. 변경: run_date+model_version 붙여 append,
    # (model_version,date,code,strategy) 중복 제거, 최근 12개 버전만 유지(무한증식 방지).
    from datetime import datetime as _dt
    run_ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    cols_to_save = ["date", "code", "strategy", "entry_date", "exit_date", "net_pct", "prob", "q", "label"]
    pred = te2[cols_to_save].copy()
    pred.insert(0, "run_date", _dt.now().strftime("%Y-%m-%d"))
    pred.insert(1, "model_version", run_ts)
    pred_path = f"{AI_DIR}/ai_predictions_history.csv"
    if os.path.exists(pred_path):
        try:
            old = pd.read_csv(pred_path, dtype={"code": str})
            pred = pd.concat([old, pred], ignore_index=True)
            pred = pred.drop_duplicates(
                subset=["model_version", "date", "code", "strategy"], keep="last")
            keep_vers = sorted(pred["model_version"].astype(str).unique())[-12:]
            pred = pred[pred["model_version"].astype(str).isin(keep_vers)]
        except Exception as e:
            print(f"[warn] 기존 예측 병합 실패(새로 작성): {e}")
    pred.to_csv(pred_path, index=False, encoding="utf-8-sig")
    _nv = pred["model_version"].nunique() if "model_version" in pred.columns else 1
    print(f"[saved] {pred_path} (예측 누적 — {len(pred):,}행 / {_nv}개 버전)")

    # ---- AI 모델 레지스트리 (학습마다 1행 — 대시보드 '모델 진화' 패널 소스) ----
    try:
        _spread = float(top - bot) if (top == top and bot == bot) else None  # NaN 가드
    except Exception:
        _spread = None
    reg_row = {
        "run_date":      _dt.now().strftime("%Y-%m-%d %H:%M"),
        "model_version": run_ts,
        "engine":        "xgboost" if _has_xgb else "sklearn",
        "n_features":    len(final_cols),
        "n_groups":      len(sel_groups),
        "groups":        "|".join(sel_groups) if sel_groups else "core_only",
        "test_auc":      round(float(auc), 4) if auc == auc else "",
        "spread_pp":     round(_spread, 3) if _spread is not None else "",
        "cv_best":       round(best_value, 3) if best_value is not None else "",
        "train_n":       int(len(tr)),
        "test_n":        int(len(te)),
    }
    reg_path = f"{AI_DIR}/ai_model_registry.csv"
    try:
        import csv as _csv
        _new = not os.path.exists(reg_path) or os.path.getsize(reg_path) == 0
        with open(reg_path, "a", newline="", encoding="utf-8-sig") as _f:
            _w = _csv.DictWriter(_f, fieldnames=list(reg_row.keys()))
            if _new:
                _w.writeheader()
            _w.writerow(reg_row)
        print(f"[saved] {reg_path} (모델 진화 레지스트리)")
    except Exception as e:
        print(f"[warn] 레지스트리 기록 실패(무시): {e}")

    print(f"\n[saved] {MODEL_PATH} (+ features 목록)")
    print("주의: 매매 결정 자동 반영 안 됨 (USE_AI=False 유지). 사이징 스프레드가")
    print("      수개월 연속 +3%p 이상으로 안정되면 그때 사이징 연동 논의.")


if __name__ == "__main__":
    main()