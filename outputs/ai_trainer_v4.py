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

    if _has_xgb:
        model = xgb.XGBClassifier(
            n_estimators=300, learning_rate=0.04, max_depth=4,
            subsample=0.8, colsample_bytree=0.8, min_child_weight=5,
            random_state=42, eval_metric="logloss",
        )
    else:
        model = GradientBoostingClassifier(
            n_estimators=300, learning_rate=0.04, max_depth=4,
            subsample=0.8, random_state=42)
    model.fit(Xtr, ytr)

    prob = model.predict_proba(Xte)[:, 1]
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
        imp = sorted(zip(X_all.columns, model.feature_importances_), key=lambda x: -x[1])[:10]
        print("\n[Feature Importance Top10]")
        for c, v in imp:
            print(f"  {c}: {v*100:.1f}%")

    os.makedirs(AI_DIR, exist_ok=True)
    if _has_xgb:
        model.save_model(MODEL_PATH + ".json")
    else:
        import joblib
        joblib.dump(model, MODEL_PATH + ".pkl")
    pd.Series(list(X_all.columns)).to_csv(MODEL_PATH + ".features.csv", index=False)
    print(f"\n[saved] {MODEL_PATH} (+ features 목록)")
    print("주의: 매매 결정 자동 반영 안 됨 (USE_AI=False 유지). 사이징 스프레드가")
    print("      수개월 연속 +3%p 이상으로 안정되면 그때 사이징 연동 논의.")


if __name__ == "__main__":
    main()
