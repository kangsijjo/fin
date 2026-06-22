"""
모의투자 대시보드 HTML 생성기.

paper_signals.csv + macro_data → dashboard.html
브라우저로 열어서 시각화 확인.

표시:
  - KPI: 자본 / CAGR / MDD / Sharpe / 보유 / 누적 매매
  - 자본 곡선 차트 (Chart.js)
  - 현재 보유 포지션 (entry 가격 + 현재가 + 평가손익)
  - 최근 매매 이력 (최근 50건)
  - 보유 종목의 일별 종가 변동 차트
"""

import os
import json
from datetime import datetime
import pandas as pd

import config
from strategies.daily_loader import load_macro_daily
from capital_simulator import simulate_capital
from strategies.base import StrategyTrade


SIGNALS_CSV = "./paper_signals.csv"
NAME_CACHE_CSV = "./name_cache.csv"
OUT_HTML = "./dashboard.html"
INITIAL_CAPITAL = 10_000_000
MAX_CONCURRENT = 10
HOLDING_DAYS = 40
COST_PCT = 0.330
LOOKBACK_DAYS = 500   # 메인 전략 룩백
PER_SLOT = INITIAL_CAPITAL // MAX_CONCURRENT   # 슬롯당 베팅 = 100만원

# X2 모드 — 시간외 매수 가정 (paper_tracker.py 와 동일)
ENTRY_AT_CLOSE = True
ENTRY_SLIPPAGE_PCT = 2.0


AI_MACRO_COLS = ["kosdaq_return", "kosdaq_disparity", "kosdaq_volatility",
                  "kosdaq_foreign_net", "kosdaq_inst_net"]
AI_STOCK_COLS = ["rsi14", "atr_pct", "vol_ratio", "tv_ratio",
                  "for_5d", "ins_5d", "mcap_class"]
AI_ALL_COLS = AI_MACRO_COLS + AI_STOCK_COLS


def _load_ai_model():
    """AI 모델 로드 (v4 우선, 없으면 v3 → v2). 없으면 None.

    v4 는 features CSV 로 컬럼 목록 동적 로드 (ai_trainer_v4 의 실제 피처셋).
    """
    def _load_v4_cols(base_path):
        """meta_model_v4.features.csv 에서 피처 목록 로드."""
        feat_csv = base_path + ".features.csv"
        if os.path.exists(feat_csv):
            try:
                return pd.read_csv(feat_csv, header=None)[0].tolist()
            except Exception:
                pass
        return AI_ALL_COLS  # fallback

    v4_base = "./ai_data/meta_model_v4"
    candidates = [
        (v4_base + ".json", "xgboost v4", None),  # cols: dynamic
        (v4_base + ".pkl",  "sklearn v4", None),
        ("./ai_data/meta_model_v3.pkl",  "sklearn v3", AI_ALL_COLS),
        ("./ai_data/meta_model_v3.json", "xgboost v3", AI_ALL_COLS),
        ("./ai_data/meta_model_v2.pkl",  "sklearn v2", AI_MACRO_COLS),
        ("./ai_data/meta_model_v2.json", "xgboost v2", AI_MACRO_COLS),
    ]
    for path, label, cols in candidates:
        if not os.path.exists(path):
            continue
        if cols is None:
            cols = _load_v4_cols(v4_base)
        try:
            if path.endswith(".pkl"):
                import joblib
                return joblib.load(path), label, cols
            else:
                import xgboost as xgb
                m = xgb.XGBClassifier()
                m.load_model(path)
                return m, label, cols
        except Exception as e:
            print(f"  [AI] {label} 로드 실패: {e}")
    return None, None, AI_MACRO_COLS


def _load_ai_features(use_cols):
    """매크로 (historical_features.csv) + 종목별 (trades_history_v2.csv) feature dict."""
    macro_map, stock_map = {}, {}
    # 매크로
    macro_path = "./ai_data/historical_features.csv"
    if os.path.exists(macro_path):
        try:
            df = pd.read_csv(macro_path)
            df["date"] = df["date"].astype(str)
            avail = [c for c in AI_MACRO_COLS if c in df.columns]
            if avail:
                macro_map = df.set_index("date")[avail].to_dict("index")
        except Exception as e:
            print(f"  [AI] macro features 로드 실패: {e}")
    # 종목별 (trades_history_v2.csv 의 사전 계산값 활용)
    stock_path = "./trades_history_v2.csv"
    if any(c in use_cols for c in AI_STOCK_COLS) and os.path.exists(stock_path):
        try:
            df = pd.read_csv(stock_path, dtype={"code": str})
            df["date"] = df["date"].astype(str)
            df["code"] = df["code"].astype(str).str.zfill(6)
            avail = [c for c in AI_STOCK_COLS if c in df.columns]
            if avail:
                stock_map = df.set_index(["code", "date"])[avail].to_dict("index")
        except Exception as e:
            print(f"  [AI] stock features 로드 실패: {e}")
    return macro_map, stock_map


def _ai_predict(model, macro_map, stock_map, use_cols, code, signal_date):
    """AI 성공 확률 (%). 모델/feature 없으면 None."""
    if model is None or not macro_map:
        return None
    m = macro_map.get(str(signal_date))
    if not m:
        return None
    s = stock_map.get((str(code).zfill(6), str(signal_date)), {})
    feat = {}
    feat.update(m)
    feat.update(s)
    try:
        import numpy as np
        X = np.array([[feat.get(c, 0.0) for c in use_cols]])
        # NaN 처리
        if np.any(np.isnan(X)):
            return None
        prob = float(model.predict_proba(X)[0][1])
        return round(prob * 100, 1)
    except Exception:
        return None


def _load_recent_logs(log_dir="./logs", n=7):
    """logs/paper_*.log 마지막 N개 파일 읽기 → 메타 + 본문 (tail 50줄)."""
    import glob
    if not os.path.exists(log_dir):
        return []
    files = sorted(glob.glob(f"{log_dir}/paper_*.log"))[-n:]
    result = []
    for fp in reversed(files):  # 최신부터
        try:
            stat = os.stat(fp)
            with open(fp, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            # exit code 찾기
            exit_code = None
            for line in lines:
                if "Exit code" in line:
                    try:
                        exit_code = int(line.split(":")[1].strip().split()[0])
                    except Exception:
                        pass
            # 단계별 상태 추출
            stages = []
            for line in lines:
                ln = line.strip()
                if ln.startswith("=== ") and ln.endswith("==="):
                    stages.append(ln.replace("=", "").strip())
            tail = "".join(lines[-50:])
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            result.append({
                "filename": os.path.basename(fp),
                "mtime": mtime,
                "size_kb": round(stat.st_size / 1024, 1),
                "exit_code": exit_code,
                "stages": stages,
                "tail": tail,
            })
        except Exception as e:
            result.append({"filename": os.path.basename(fp), "error": str(e)})
    return result


def _check_health(logs):
    """작동 여부 평가 — 최근 로그 기준."""
    from datetime import datetime as dt, timedelta
    if not logs:
        return {"status": "🔴 미실행", "msg": "로그 없음. install_scheduler.ps1 실행 필요"}
    latest = logs[0]
    if "error" in latest:
        return {"status": "🟡 로그 읽기 실패", "msg": latest.get("error", "")}
    # mtime 으로 마지막 실행 판단
    try:
        last_run = dt.strptime(latest["mtime"], "%Y-%m-%d %H:%M:%S")
        hours_ago = (dt.now() - last_run).total_seconds() / 3600
    except Exception:
        hours_ago = 999
    exit_code = latest.get("exit_code")
    if hours_ago > 72:
        return {"status": "🔴 장기 미실행", "msg": f"{hours_ago:.0f}시간 전 마지막 실행"}
    elif hours_ago > 36:
        return {"status": "🟡 휴장? 점검 필요", "msg": f"{hours_ago:.0f}시간 전 마지막 실행"}
    if exit_code is not None and exit_code != 0:
        return {"status": "🟡 마지막 실행 에러", "msg": f"exit code {exit_code}"}
    return {"status": "🟢 정상", "msg": f"마지막 실행 {hours_ago:.1f}시간 전, exit 0"}


def _ffloat(v, default=0.0):
    """문자열/None/NaN 등 어떤 값이 와도 float 로 안전 변환(실패 시 default). 크래시 방지."""
    try:
        f = float(v)
        return f if f == f else default   # NaN 거르기
    except (TypeError, ValueError):
        return default


def _load_pending_signals(signals_df, code_dates, price_map, name_cache, n=20):
    """매매 예정 종목 — paper_signals.csv 의 최근 signal_date 행들."""
    if signals_df.empty:
        return []
    latest_date = signals_df["signal_date"].astype(str).max()
    recent = signals_df[signals_df["signal_date"].astype(str) == latest_date].head(n)
    items = []
    for _, sig in recent.iterrows():
        code = str(sig["code"]).zfill(6)
        sig_close = price_map.get((code, latest_date), {}).get("close")
        items.append({
            "code": code,
            "name": name_cache.get(code, _safe_name(sig.get("name"))),
            "signal_date": latest_date,
            "sig_close": int(sig_close) if sig_close else None,
            "lookback_high": _ffloat(sig.get("lookback_high", 0)),
        })
    return items, latest_date


def _calc_shares_amounts(entry_price, exit_or_current_price):
    """주식 수 + 매수/매도(평가) 금액 + 잔액 + 손익 계산.
    슬롯 베팅 모델: floor(PER_SLOT / entry_price) 주.
    """
    if not entry_price or entry_price <= 0:
        return None
    shares = int(PER_SLOT // entry_price)
    buy_amt = shares * entry_price
    cash_left = PER_SLOT - buy_amt
    if not exit_or_current_price or exit_or_current_price <= 0:
        return {
            "shares": shares, "buy_amount": buy_amt, "cash_left": cash_left,
            "sell_amount": None, "gross_profit": None, "cost": None, "net_profit": None,
        }
    sell_amt = shares * exit_or_current_price
    gross = sell_amt - buy_amt
    # 비용: (매수액 + 매도액) × 0.165% = COST_PCT 의 절반씩 양방향 (현실적)
    cost = (buy_amt + sell_amt) * (COST_PCT / 2 / 100)
    net = gross - cost
    return {
        "shares": shares, "buy_amount": buy_amt, "cash_left": cash_left,
        "sell_amount": sell_amt, "gross_profit": gross, "cost": cost, "net_profit": net,
    }


def _load_name_cache(path=NAME_CACHE_CSV):
    """code → name dict. 없으면 {}."""
    if not os.path.exists(path):
        return {}
    df = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")
    df["code"] = df["code"].astype(str).str.zfill(6)
    return dict(zip(df["code"], df["name"]))


def _safe_name(v):
    """NaN/float/None → 빈 문자열."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v)


def _calc_exit_meta(code, entry_date, exit_date, current_date,
                    dates_list, price_map, entry_price):
    """청산/현재 시점 메타 — 보유 중 고저점, 최고가 대비 drawdown, 청산 후 N일."""
    try:
        entry_idx = dates_list.index(entry_date)
    except ValueError:
        return {}

    # 청산일 또는 현재일 인덱스
    end_date = exit_date if exit_date else current_date
    try:
        end_idx = dates_list.index(end_date)
    except ValueError:
        return {}

    # 보유 기간 (entry+1 ~ end) 의 high/low/close
    peak_high, peak_high_date = entry_price, entry_date
    trough_low, trough_low_date = entry_price, entry_date
    for d in dates_list[entry_idx:end_idx + 1]:
        h = price_map.get((code, d), {}).get("high", 0) or 0
        l = price_map.get((code, d), {}).get("low", 0) or 0
        if h > peak_high:
            peak_high, peak_high_date = float(h), d
        if l > 0 and (trough_low == entry_price or l < trough_low):
            trough_low, trough_low_date = float(l), d

    end_price = price_map.get((code, end_date), {}).get("close", 0) or 0
    drawdown_from_peak = ((end_price / peak_high) - 1) * 100 if peak_high > 0 else None

    # 청산 후 5일 가격 (있으면)
    after5_pct = None
    if exit_date and end_idx + 5 < len(dates_list):
        after5_date = dates_list[end_idx + 5]
        after5_p = price_map.get((code, after5_date), {}).get("close", 0) or 0
        if after5_p > 0 and end_price > 0:
            after5_pct = ((after5_p / end_price) - 1) * 100

    return {
        "peak_high": round(peak_high, 0),
        "peak_high_date": peak_high_date,
        "trough_low": round(trough_low, 0),
        "drawdown_from_peak": round(drawdown_from_peak, 2) if drawdown_from_peak is not None else None,
        "after5_pct": round(after5_pct, 2) if after5_pct is not None else None,
    }


def _calc_reason_meta(code, sig_date, dates_list, price_map, entry_price_close):
    """매매 근거 메타 즉석 계산 — signal_date 기준 직전 500일 고점, 돌파율, 거래대금."""
    try:
        sig_idx = dates_list.index(sig_date)
    except ValueError:
        return {}
    # 직전 LOOKBACK_DAYS 일의 high 최대값 (signal_date 포함 안 함 = 진짜 직전)
    start = max(0, sig_idx - LOOKBACK_DAYS)
    window = dates_list[start:sig_idx]
    if not window:
        return {}
    prior_high = 0.0
    prior_high_date = None
    for d in window:
        h = price_map.get((code, d), {}).get("high", 0) or 0
        if h > prior_high:
            prior_high = float(h)
            prior_high_date = d
    breakout_pct = ((entry_price_close / prior_high) - 1) * 100 if prior_high > 0 else None
    trading_value = price_map.get((code, sig_date), {}).get("trading_value", 0) or 0
    return {
        "prior_high": round(prior_high, 0) if prior_high else None,
        "prior_high_date": prior_high_date,
        "breakout_pct": round(breakout_pct, 2) if breakout_pct is not None else None,
        "trading_value_eok": round(float(trading_value) / 1e8, 1) if trading_value else None,
    }


def build_trades(signals_df, code_dates, price_map, name_cache=None,
                  ai_model=None, ai_macro_map=None, ai_stock_map=None,
                  ai_use_cols=None, ca_map=None):
    """signals → 매매 변환. AI 점수 부여 (모델 있으면).

    [2026-06-12 교정] ca_map 전달 시 보유 중 기업행위(액면분할 등) 발생 매매를
    청산 통계에서 제외 (무수정주가 왜곡 방어 — 백테스트/페이퍼와 동일 정책).
    """
    from strategies._swing_base import _trade_has_corporate_action
    ca_map = ca_map or {}
    name_cache = name_cache or {}
    ai_macro_map = ai_macro_map or {}
    ai_stock_map = ai_stock_map or {}
    ai_use_cols = ai_use_cols or AI_MACRO_COLS
    trades = []
    for _, sig in signals_df.iterrows():
        code = sig["code"]
        sig_date = sig["signal_date"]
        if code not in code_dates:
            continue
        dates_list = code_dates[code]

        # X2 모드: T 시간외 매수 / 원본: T+1 시가 매수
        if ENTRY_AT_CLOSE:
            if sig_date not in dates_list:
                continue
            entry_date = sig_date
            sig_close_raw = price_map.get((code, sig_date), {}).get("close")
            if not sig_close_raw or sig_close_raw <= 0:
                continue
            entry_p = sig_close_raw * (1 + ENTRY_SLIPPAGE_PCT / 100)
            idx = dates_list.index(sig_date)
            holding_offset = HOLDING_DAYS - 1   # T+39 종가
        else:
            next_dates = [d for d in dates_list if d > sig_date]
            if not next_dates:
                continue
            entry_date = next_dates[0]
            entry_p = price_map.get((code, entry_date), {}).get("open")
            if not entry_p or entry_p <= 0:
                continue
            idx = dates_list.index(entry_date)
            holding_offset = HOLDING_DAYS - 1   # [fix] 진입일 포함 40영업일째 (백테스트와 통일)

        # name: csv → name_cache fallback
        nm = _safe_name(sig.get("name"))
        if not nm and code in name_cache:
            nm = name_cache[code]

        # 신호일 종가
        sig_close = price_map.get((code, sig_date), {}).get("close", entry_p) or entry_p

        # 매매 근거 메타 (즉석 계산)
        reason = _calc_reason_meta(code, sig_date, dates_list, price_map, sig_close)
        try:
            market_strong = str(sig.get("market_strong", "")).strip().lower() in ("true", "1")
        except Exception:
            market_strong = False

        reason["strategy"] = "high_500d_h40_MKT"
        reason["market_strong"] = market_strong
        reason["sig_close"] = round(float(sig_close), 0)
        # AI 점수 (signal_date 기준 매크로 feature → 성공 확률)
        reason["ai_score"] = _ai_predict(ai_model, ai_macro_map, ai_stock_map,
                                          ai_use_cols, code, sig_date)

        exit_idx = idx + holding_offset
        if exit_idx >= len(dates_list):
            current_p = price_map.get((code, dates_list[-1]), {}).get("close", entry_p)
            mtm_pct = (current_p / entry_p - 1) * 100
            qty = _calc_shares_amounts(entry_p, current_p)
            exit_meta = _calc_exit_meta(code, entry_date, None, dates_list[-1],
                                         dates_list, price_map, entry_p)
            exit_meta["rule"] = f"보유 중 ({len(dates_list) - idx - 1}/{HOLDING_DAYS}일)"
            trades.append({
                "code": code, "name": nm,
                "signal_date": sig_date, "entry_date": entry_date,
                "entry_price": float(entry_p),
                "exit_date": None, "exit_price": None,
                "current_price": float(current_p),
                "mtm_pct": float(mtm_pct),
                "net_pct": None, "status": "open",
                "holding_so_far": len(dates_list) - idx - 1,
                "reason": reason,
                "qty": qty,
                "exit_meta": exit_meta,
            })
        else:
            exit_date = dates_list[exit_idx]
            exit_p = price_map.get((code, exit_date), {}).get("close")
            if not exit_p or exit_p <= 0:
                continue
            # 기업행위(액면분할 등) 포함 매매 — 가격 왜곡이라 통계 제외
            if ca_map and _trade_has_corporate_action(ca_map, code, entry_date, exit_date):
                continue
            gross_pct = (exit_p / entry_p - 1) * 100
            net_pct = gross_pct - COST_PCT
            qty = _calc_shares_amounts(entry_p, exit_p)
            exit_meta = _calc_exit_meta(code, entry_date, exit_date, None,
                                         dates_list, price_map, entry_p)
            exit_meta["rule"] = f"보유 {HOLDING_DAYS}일 만기 → 종가 청산"
            trades.append({
                "code": code, "name": nm,
                "signal_date": sig_date, "entry_date": entry_date,
                "entry_price": float(entry_p),
                "exit_date": exit_date, "exit_price": float(exit_p),
                "current_price": float(exit_p),
                "mtm_pct": float(gross_pct),
                "net_pct": float(net_pct), "status": "closed",
                "holding_so_far": HOLDING_DAYS,
                "reason": reason,
                "qty": qty,
                "exit_meta": exit_meta,
            })
    return trades


def build_equity_curve(closed_trades):
    """청산 매매를 entry_date 순으로 정렬해 누적 net_pct 곡선."""
    rows = sorted(closed_trades, key=lambda t: t["entry_date"])
    points = [{"date": "start", "equity": INITIAL_CAPITAL}]
    cap = INITIAL_CAPITAL
    n_active = 0
    per_slot = INITIAL_CAPITAL / MAX_CONCURRENT  # 단순화 모델
    for t in rows:
        # 단순화: 각 매매 자본 1/N 베팅 가정
        invest = per_slot
        result = invest * (1 + t["net_pct"] / 100)
        cap = cap - invest + result   # 거의 동일 (delta = invest × net_pct/100)
        points.append({
            "date": t["exit_date"],
            "equity": round(cap, 0),
            "code": t["code"],
            "net_pct": round(t["net_pct"], 2),
        })
    return points


def build_holding_history(open_trades, price_map, code_dates):
    """보유 중인 종목의 entry 이후 일별 종가 시계열."""
    result = []
    for t in open_trades[:20]:  # 너무 많으면 자름
        code = t["code"]
        entry_date = t["entry_date"]
        if code not in code_dates:
            continue
        dates_list = code_dates[code]
        try:
            start_idx = dates_list.index(entry_date)
        except ValueError:
            continue
        series = []
        for d in dates_list[start_idx:]:
            close = price_map.get((code, d), {}).get("close")
            if close and close > 0:
                pct = (close / t["entry_price"] - 1) * 100
                series.append({"date": d, "close": float(close), "pct": round(pct, 2)})
        result.append({
            "code": code,
            "name": t["name"],
            "entry_date": entry_date,
            "entry_price": t["entry_price"],
            "series": series,
        })
    return result


def _render_ops_tab(logs, health, pending_items, pending_date):
    """작동 현황 탭 HTML."""
    from datetime import datetime as dt, timedelta
    # 헬스 박스 색상
    h_status = health.get("status", "N/A")
    h_class = "err" if "🔴" in h_status else ("warn" if "🟡" in h_status else "")

    # 다음 실행 예정 시각 (다음 평일 16:30)
    now = dt.now()
    next_run = now.replace(hour=16, minute=30, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    while next_run.weekday() >= 5:  # 토/일 스킵
        next_run += timedelta(days=1)
    next_run_str = next_run.strftime("%Y-%m-%d (%a) %H:%M")

    # 로그 row
    log_rows = ""
    if not logs:
        log_rows = '<div class="log-row"><div class="log-meta"><span style="color:#8b939e;">로그 없음. 자동 실행 시작되면 logs/ 폴더에 누적</span></div></div>'
    for i, lg in enumerate(logs):
        if "error" in lg:
            log_rows += f'<div class="log-row"><div class="log-meta"><strong>{lg["filename"]}</strong> <span style="color:#f87171;">읽기 실패: {lg.get("error","")}</span></div></div>'
            continue
        exit_code = lg.get("exit_code")
        exit_str = f"exit {exit_code}" if exit_code is not None else "exit ?"
        exit_color = "#4ade80" if exit_code == 0 else "#f87171"
        stages_str = " · ".join(lg.get("stages", [])) or "(단계 정보 없음)"
        tail_safe = (lg.get("tail", "")
                     .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        log_rows += f"""
        <div class="log-row" onclick="this.classList.toggle('open')">
          <div class="log-meta">
            <strong>📄 {lg["filename"]}</strong>
            <span class="log-tag">{lg["mtime"]}</span>
            <span class="log-tag">{lg["size_kb"]} KB</span>
            <span class="log-tag" style="color:{exit_color}">{exit_str}</span>
            <span style="font-size:11px; color:#8b939e; margin-left:auto;">{stages_str}</span>
          </div>
          <div class="log-tail">{tail_safe}</div>
        </div>"""

    # 매매 예정 종목
    pending_rows = ""
    if not pending_items:
        pending_rows = '<tr><td colspan="5" style="text-align:center; color:#666;">신호 없음</td></tr>'
    for p in pending_items:
        sig_close_str = f"{p['sig_close']:,}원" if p.get('sig_close') else "-"
        lookback_str = f"{int(p['lookback_high']):,}원" if p.get('lookback_high') else "-"
        pending_rows += f"""
        <tr>
          <td>{p['code']}</td>
          <td>{p['name'][:15] if p['name'] else '-'}</td>
          <td>{p['signal_date']}</td>
          <td>{sig_close_str}</td>
          <td>{lookback_str}</td>
        </tr>"""

    pending_title = f"매매 예정 종목 (signal_date: {pending_date})" if pending_date else "매매 예정 종목 (신호 없음)"

    return f"""
    <div class="health-card">
      <div class="health-box {h_class}">
        <h3>작동 상태</h3>
        <p>{h_status}</p>
        <small>{health.get("msg", "")}</small>
      </div>
      <div class="health-box">
        <h3>마지막 로그</h3>
        <p>{logs[0]["filename"] if logs and "filename" in logs[0] else "—"}</p>
        <small>{logs[0]["mtime"] if logs and "mtime" in logs[0] else "로그 없음"}</small>
      </div>
      <div class="health-box">
        <h3>다음 실행 예정</h3>
        <p>{next_run_str}</p>
        <small>매 평일 16:30 자동 실행</small>
      </div>
    </div>

    <div class="card">
      <h2>📅 {pending_title}</h2>
      <p style="font-size:12px; color:#8b939e; margin: -8px 0 12px 0;">
        signal_date 의 종가 기준 시간외 (T 종가 × 1.02) 매수 예정 — paper trading 시뮬레이션
      </p>
      <table>
        <thead><tr>
          <th>코드</th><th>종목</th><th>신호일</th>
          <th>신호일 종가</th><th>직전 신고가</th>
        </tr></thead>
        <tbody>{pending_rows}</tbody>
      </table>
    </div>

    <div class="card">
      <h2>📜 최근 작동 로그 (행 클릭 시 본문 펼침)</h2>
      {log_rows}
    </div>
    """


def render_html(kpi, equity_pts, open_trades, closed_recent, holdings_hist,
                 logs=None, health=None, pending_items=None, pending_date=None):
    """HTML 문자열 생성."""
    logs = logs or []
    health = health or {"status": "N/A", "msg": ""}
    pending_items = pending_items or []
    eq_labels = json.dumps([p["date"] for p in equity_pts])
    eq_values = json.dumps([p["equity"] for p in equity_pts])

    # 보유 포지션 표 (▶ 클릭 → 매수 근거 + 현재 평가 row 펼침)
    open_rows = ""
    for j, t in enumerate(sorted(open_trades, key=lambda x: x["mtm_pct"], reverse=True)):
        cls = "win" if t["mtm_pct"] >= 0 else "loss"
        q = t.get("qty") or {}
        shares = q.get("shares", 0)
        buy_amt = q.get("buy_amount", 0)
        cur_amt = q.get("sell_amount") or (shares * t["current_price"])
        pnl_won = (cur_amt or 0) - (buy_amt or 0)
        r = t.get("reason") or {}
        em = t.get("exit_meta") or {}
        prior_high = r.get("prior_high")
        breakout = r.get("breakout_pct")
        tv = r.get("trading_value_eok")
        mkt = "✅" if r.get("market_strong") else "❌"
        strat = r.get("strategy", "-")
        sig_close = r.get("sig_close")
        peak = em.get("peak_high")
        peak_date = em.get("peak_high_date", "-")
        dd = em.get("drawdown_from_peak")
        open_rows += f"""
        <tr class="trade-row" onclick="toggleOpenReason({j})">
          <td><span class="toggle" id="otg{j}">▶</span></td>
          <td>{t['code']}</td><td>{_safe_name(t['name'])[:15]}</td>
          <td>{t['entry_date']}</td>
          <td>{int(t['entry_price']):,}</td>
          <td>{int(t['current_price']):,}</td>
          <td>{shares:,}주</td>
          <td>{int(buy_amt):,}원</td>
          <td>{int(cur_amt):,}원</td>
          <td class="{cls}">{int(pnl_won):+,}원</td>
          <td class="{cls}">{t['mtm_pct']:+.2f}%</td>
          <td>{t['holding_so_far']}일</td>
        </tr>
        <tr class="reason-row" id="ors{j}" style="display:none;">
          <td colspan="12">
            <div class="reason-box">
              <div class="reason-twocol">
                <div>
                  <div class="reason-title">📥 매수 근거</div>
                  <div class="reason-grid">
                    <div><span class="lbl">전략</span><span class="val">{strat}</span></div>
                    <div><span class="lbl">신호일 종가</span><span class="val">{int(sig_close) if sig_close else 0:,}원</span></div>
                    <div><span class="lbl">직전 500일 고점</span><span class="val">{int(prior_high) if prior_high else 0:,}원</span></div>
                    <div><span class="lbl">돌파율</span><span class="val">{(breakout or 0):+.2f}%</span></div>
                    <div><span class="lbl">거래대금</span><span class="val">{(tv or 0):.1f}억원</span></div>
                    <div><span class="lbl">시장 게이트</span><span class="val">{mkt}</span></div>
                  </div>
                </div>
                <div>
                  <div class="reason-title">📊 현재 평가 + 🤖 AI</div>
                  <div class="reason-grid">
                    <div><span class="lbl">상태</span><span class="val">{em.get('rule','보유 중')}</span></div>
                    <div><span class="lbl">보유 중 최고가</span><span class="val">{int(peak) if peak else 0:,}원</span></div>
                    <div><span class="lbl">최고가 대비</span><span class="val">{(dd if dd is not None else 0):+.2f}%</span></div>
                    <div><span class="lbl">진입 대비</span><span class="val">{t['mtm_pct']:+.2f}%</span></div>
                    <div><span class="lbl">🤖 AI 예측 (진입시)</span><span class="val">{(str(r.get('ai_score','-')) + '%') if r.get('ai_score') is not None else 'N/A'}</span></div>
                    <div><span class="lbl">평가 손익</span><span class="val">{int(pnl_won):+,}원</span></div>
                  </div>
                </div>
              </div>
            </div>
          </td>
        </tr>"""

    # 최근 매매 이력 (▼ 클릭 → 근거 row 펼침)
    closed_rows = ""
    for i, t in enumerate(sorted(closed_recent, key=lambda x: x["exit_date"], reverse=True)[:50]):
        cls = "win" if t["net_pct"] >= 0 else "loss"
        r = t.get("reason") or {}
        q = t.get("qty") or {}
        prior_high = r.get("prior_high")
        breakout = r.get("breakout_pct")
        tv = r.get("trading_value_eok")
        mkt = "✅" if r.get("market_strong") else "❌"
        strat = r.get("strategy", "-")
        sig_close = r.get("sig_close")
        ph_date = r.get("prior_high_date") or "-"
        shares = q.get("shares", 0)
        buy_amt = q.get("buy_amount", 0)
        sell_amt = q.get("sell_amount", 0)
        net_won = q.get("net_profit", 0) or 0
        cost = q.get("cost", 0) or 0
        cash_left = q.get("cash_left", 0) or 0
        main_row = f"""
        <tr class="trade-row" onclick="toggleReason({i})">
          <td><span class="toggle" id="tg{i}">▶</span></td>
          <td>{t['code']}</td><td>{_safe_name(t['name'])[:15]}</td>
          <td>{t['entry_date']}</td>
          <td>{t['exit_date']}</td>
          <td>{int(t['entry_price']):,}</td>
          <td>{int(t['exit_price']):,}</td>
          <td>{shares:,}주</td>
          <td>{int(buy_amt):,}원</td>
          <td>{int(sell_amt):,}원</td>
          <td class="{cls}">{int(net_won):+,}원</td>
          <td class="{cls}">{t['net_pct']:+.2f}%</td>
        </tr>"""
        em = t.get("exit_meta") or {}
        peak = em.get("peak_high")
        peak_date = em.get("peak_high_date", "-")
        dd = em.get("drawdown_from_peak")
        after5 = em.get("after5_pct")
        exit_rule = em.get("rule", f"보유 {HOLDING_DAYS}일 만기")
        after5_str = f"{after5:+.2f}%" if after5 is not None else "데이터 없음"
        ai_score = r.get("ai_score")
        if ai_score is None:
            ai_str = "N/A (모델/feature 없음)"
            ai_verdict = "-"
        else:
            ai_str = f"{ai_score:.1f}%"
            # 실제 결과 vs 예측 비교
            actual_ok = t["net_pct"] > 0
            pred_ok = ai_score >= 50
            if pred_ok == actual_ok:
                ai_verdict = "✅ 적중" if actual_ok else "✅ 적중 (실패 예측)"
            else:
                ai_verdict = "❌ 빗나감"
        if all(x is not None for x in (prior_high, breakout, tv, sig_close)):
            reason_row = f"""
        <tr class="reason-row" id="rs{i}" style="display:none;">
          <td colspan="12">
            <div class="reason-box">
              <div class="reason-twocol">
                <div>
                  <div class="reason-title">📥 매수 근거</div>
                  <div class="reason-grid">
                    <div><span class="lbl">전략</span><span class="val">{strat}</span></div>
                    <div><span class="lbl">신호일 종가</span><span class="val">{int(sig_close):,}원</span></div>
                    <div><span class="lbl">직전 500일 고점</span><span class="val">{int(prior_high):,}원 ({ph_date})</span></div>
                    <div><span class="lbl">돌파율</span><span class="val">{breakout:+.2f}%</span></div>
                    <div><span class="lbl">거래대금</span><span class="val">{tv:.1f}억원 (필터 30억+)</span></div>
                    <div><span class="lbl">시장 게이트</span><span class="val">KOSDAQ MA60 {mkt}</span></div>
                  </div>
                </div>
                <div>
                  <div class="reason-title">📤 청산 근거 + 🤖 AI</div>
                  <div class="reason-grid">
                    <div><span class="lbl">청산 규칙</span><span class="val">{exit_rule}</span></div>
                    <div><span class="lbl">보유 중 최고가</span><span class="val">{int(peak) if peak else 0:,}원</span></div>
                    <div><span class="lbl">최고가 대비 청산</span><span class="val">{(dd if dd is not None else 0):+.2f}%</span></div>
                    <div><span class="lbl">청산 후 5일</span><span class="val">{after5_str}</span></div>
                    <div><span class="lbl">🤖 AI 예측</span><span class="val">{ai_str} (성공 확률)</span></div>
                    <div><span class="lbl">🤖 AI 적중 여부</span><span class="val">{ai_verdict}</span></div>
                    <div><span class="lbl">거래 비용</span><span class="val">{int(cost):,}원</span></div>
                    <div><span class="lbl">실현 손익</span><span class="val">{int(net_won):+,}원</span></div>
                  </div>
                </div>
              </div>
            </div>
          </td>
        </tr>"""
        else:
            reason_row = f"""
        <tr class="reason-row" id="rs{i}" style="display:none;">
          <td colspan="12"><div class="reason-box"><em style="color:#8b939e">매수 근거 메타 부족 (macro_data 범위 밖). 청산 규칙: {exit_rule}, 보유 중 최고가 대비 청산: {(dd if dd is not None else 0):+.2f}%</em></div></td>
        </tr>"""
        closed_rows += main_row + reason_row

    # 보유 종목 차트 데이터
    holdings_data = json.dumps(holdings_hist)

    # 기간 필터 + AI 컷오프 시뮬용 — 매매 이력 JSON 임베드 (ai_score 포함)
    period_trades = json.dumps([
        {
            "entry_date": t["entry_date"],
            "exit_date": t["exit_date"],
            "net_pct": float(t["net_pct"]),
            "code": t["code"],
            "ai_score": (t.get("reason") or {}).get("ai_score"),
        }
        for t in closed_recent
        if t.get("net_pct") is not None and t.get("exit_date")
    ])

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>천억이의 대시보드 — high_500d_h40_MKT</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body {{ font-family: -apple-system, "Malgun Gothic", sans-serif;
            margin: 0; background: #0f1115; color: #e8eaed; }}
    .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
    h1 {{ font-size: 24px; margin: 0 0 4px 0; }}
    .subtitle {{ color: #8b939e; font-size: 13px; margin-bottom: 20px; }}
    .kpi-grid {{ display: grid; grid-template-columns: repeat(7, 1fr);
                gap: 12px; margin-bottom: 20px; }}
    .kpi {{ background: #1a1e26; padding: 14px; border-radius: 8px;
            border-left: 3px solid #4a9eff; }}
    .kpi h3 {{ font-size: 11px; color: #8b939e; margin: 0 0 4px 0;
              text-transform: uppercase; letter-spacing: 0.5px; }}
    .kpi p {{ font-size: 18px; font-weight: 600; margin: 0; }}
    .card {{ background: #1a1e26; padding: 20px; border-radius: 8px;
            margin-bottom: 20px; }}
    .card h2 {{ font-size: 16px; margin: 0 0 14px 0; color: #e8eaed; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th {{ text-align: left; padding: 6px 5px; color: #8b939e;
          border-bottom: 1px solid #2a2f3a; font-weight: 500; white-space: nowrap; }}
    td {{ padding: 6px 5px; border-bottom: 1px solid #2a2f3a; white-space: nowrap; }}
    .win {{ color: #4ade80; }}
    .loss {{ color: #f87171; }}
    .chart-wrap {{ height: 320px; }}
    .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
    .trade-row {{ cursor: pointer; }}
    .trade-row:hover {{ background: #20242e; }}
    .toggle {{ display: inline-block; transition: transform 0.15s;
              color: #8b939e; font-size: 10px; user-select: none; }}
    .toggle.open {{ transform: rotate(90deg); }}
    .reason-row td {{ padding: 0; background: #0f1115; border-bottom: 1px solid #2a2f3a; }}
    .reason-box {{ padding: 12px 14px; }}
    .reason-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px 24px; }}
    .reason-grid .lbl {{ color: #8b939e; font-size: 11px; margin-right: 8px;
                         text-transform: uppercase; letter-spacing: 0.3px; }}
    .reason-grid .val {{ color: #e8eaed; font-size: 13px; font-weight: 500; }}
    .reason-twocol {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
    .reason-title {{ font-size: 12px; color: #4a9eff; font-weight: 600;
                      margin-bottom: 8px; padding-bottom: 6px;
                      border-bottom: 1px solid #2a2f3a; }}
    @media (max-width: 1000px) {{ .reason-twocol {{ grid-template-columns: 1fr; }} }}
    .period-bar {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
                   margin-bottom: 14px; padding-bottom: 14px;
                   border-bottom: 1px solid #2a2f3a; }}
    .period-btn {{ background: #1a1e26; color: #8b939e; border: 1px solid #2a2f3a;
                   padding: 6px 12px; border-radius: 6px; font-size: 12px;
                   cursor: pointer; transition: all 0.15s; }}
    .period-btn:hover {{ background: #20242e; color: #e8eaed; }}
    .period-btn.active {{ background: #4a9eff; color: #fff; border-color: #4a9eff; }}
    .ai-btn {{ background: #1a1e26; color: #8b939e; border: 1px solid #2a2f3a;
              padding: 6px 12px; border-radius: 6px; font-size: 12px;
              cursor: pointer; transition: all 0.15s; }}
    .ai-btn:hover {{ background: #20242e; color: #e8eaed; }}
    .ai-btn.active {{ background: #a78bfa; color: #fff; border-color: #a78bfa; }}
    .tabs {{ display: flex; gap: 4px; margin-bottom: 16px;
             border-bottom: 1px solid #2a2f3a; }}
    .tab-btn {{ background: transparent; color: #8b939e; border: none;
                padding: 10px 18px; font-size: 14px; cursor: pointer;
                border-bottom: 2px solid transparent; transition: all 0.15s; }}
    .tab-btn:hover {{ color: #e8eaed; }}
    .tab-btn.active {{ color: #4a9eff; border-bottom-color: #4a9eff; font-weight: 600; }}
    .tab-pane {{ display: none !important; }}
    .tab-pane.active {{ display: block !important; }}
    .log-row {{ background: #1a1e26; padding: 10px 14px; border-radius: 6px;
                margin-bottom: 8px; cursor: pointer; transition: background 0.1s; }}
    .log-row:hover {{ background: #20242e; }}
    .log-row.open .log-tail {{ display: block; }}
    .log-tail {{ display: none; background: #0a0d11; color: #c5cad3;
                  padding: 10px; border-radius: 4px; font-family: monospace;
                  font-size: 11px; white-space: pre-wrap; margin-top: 8px;
                  max-height: 400px; overflow-y: auto; }}
    .log-meta {{ display: flex; gap: 14px; align-items: center;
                  font-size: 13px; color: #e8eaed; }}
    .log-tag {{ background: #2a2f3a; color: #c5cad3; padding: 2px 8px;
                border-radius: 4px; font-size: 11px; }}
    .health-card {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px;
                     margin-bottom: 20px; }}
    .health-box {{ background: #1a1e26; padding: 16px; border-radius: 8px;
                    border-left: 3px solid #4a9eff; }}
    .health-box.warn {{ border-left-color: #fbbf24; }}
    .health-box.err {{ border-left-color: #f87171; }}
    .health-box h3 {{ font-size: 12px; color: #8b939e; margin: 0 0 6px 0;
                       text-transform: uppercase; letter-spacing: 0.3px; }}
    .health-box p {{ font-size: 16px; font-weight: 600; margin: 0; }}
    .health-box small {{ display: block; margin-top: 4px; color: #8b939e;
                          font-size: 11px; font-weight: 400; }}
    .period-bar .custom {{ margin-left: auto; display: flex; align-items: center; gap: 6px;
                           color: #8b939e; font-size: 12px; }}
    .period-bar input[type=date] {{ background: #0f1115; color: #e8eaed;
                                     border: 1px solid #2a2f3a; padding: 4px 8px;
                                     border-radius: 4px; font-size: 12px; }}
    .period-kpi {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }}
    .period-kpi .box {{ background: #0f1115; padding: 10px 12px; border-radius: 6px;
                         border-left: 2px solid #4a9eff; }}
    .period-kpi .lbl {{ font-size: 10px; color: #8b939e; text-transform: uppercase;
                         letter-spacing: 0.3px; margin-bottom: 3px; }}
    .period-kpi .val {{ font-size: 15px; color: #e8eaed; font-weight: 600; }}
    @media (max-width: 1000px) {{ .period-kpi {{ grid-template-columns: repeat(2, 1fr); }} }}
    @media (max-width: 1000px) {{
      .kpi-grid {{ grid-template-columns: repeat(3, 1fr); }}
      .grid2 {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <h1>📊 천억이의 대시보드</h1>
    <div class="subtitle">메인 전략: high_500d_h40_MKT (X2 시간외 +2%) · 자본: 1,000만원 · 마지막 업데이트: {now}</div>

    <div class="tabs">
      <button class="tab-btn active" data-tab="perf" onclick="switchTab('perf', this)">📊 성과</button>
      <button class="tab-btn" data-tab="ops" onclick="switchTab('ops', this)">⚙️ 작동 현황</button>
    </div>
    <script>
      // Inline fallback — onclick 핸들러가 안 잡혀도 작동
      (function(){{
        document.querySelectorAll('.tab-btn[data-tab]').forEach(function(btn){{
          btn.addEventListener('click', function(){{
            var name = btn.getAttribute('data-tab');
            document.querySelectorAll('.tab-btn').forEach(function(b){{ b.classList.remove('active'); }});
            document.querySelectorAll('.tab-pane').forEach(function(p){{ p.classList.remove('active'); }});
            btn.classList.add('active');
            var pane = document.getElementById('tab-' + name);
            if (pane) pane.classList.add('active');
          }});
        }});
      }})();
    </script>

    <div id="tab-perf" class="tab-pane active">

    <div class="kpi-grid">
      <div class="kpi"><h3>최종 자본</h3><p>{int(kpi.get('final', INITIAL_CAPITAL)):,}원</p></div>
      <div class="kpi"><h3>총 수익률</h3><p class="{'win' if kpi.get('total_ret_pct',0)>=0 else 'loss'}">{kpi.get('total_ret_pct', 0):+.2f}%</p></div>
      <div class="kpi"><h3>CAGR</h3><p>{kpi.get('cagr_pct', 0):+.2f}%/년</p></div>
      <div class="kpi"><h3>Real MDD</h3><p class="loss">{kpi.get('real_mdd_pct', 0):.2f}%</p></div>
      <div class="kpi"><h3>Sharpe</h3><p>{kpi.get('real_sharpe', 0):.2f}</p></div>
      <div class="kpi"><h3>보유 중</h3><p>{len(open_trades)}/{MAX_CONCURRENT}</p></div>
      <div class="kpi"><h3>누적 매매</h3><p>{len(closed_recent):,}건</p></div>
    </div>

    <div class="card">
      <h2>💰 자본 곡선 + 기간별 수익</h2>
      <div class="period-bar">
        <button class="period-btn" data-days="7" onclick="setPeriod(7,this)">1주</button>
        <button class="period-btn" data-days="30" onclick="setPeriod(30,this)">1개월</button>
        <button class="period-btn" data-days="90" onclick="setPeriod(90,this)">3개월</button>
        <button class="period-btn" data-days="180" onclick="setPeriod(180,this)">6개월</button>
        <button class="period-btn" data-days="365" onclick="setPeriod(365,this)">1년</button>
        <button class="period-btn active" data-days="all" onclick="setPeriod('all',this)">전체</button>
        <span class="custom">
          <input type="date" id="periodStart">
          <span>~</span>
          <input type="date" id="periodEnd">
          <button class="period-btn" onclick="applyCustom()">적용</button>
        </span>
      </div>
      <div class="period-kpi" id="periodKpi">
        <div class="box"><div class="lbl">매매 수</div><div class="val" id="kpiCount">-</div></div>
        <div class="box"><div class="lbl">시작 자본</div><div class="val" id="kpiStart">-</div></div>
        <div class="box"><div class="lbl">종료 자본</div><div class="val" id="kpiEnd">-</div></div>
        <div class="box"><div class="lbl">기간 수익률</div><div class="val" id="kpiRet">-</div></div>
        <div class="box"><div class="lbl">기간 승률</div><div class="val" id="kpiWin">-</div></div>
      </div>
      <div class="chart-wrap" style="margin-top:16px;"><canvas id="equityChart"></canvas></div>
    </div>

    <div class="card">
      <h2>🤖 AI 컷오프 시뮬 <span style="font-size:11px; color:#8b939e; font-weight:normal;">— AI 점수 ≥ 컷오프 매매만 가정 (매매 결정엔 영향 X, 정보만)</span></h2>
      <div class="period-bar">
        <button class="ai-btn active" data-ai="all" onclick="setAiCutoff('all',this)">전체</button>
        <button class="ai-btn" data-ai="50" onclick="setAiCutoff(50,this)">≥50%</button>
        <button class="ai-btn" data-ai="55" onclick="setAiCutoff(55,this)">≥55%</button>
        <button class="ai-btn" data-ai="60" onclick="setAiCutoff(60,this)">≥60%</button>
        <button class="ai-btn" data-ai="65" onclick="setAiCutoff(65,this)">≥65%</button>
        <button class="ai-btn" data-ai="70" onclick="setAiCutoff(70,this)">≥70%</button>
      </div>
      <div class="period-kpi" id="aiKpi">
        <div class="box"><div class="lbl">매매 수</div><div class="val" id="aiCount">-</div></div>
        <div class="box"><div class="lbl">최종 자본</div><div class="val" id="aiFinal">-</div></div>
        <div class="box"><div class="lbl">총 수익률</div><div class="val" id="aiRet">-</div></div>
        <div class="box"><div class="lbl">평균 yield</div><div class="val" id="aiAvg">-</div></div>
        <div class="box"><div class="lbl">승률</div><div class="val" id="aiWin">-</div></div>
      </div>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>📈 현재 보유 포지션 ({len(open_trades)}건) <span style="font-size:11px; color:#8b939e; font-weight:normal;">— 행 클릭 시 근거 펼침</span></h2>
        <table>
          <thead><tr>
            <th style="width:24px;"></th>
            <th>코드</th><th>종목</th><th>진입일</th>
            <th>진입가</th><th>현재가</th>
            <th>수량</th><th>매수액</th><th>평가액</th>
            <th>평가손익</th><th>(%)</th><th>보유</th>
          </tr></thead>
          <tbody>{open_rows or '<tr><td colspan="12" style="text-align:center; color:#666;">보유 없음</td></tr>'}</tbody>
        </table>
      </div>

      <div class="card">
        <h2>📜 최근 매매 이력 (최근 50건) <span style="font-size:11px; color:#8b939e; font-weight:normal;">— 행 클릭 시 근거 펼침</span></h2>
        <table>
          <thead><tr>
            <th style="width:24px;"></th>
            <th>코드</th><th>종목</th><th>진입</th><th>청산</th>
            <th>매수가</th><th>매도가</th>
            <th>수량</th><th>매수액</th><th>매도액</th>
            <th>손익(원)</th><th>(%)</th>
          </tr></thead>
          <tbody>{closed_rows}</tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <h2>📉 보유 종목 변동 추이 (entry 이후 일별)</h2>
      <div class="chart-wrap"><canvas id="holdingsChart"></canvas></div>
    </div>

    </div><!-- /tab-perf -->

    <div id="tab-ops" class="tab-pane">
      {_render_ops_tab(logs, health, pending_items, pending_date)}
    </div>

  </div><!-- /container -->

  <script>
    function switchTab(name, btn) {{
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      if (btn) btn.classList.add('active');
      const pane = document.getElementById('tab-' + name);
      if (pane) pane.classList.add('active');
    }}

    function toggleReason(i) {{
      const row = document.getElementById('rs' + i);
      const tg = document.getElementById('tg' + i);
      if (!row) return;
      const open = row.style.display !== 'none';
      row.style.display = open ? 'none' : 'table-row';
      if (tg) tg.classList.toggle('open', !open);
    }}

    function toggleOpenReason(j) {{
      const row = document.getElementById('ors' + j);
      const tg = document.getElementById('otg' + j);
      if (!row) return;
      const open = row.style.display !== 'none';
      row.style.display = open ? 'none' : 'table-row';
      if (tg) tg.classList.toggle('open', !open);
    }}

    function setAiCutoff(cutoff, btn) {{
      document.querySelectorAll('.ai-btn').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      let filtered = ALL_TRADES;
      if (cutoff !== 'all') filtered = ALL_TRADES.filter(t => t.ai_score !== null && t.ai_score >= cutoff);
      let cap = INITIAL_CAP;
      let wins = 0;
      filtered.forEach(t => {{
        cap += PER_SLOT * t.net_pct / 100;
        if (t.net_pct > 0) wins++;
      }});
      const ret = (cap / INITIAL_CAP - 1) * 100;
      const avg = filtered.length ? filtered.reduce((s, t) => s + t.net_pct, 0) / filtered.length : 0;
      const wr = filtered.length ? wins / filtered.length * 100 : 0;
      document.getElementById('aiCount').textContent = filtered.length.toLocaleString() + '건';
      document.getElementById('aiFinal').textContent = Math.round(cap).toLocaleString() + '원';
      const retEl = document.getElementById('aiRet');
      retEl.textContent = (ret>=0?'+':'') + ret.toFixed(1) + '%';
      retEl.style.color = ret >= 0 ? '#4ade80' : '#f87171';
      const avgEl = document.getElementById('aiAvg');
      avgEl.textContent = (avg>=0?'+':'') + avg.toFixed(2) + '%';
      avgEl.style.color = avg >= 0 ? '#4ade80' : '#f87171';
      document.getElementById('aiWin').textContent = wr.toFixed(1) + '%';
    }}

    // 기간 필터 ─────────────────────────────────────────
    const ALL_TRADES = {period_trades};
    const INITIAL_CAP = {INITIAL_CAPITAL};
    const PER_SLOT = INITIAL_CAP / {MAX_CONCURRENT};

    function ymd(d) {{
      // 8자리 yyyymmdd 또는 Date → 비교 가능한 string (yyyymmdd)
      if (d instanceof Date) {{
        const y = d.getFullYear(), m = String(d.getMonth()+1).padStart(2,'0'),
              dd = String(d.getDate()).padStart(2,'0');
        return y + m + dd;
      }}
      return String(d).replaceAll('-','').slice(0,8);
    }}

    function calcPeriodKPI(startStr, endStr) {{
      const sorted = [...ALL_TRADES].sort((a,b) => a.exit_date.localeCompare(b.exit_date));
      // 시작 직전까지 누적 자본
      let capBefore = INITIAL_CAP;
      sorted.forEach(t => {{
        if (t.exit_date < startStr) capBefore += PER_SLOT * t.net_pct / 100;
      }});
      // 기간 내 매매
      const inPeriod = sorted.filter(t => t.exit_date >= startStr && t.exit_date <= endStr);
      let capAfter = capBefore;
      let wins = 0;
      inPeriod.forEach(t => {{
        capAfter += PER_SLOT * t.net_pct / 100;
        if (t.net_pct > 0) wins++;
      }});
      const ret = capBefore > 0 ? (capAfter/capBefore - 1) * 100 : 0;
      const winRate = inPeriod.length > 0 ? (wins / inPeriod.length * 100) : 0;
      return {{ count: inPeriod.length, capBefore, capAfter, ret, winRate, inPeriod }};
    }}

    function fmt(n) {{ return Math.round(n).toLocaleString(); }}

    function renderPeriodKPI(k) {{
      document.getElementById('kpiCount').textContent = k.count.toLocaleString() + '건';
      document.getElementById('kpiStart').textContent = fmt(k.capBefore) + '원';
      document.getElementById('kpiEnd').textContent = fmt(k.capAfter) + '원';
      const retEl = document.getElementById('kpiRet');
      retEl.textContent = (k.ret>=0?'+':'') + k.ret.toFixed(2) + '%';
      retEl.style.color = k.ret >= 0 ? '#4ade80' : '#f87171';
      document.getElementById('kpiWin').textContent = k.winRate.toFixed(1) + '%';
    }}

    function applyPeriod(startStr, endStr) {{
      const k = calcPeriodKPI(startStr, endStr);
      renderPeriodKPI(k);
      updateEquityChart(k.inPeriod, k.capBefore, startStr, endStr);
    }}

    function setPeriod(days, btn) {{
      document.querySelectorAll('.period-btn[data-days]').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      const sorted = [...ALL_TRADES].sort((a,b) => a.exit_date.localeCompare(b.exit_date));
      if (sorted.length === 0) {{ renderPeriodKPI({{count:0,capBefore:INITIAL_CAP,capAfter:INITIAL_CAP,ret:0,winRate:0,inPeriod:[]}}); return; }}
      const lastStr = sorted[sorted.length-1].exit_date;
      let startStr;
      if (days === 'all') {{
        startStr = sorted[0].exit_date;
      }} else {{
        const y = parseInt(lastStr.slice(0,4)), m = parseInt(lastStr.slice(4,6))-1, d = parseInt(lastStr.slice(6,8));
        const dt = new Date(y, m, d);
        dt.setDate(dt.getDate() - days);
        startStr = ymd(dt);
      }}
      document.getElementById('periodStart').value =
          startStr.slice(0,4)+'-'+startStr.slice(4,6)+'-'+startStr.slice(6,8);
      document.getElementById('periodEnd').value =
          lastStr.slice(0,4)+'-'+lastStr.slice(4,6)+'-'+lastStr.slice(6,8);
      applyPeriod(startStr, lastStr);
    }}

    function applyCustom() {{
      const s = document.getElementById('periodStart').value;
      const e = document.getElementById('periodEnd').value;
      if (!s || !e) {{ alert('시작일과 종료일 모두 선택'); return; }}
      document.querySelectorAll('.period-btn[data-days]').forEach(b => b.classList.remove('active'));
      applyPeriod(ymd(s), ymd(e));
    }}

    let equityChart = null;
    function updateEquityChart(inPeriod, capBefore, startStr, endStr) {{
      const labels = ['시작 ' + (startStr.slice(0,4)+'-'+startStr.slice(4,6)+'-'+startStr.slice(6,8))];
      const values = [capBefore];
      let cap = capBefore;
      inPeriod.forEach(t => {{
        cap += PER_SLOT * t.net_pct / 100;
        labels.push(t.exit_date.slice(0,4)+'-'+t.exit_date.slice(4,6)+'-'+t.exit_date.slice(6,8));
        values.push(Math.round(cap));
      }});
      if (!equityChart) return;
      equityChart.data.labels = labels;
      equityChart.data.datasets[0].data = values;
      equityChart.update();
    }}

    const eqCtx = document.getElementById('equityChart').getContext('2d');
    equityChart = new Chart(eqCtx, {{
      type: 'line',
      data: {{
        labels: {eq_labels},
        datasets: [{{
          label: '자본 (원)',
          data: {eq_values},
          borderColor: '#4a9eff',
          backgroundColor: 'rgba(74,158,255,0.1)',
          fill: true, tension: 0.1, pointRadius: 0,
        }}]
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        plugins: {{ legend: {{ labels: {{ color: '#e8eaed' }} }} }},
        scales: {{
          x: {{ ticks: {{ color: '#8b939e', maxTicksLimit: 20 }}, grid: {{ color: '#2a2f3a' }} }},
          y: {{ ticks: {{ color: '#8b939e', callback: v => (v/10000).toFixed(0)+'만' }},
                grid: {{ color: '#2a2f3a' }} }}
        }}
      }}
    }});

    const holdings = {holdings_data};
    const holdingsCtx = document.getElementById('holdingsChart').getContext('2d');
    const colors = ['#4ade80','#f87171','#fbbf24','#4a9eff','#a78bfa',
                    '#f472b6','#34d399','#fb923c','#60a5fa','#c084fc'];

    setPeriod('all', document.querySelector('.period-btn[data-days="all"]'));
    setAiCutoff('all', document.querySelector('.ai-btn[data-ai="all"]'));

    new Chart(holdingsCtx, {{
      type: 'line',
      data: {{
        datasets: holdings.slice(0,10).map((h, i) => ({{
          label: h.code + ' ' + h.name,
          data: h.series.map(s => ({{x: s.date, y: s.pct}})),
          borderColor: colors[i % colors.length],
          backgroundColor: 'transparent',
          tension: 0.1, pointRadius: 1, borderWidth: 1.5,
        }}))
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        plugins: {{ legend: {{ labels: {{ color: '#e8eaed', font: {{size:10}} }} }} }},
        scales: {{
          x: {{ type: 'category', ticks: {{ color: '#8b939e', maxTicksLimit: 30 }},
                grid: {{ color: '#2a2f3a' }} }},
          y: {{ ticks: {{ color: '#8b939e', callback: v => v.toFixed(0)+'%' }},
                grid: {{ color: '#2a2f3a' }} }}
        }}
      }}
    }});
  </script>
</body>
</html>"""
    return html


def _render_kiwoom_section(name_cache):
    """db/kiwoom/snapshot.json + orders_*.csv → 키움 모의계좌 섹션 HTML."""
    import json
    import glob as _g
    snap_path = "./db/kiwoom/snapshot.json"
    if not os.path.exists(snap_path):
        return ('<div style="background:#1c2333;border:1px solid #2e3a55;border-radius:10px;'
                'padding:14px 18px;margin:12px 0;color:#9fb0d0">'
                '🏦 <b>키움 모의계좌</b> — 아직 스냅샷 없음. '
                '<code>python kiwoom_trader.py status</code> 첫 실행 후 표시됩니다.</div>')
    with open(snap_path, encoding="utf-8") as f:
        snap = json.load(f)
    dep = int(snap.get("deposit", 0) or 0)
    poss = snap.get("positions", [])

    rows = ""
    for p in poss:
        nm = p.get("name") or name_cache.get(str(p.get("code", "")).zfill(6), "")
        rows += (f'<tr><td>{p.get("code","")}</td><td>{nm}</td>'
                 f'<td style="text-align:right">{int(p.get("qty",0)):,}주</td></tr>')
    if not rows:
        rows = '<tr><td colspan="3" style="color:#8b96ad">보유 없음</td></tr>'

    # 최근 주문 5건
    order_rows = ""
    ofiles = sorted(_g.glob("./db/kiwoom/orders_*.csv"))
    if ofiles:
        try:
            od = pd.read_csv(ofiles[-1], dtype=str).tail(5)
            for _, r in od.iterrows():
                ok = str(r.get("ok", "")).lower() in ("true", "1")
                badge = "✅" if ok else "❌"
                order_rows += (f'<tr><td>{r.get("time","")}</td>'
                               f'<td>{"매수" if r.get("side")=="buy" else "매도"}</td>'
                               f'<td>{r.get("code","")} {r.get("name","")}</td>'
                               f'<td style="text-align:right">{r.get("qty","")}주</td>'
                               f'<td>{badge}</td></tr>')
        except Exception:
            pass
    if not order_rows:
        order_rows = '<tr><td colspan="5" style="color:#8b96ad">최근 주문 없음</td></tr>'

    th = 'style="text-align:left;color:#8b96ad;font-weight:600;padding:4px 8px"'
    td_table = ('style="width:100%;border-collapse:collapse;font-size:13px" '
                'cellpadding="4"')
    return f"""
<div style="background:#151b2b;border:1px solid #2e3a55;border-radius:12px;padding:16px 20px;margin:14px 0">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <h2 style="margin:0;font-size:17px">🏦 키움 모의계좌 <span style="font-size:12px;color:#8b96ad">
      ({snap.get('env','mock')} · {snap.get('date','')} {snap.get('time','')} 기준 · 원본 모드: 09:01 시가 매수 / 15:21 종가 매도)</span></h2>
    <div style="font-size:15px"><b>예수금 {dep:,}원</b> · 보유 {len(poss)}/10</div>
  </div>
  <div style="display:flex;gap:24px;margin-top:10px;flex-wrap:wrap">
    <div style="flex:1;min-width:260px">
      <div style="color:#8b96ad;font-size:12px;margin-bottom:4px">보유 종목</div>
      <table {td_table}><tr><th {th}>코드</th><th {th}>종목명</th><th {th}>수량</th></tr>{rows}</table>
    </div>
    <div style="flex:1;min-width:300px">
      <div style="color:#8b96ad;font-size:12px;margin-bottom:4px">최근 주문 (당일)</div>
      <table {td_table}><tr><th {th}>시각</th><th {th}>구분</th><th {th}>종목</th><th {th}>수량</th><th {th}>결과</th></tr>{order_rows}</table>
    </div>
  </div>
</div>"""


def main():
    if not os.path.exists(SIGNALS_CSV):
        print(f"[error] {SIGNALS_CSV} 없음. seed_paper_signals.py 먼저 실행.")
        return

    print("=== Dashboard 생성 ===")
    signals = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
    signals["signal_date"] = signals["signal_date"].astype(str)
    signals["code"] = signals["code"].astype(str).str.zfill(6)

    df = load_macro_daily()
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str).str.zfill(6)
    code_dates = df.groupby("code")[["date"]].apply(lambda x: sorted(x["date"].tolist())).to_dict()
    cols = [c for c in ["open", "high", "low", "close", "trading_value"] if c in df.columns]
    price_map = df.set_index(["code", "date"])[cols].to_dict("index")

    name_cache = _load_name_cache()
    if name_cache:
        print(f"  [name_cache] {len(name_cache):,} 종목명 로드")

    ai_model, ai_label, ai_use_cols = _load_ai_model()
    ai_macro_map, ai_stock_map = _load_ai_features(ai_use_cols)
    if ai_model and ai_macro_map:
        print(f"  [AI] {ai_label} ({len(ai_use_cols)} feature)")

    # [2026-06-12 교정] CA필터 + MTM 평가 — 백테스트/페이퍼와 동일 기준의 KPI
    from strategies._swing_base import find_corporate_action_dates
    from capital_simulator import build_price_lookup
    ca_map = find_corporate_action_dates(df)

    trades = build_trades(signals, code_dates, price_map, name_cache=name_cache,
                           ai_model=ai_model, ai_macro_map=ai_macro_map,
                           ai_stock_map=ai_stock_map, ai_use_cols=ai_use_cols,
                           ca_map=ca_map)
    closed = [t for t in trades if t["status"] == "closed"]
    open_pos = [t for t in trades if t["status"] == "open"]

    sim_trades = [
        StrategyTrade(
            strategy="paper", code=t["code"], entry_date=t["entry_date"],
            entry_price=t["entry_price"], exit_date=t["exit_date"],
            exit_price=t["exit_price"], holding_days=HOLDING_DAYS,
            gross_pct=(t["exit_price"]/t["entry_price"]-1)*100,
            cost_pct=COST_PCT, net_pct=t["net_pct"], exit_reason="hold_exit",
            score=float((price_map.get((t["code"], t["signal_date"]), {})
                         or {}).get("trading_value", 0) or 0),
        ) for t in closed
    ]
    price_lookup, trading_dates = build_price_lookup(df)
    cap = simulate_capital(sim_trades, initial_capital=INITIAL_CAPITAL,
                            max_concurrent=MAX_CONCURRENT,
                            price_map=price_lookup,
                            trading_dates=trading_dates) or {}

    equity_pts = build_equity_curve(closed)
    holdings_hist = build_holding_history(open_pos, price_map, code_dates)

    logs = _load_recent_logs()
    health = _check_health(logs)
    pending_result = _load_pending_signals(signals, code_dates, price_map, name_cache or {})
    pending_items, pending_date = pending_result if pending_result else ([], None)

    html = render_html(cap, equity_pts, open_pos, closed, holdings_hist,
                       logs=logs, health=health,
                       pending_items=pending_items, pending_date=pending_date)

    # [2026-06-12] 키움 모의계좌 섹션을 최상단에 주입 (메인), 기존 paper 는 참고(X2)
    try:
        kiwoom_html = _render_kiwoom_section(name_cache or {})
        anchor = "<h1>📊 천억이의 대시보드</h1>"
        if anchor in html:
            html = html.replace(
                anchor,
                "<h1>📊 천억이의 대시보드 — 키움 모의계좌(메인) · Paper X2(참고)</h1>"
                + kiwoom_html, 1)
    except Exception as e:
        print(f"[warn] 키움 섹션 렌더 실패 (무시): {e}")

    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[saved] {OUT_HTML}")


if __name__ == "__main__":
    main()
