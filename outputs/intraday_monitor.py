"""
intraday_monitor.py — 장중 시황 데이터 수집기 (KIS API 버전)

15분마다 실행:
  1. KIS REST API 로 거래량순위 / 기관·외국인 가집계 / 체결강도 상위 20 수집
  2. 보유 포지션(paper_signals.csv) 현재가 + 예상 P&L 계산 (KIS 현재가 조회)
  3. db/kiwoom/intraday_cache.json 저장 → Flask /api/intraday 로 대시보드에 제공

KIS API 실패 시 pykrx fallback:
  - pykrx 는 거래량/OHLCV만 제공 (기관·외국인 수급은 15:30 이후 확정)
  - fallback 동작 중이면 cache["data_source"] = "pykrx_fallback"

Windows 작업 스케줄러 등록 예시:
  프로그램: python
  인수:     C:\\fin\\outputs\\intraday_monitor.py
  시작위치: C:\\fin\\outputs
  트리거:   매일 09:00~16:00 반복, 간격 15분

환경:
  .env 에 KIS_ENV / KIS_PROD_APP_KEY ... 설정 후 사용
  자세한 내용은 kis_client.py 상단 주석 참조
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

os.chdir(Path(__file__).parent)
sys.path.insert(0, os.getcwd())

BASE       = Path(".")
PAPER_CSV  = BASE / "paper_signals.csv"
CACHE_OUT  = BASE / "db" / "kiwoom" / "intraday_cache.json"
NAME_CACHE = BASE / "name_cache.csv"

COST_ROUND_TRIP = 0.245   # 왕복 비용 % (수수료+세금+슬리피지)


# ── 종목명 캐시 ────────────────────────────────────────────────────────────────
def load_name_map() -> dict:
    if not NAME_CACHE.exists():
        return {}
    try:
        df = pd.read_csv(NAME_CACHE, dtype={"code": str})
        df["code"] = df["code"].str.zfill(6)
        return dict(zip(df["code"], df["name"]))
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# ① KIS API 데이터 수집
# ══════════════════════════════════════════════════════════════════════════════

def fetch_kis() -> dict:
    """
    KIS REST API 로 4개 데이터셋 수집.

    반환:
      {
        "volume_top20":   [...],
        "inst_top20":     [...],
        "foreign_top20":  [...],
        "exec_top20":     [...],
        "price_map":      {code: {"close":..., "change_pct":...}},
        "ok":             True/False,
      }
    """
    empty = {
        "volume_top20": [], "inst_top20": [], "foreign_top20": [],
        "exec_top20": [], "price_map": {}, "ok": False,
    }

    try:
        from kis_client import KISClient
        kis = KISClient()
        if not kis.app_key:
            print("  [KIS] 앱키 없음 → pykrx fallback 으로 전환")
            return empty
    except ImportError:
        print("  [KIS] kis_client.py 없음 → pykrx fallback 으로 전환")
        return empty
    except Exception as e:
        print(f"  [KIS] 초기화 오류: {e}")
        return empty

    result = dict(empty)
    result["ok"] = True  # 낙관적으로 시작, 개별 실패 시 빈 리스트

    # ── 거래량순위 ──────────────────────────────────────────────────────────
    try:
        result["volume_top20"] = kis.volume_rank(20)
        print(f"  [KIS] 거래량순위 {len(result['volume_top20'])}건")
    except Exception as e:
        print(f"  [KIS] 거래량순위 실패: {e}")
        result["ok"] = False

    # ── 기관/외국인 가집계 ────────────────────────────────────────────────
    try:
        inst, frn = kis.inst_foreign_total(20)
        result["inst_top20"]    = inst
        result["foreign_top20"] = frn
        print(f"  [KIS] 기관 {len(inst)}건 / 외국인 {len(frn)}건")
    except Exception as e:
        print(f"  [KIS] 기관/외국인 가집계 실패: {e}")

    # ── 체결강도 상위 (KIS ccld_rate 기반, 없으면 빈 리스트 — pykrx에서 보완) ──
    try:
        result["exec_top20"] = kis.exec_strength_rank(20)
        print(f"  [KIS] 체결강도 {len(result['exec_top20'])}건"
              + (" (pykrx에서 보완 예정)" if not result["exec_top20"] else ""))
    except Exception as e:
        print(f"  [KIS] 체결강도 실패: {e}")

    # ── price_map 구성 (보유종목 현재가용) ──────────────────────────────
    # 거래량순위 응답에 이미 현재가가 있으므로 우선 사용
    pm: dict = {}
    for r in result["volume_top20"]:
        code = r.get("code", "")
        if code:
            pm[code] = {"close": r["close"], "change_pct": r["change_pct"]}
    result["price_map"] = pm

    return result


# ══════════════════════════════════════════════════════════════════════════════
# ② pykrx fallback
# ══════════════════════════════════════════════════════════════════════════════

def fetch_pykrx_fallback() -> dict:
    """KIS API 실패 시 pykrx 로 OHLCV + 거래량 기반 데이터만 수집."""
    empty = {
        "volume_top20": [], "inst_top20": [], "foreign_top20": [],
        "exec_top20": [], "price_map": {}, "ok": False,
    }

    try:
        from pykrx import stock as pystock
    except ImportError:
        print("  [pykrx] 미설치 → pip install pykrx")
        return empty

    # OHLCV 수집 (오늘 없으면 최근 3영업일 소급)
    df_price = None
    for delta in range(0, 4):
        target = (datetime.today() - timedelta(days=delta)).strftime("%Y%m%d")
        try:
            df = pystock.get_market_ohlcv(target, market="ALL")
            if df is not None and len(df) > 100:
                df_price = df
                if delta > 0:
                    print(f"  [pykrx] 오늘 데이터 없음 → {target} 사용")
                break
        except Exception as e:
            print(f"  [pykrx] OHLCV {target} 오류: {e}")

    if df_price is None:
        print("  [pykrx] OHLCV 수집 실패")
        return empty

    # 컬럼명 표준화
    col_map = {
        "시가": "open", "고가": "high", "저가": "low", "종가": "close",
        "거래량": "volume", "거래대금": "trading_value", "등락률": "change_pct",
    }
    df_price = df_price.rename(columns=col_map)
    df_price.index.name = "ticker"
    df_price = df_price.reset_index()
    df_price["ticker"] = df_price["ticker"].astype(str).str.zfill(6)

    # pykrx 수급 (가집계 아님 — 당일 확정치는 15:30 이후)
    today_str = datetime.today().strftime("%Y%m%d")
    df_net = None
    try:
        net_raw = pystock.get_market_net_purchases_of_investors(
            today_str, today_str, market="ALL"
        )
        if net_raw is not None and len(net_raw) > 50:
            net_col_map = {
                "기관합계": "inst_net", "기관계": "inst_net",
                "외국인합계": "foreign_net", "외국인": "foreign_net",
            }
            df_net = net_raw.rename(columns=net_col_map).reset_index()
            df_net.index.name = "ticker"
            df_net = df_net.reset_index(drop=True)
            df_net["ticker"] = df_net["ticker"].astype(str).str.zfill(6)
    except Exception as e:
        print(f"  [pykrx] 수급 오류 (무시): {e}")

    name_map = load_name_map()

    # price_map
    pm: dict = {}
    for _, r in df_price.iterrows():
        code = str(r["ticker"]).zfill(6)
        pm[code] = {
            "close":      int(r.get("close", 0) or 0),
            "change_pct": round(float(r.get("change_pct", 0) or 0), 2),
        }

    def _top20_volume():
        sub = df_price[df_price.get("trading_value", pd.Series(dtype=float)) > 0]
        if "trading_value" not in df_price.columns:
            return []
        sub = df_price[df_price["trading_value"] > 0].nlargest(20, "trading_value")
        res = []
        for i, (_, r) in enumerate(sub.iterrows(), 1):
            code = str(r["ticker"]).zfill(6)
            res.append({
                "rank": i, "code": code, "name": name_map.get(code, ""),
                "close":         int(r.get("close", 0) or 0),
                "change_pct":    round(float(r.get("change_pct", 0) or 0), 2),
                "volume":        int(r.get("volume", 0) or 0),
                "trading_value": int(r.get("trading_value", 0) or 0),
            })
        return res

    def _top20_inst():
        if df_net is None or "inst_net" not in df_net.columns:
            return []
        sub = df_net[df_net["inst_net"] > 0].nlargest(20, "inst_net")
        res = []
        for i, (_, r) in enumerate(sub.iterrows(), 1):
            code = str(r["ticker"]).zfill(6)
            info = pm.get(code, {})
            res.append({
                "rank": i, "code": code, "name": name_map.get(code, ""),
                "close":      info.get("close", 0),
                "change_pct": info.get("change_pct", 0.0),
                "inst_net":   int(r["inst_net"] or 0),
            })
        return res

    def _top20_foreign():
        if df_net is None or "foreign_net" not in df_net.columns:
            return []
        sub = df_net[df_net["foreign_net"] > 0].nlargest(20, "foreign_net")
        res = []
        for i, (_, r) in enumerate(sub.iterrows(), 1):
            code = str(r["ticker"]).zfill(6)
            info = pm.get(code, {})
            res.append({
                "rank": i, "code": code, "name": name_map.get(code, ""),
                "close":       info.get("close", 0),
                "change_pct":  info.get("change_pct", 0.0),
                "foreign_net": int(r["foreign_net"] or 0),
            })
        return res

    def _top20_exec():
        need = {"close", "high", "low", "trading_value"}
        if not need.issubset(df_price.columns):
            return []
        df = df_price.copy()
        df = df[df["trading_value"] >= 500_000_000]
        hl = (df["high"] - df["low"]).replace(0, 1)
        df["exec_str"] = ((df["close"] - df["low"]) / hl * 100).round(1)
        df = df[df["exec_str"] > 0]
        sub = df.nlargest(20, "exec_str")
        res = []
        for i, (_, r) in enumerate(sub.iterrows(), 1):
            code = str(r["ticker"]).zfill(6)
            res.append({
                "rank": i, "code": code, "name": name_map.get(code, ""),
                "close":         int(r.get("close", 0) or 0),
                "change_pct":    round(float(r.get("change_pct", 0) or 0), 2),
                "exec_strength": round(float(r["exec_str"]), 1),
            })
        return res

    print(f"  [pykrx] OHLCV {len(df_price)}종목 로드 완료")
    return {
        "volume_top20":  _top20_volume(),
        "inst_top20":    _top20_inst(),
        "foreign_top20": _top20_foreign(),
        "exec_top20":    _top20_exec(),
        "price_map":     pm,
        "ok":            True,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ③ 보유 포지션 P&L (현재가 KIS 직접 조회)
# ══════════════════════════════════════════════════════════════════════════════

def get_positions(price_map: dict, name_map: dict, kis_client=None) -> list[dict]:
    """
    paper_signals.csv 활성 포지션 목록 + 예상 P&L.

    price_map 에 없는 종목은 kis_client 로 개별 조회.
    kis_client 도 없으면 curr_price = None (대시보드에서 '-' 표시).
    """
    if not PAPER_CSV.exists():
        return []

    try:
        df = pd.read_csv(PAPER_CSV, dtype={"code": str})
        df["code"] = df["code"].str.zfill(6)
        today_str = datetime.today().strftime("%Y%m%d")   # target_exit_date 는 YYYYMMDD(대시없음) — 형식 일치(2026-06-30 수정)
        if "target_exit_date" in df.columns:
            active = df[df["target_exit_date"].astype(str) >= today_str]
        else:
            active = df
    except Exception as e:
        print(f"  [positions] CSV 로드 오류: {e}")
        return []

    rows = []
    for _, r in active.iterrows():
        code = str(r["code"]).zfill(6)
        ep   = float(r.get("entry_price_close", 0) or 0)

        # price_map 우선, 없으면 KIS 단건 조회
        info = price_map.get(code)
        if info is None and kis_client is not None:
            try:
                info = kis_client.get_price(code)
            except Exception:
                info = None

        curr_price  = info["close"]      if info else None
        change_pct  = info["change_pct"] if info else None
        est_pct     = None
        if ep > 0 and curr_price:
            est_pct = round((curr_price / ep - 1) * 100 - COST_ROUND_TRIP, 2)

        rows.append({
            "code":             code,
            "name":             name_map.get(code, str(r.get("name", ""))),
            "strategy":         str(r.get("strategy", "")),
            "signal_date":      str(r.get("signal_date", "")),
            "target_exit_date": str(r.get("target_exit_date", "")),
            "entry_price":      ep,
            "curr_price":       curr_price,
            "est_pct":          est_pct,
            "change_today":     change_pct,
        })
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════

def run():
    now = datetime.now()
    print(f"[intraday_monitor] {now.strftime('%Y-%m-%d %H:%M:%S')}")

    name_map = load_name_map()

    # 1차: KIS API 시도
    data = fetch_kis()
    data_source = "kis"

    # KIS 실패 또는 appkey 미설정 → pykrx fallback
    if not data["ok"] or not data["volume_top20"]:
        print("  → pykrx fallback 으로 전환")
        data        = fetch_pykrx_fallback()
        data_source = "pykrx_fallback"

    # 체결강도가 KIS에서 비어있으면 pykrx OHLCV로 계산
    if data_source == "kis" and not data["exec_top20"]:
        pyx = fetch_pykrx_fallback()
        if pyx["exec_top20"]:
            data["exec_top20"] = pyx["exec_top20"]
            print(f"  [pykrx] 체결강도 보완 {len(data['exec_top20'])}건")

    # 보유 포지션 P&L
    kis_client = None
    if data_source == "kis":
        try:
            from kis_client import KISClient
            kis_client = KISClient()
        except Exception:
            pass

    positions = get_positions(data["price_map"], name_map, kis_client)

    # 이름 보완 (volume/inst/foreign/exec 목록 name 이 비어있을 때)
    for lst in [data["volume_top20"], data["inst_top20"],
                data["foreign_top20"], data["exec_top20"]]:
        for item in lst:
            if not item.get("name") and item.get("code"):
                item["name"] = name_map.get(item["code"], "")

    print(f"  거래량 {len(data['volume_top20'])} / 기관 {len(data['inst_top20'])} / "
          f"외국인 {len(data['foreign_top20'])} / 체결강도 {len(data['exec_top20'])} / "
          f"포지션 {len(positions)} / 소스: {data_source}")

    cache = {
        "updated_at":     now.strftime("%Y-%m-%d %H:%M:%S"),
        "market_date":    now.strftime("%Y%m%d"),
        "data_time":      now.strftime("%H:%M"),
        "is_market_hours": 9 <= now.hour < 16,
        "data_source":    data_source,
        "volume_top20":   data["volume_top20"],
        "inst_top20":     data["inst_top20"],
        "foreign_top20":  data["foreign_top20"],
        "exec_top20":     data["exec_top20"],
        "positions":      positions,
        "position_count": len(positions),
    }

    CACHE_OUT.parent.mkdir(parents=True, exist_ok=True)
    CACHE_OUT.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  ✓ 저장: {CACHE_OUT}")


if __name__ == "__main__":
    run()
