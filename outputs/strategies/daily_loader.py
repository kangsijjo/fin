"""
일봉 통합 데이터 로더.

macro_data/daily/YYYYMMDD.csv 파일들을 모두 합쳐 단일 DataFrame 반환.
컬럼 표준화: 한글 컬럼명 → 영문.
"""

import glob
import os
import pandas as pd

DATA_DIR = "./macro_data/daily"


CACHE_PATH = f"{DATA_DIR}/_daily_cache.pkl"


def _cache_signature(files):
    """파일 개수 + 마지막 파일명 + 총 크기 — 추가/변경 시 캐시 무효화."""
    total = 0
    for f in files:
        try:
            total += os.path.getsize(f)
        except OSError:
            pass
    return (len(files), os.path.basename(files[-1]) if files else "", total)


def load_macro_daily(start_date=None, end_date=None) -> pd.DataFrame:
    """
    Returns: DataFrame[code, date, open, high, low, close, volume,
                      trading_value, change_pct, market_cap,
                      foreign_net, inst_net]
    date 는 YYYYMMDD 문자열, 종목/날짜 정렬.

    [캐시] 데이터가 8년(1,900+ 파일)으로 커져 매 호출 풀스캔이 수 분 걸림.
    필터 없는 호출은 pickle 캐시 사용 — 파일 추가/변경 시 자동 재생성.
    """
    files = sorted(glob.glob(f"{DATA_DIR}/*.csv"))
    if not files:
        raise FileNotFoundError(f"{DATA_DIR} 에 일봉 데이터 없음. pykrx_collector.py 먼저 실행.")

    use_cache = (start_date is None and end_date is None)
    if use_cache and os.path.exists(CACHE_PATH):
        try:
            import pickle
            with open(CACHE_PATH, "rb") as fh:
                cached = pickle.load(fh)
            if cached.get("sig") == _cache_signature(files):
                return cached["df"]
            print("  [loader] 캐시 무효 (데이터 변경) — 재생성")
        except Exception:
            pass

    dfs = []
    for f in files:
        date_from_name = os.path.basename(f).rsplit(".", 1)[0]
        if start_date and date_from_name < str(start_date):
            continue
        if end_date and date_from_name > str(end_date):
            continue
        df = pd.read_csv(f, encoding="utf-8-sig", dtype={"code": str})
        if "date" not in df.columns:
            df["date"] = date_from_name
        dfs.append(df)

    full = pd.concat(dfs, ignore_index=True)
    full["date"] = full["date"].astype(str)
    full["code"] = full["code"].astype(str).str.zfill(6)

    # 한글 컬럼 → 영문 표준화
    rename = {
        "거래대금": "trading_value",
        "등락률": "change_pct",
        "시가총액": "market_cap",
    }
    full = full.rename(columns=rename)
    # 중복 컬럼 제거 — 한글+영문이 동시에 존재하는 CSV 혼재 시 발생 (첫 번째 유지)
    full = full.loc[:, ~full.columns.duplicated()]
    # 누락 컬럼 0 채움 (옛 데이터 호환)
    for c in ["trading_value", "change_pct", "market_cap",
              "foreign_net", "inst_net"]:
        if c not in full.columns:
            full[c] = 0.0
    # 종목명 컬럼 — pykrx_collector 가 저장 안 한 경우 빈 문자열
    if "name" not in full.columns:
        full["name"] = ""

    # [방어] close<=0 행 제거 — KRX 가 휴장일/거래정지에 0원 행을 주는 경우가 있어
    # 가짜 거래일이 신호·청산일 계산을 오염시킴 (휴장일 파일 자체는 수집기가 차단)
    n0 = len(full)
    full = full[full["close"] > 0]
    if n0 - len(full) > 0:
        print(f"  [loader] close<=0 행 {n0 - len(full):,}개 제거")

    full = full.sort_values(["code", "date"]).reset_index(drop=True)

    if use_cache:
        try:
            import pickle
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "wb") as fh:
                pickle.dump({"sig": _cache_signature(files), "df": full}, fh,
                            protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, CACHE_PATH)
            print(f"  [loader] 캐시 저장 ({len(full):,}행) — 다음 로드부터 수 초")
        except Exception as e:
            print(f"  [loader] 캐시 저장 실패 (무시): {e}")
    return full


def filter_universe(df, name_cache_path="./name_cache.csv"):
    """ETF/우선주/스팩 제외 — live_signal 과 동일 유니버스로 백테스트.

    주의: name_cache 는 '현재 상장' 종목 기준이라, 이름을 모르는(상장폐지 등)
    종목은 제외하지 않는다 — 모르는 종목까지 빼면 생존편향이 생기기 때문.
    이름이 확인되는 ETF/우선주/스팩만 제거한다.
    """
    if not os.path.exists(name_cache_path):
        return df
    try:
        nc = pd.read_csv(name_cache_path, dtype={"code": str})
        nc["code"] = nc["code"].astype(str).str.zfill(6)
        name_map = dict(zip(nc["code"], nc["name"].astype(str)))
    except Exception:
        return df

    etf_prefixes = ("KODEX", "TIGER", "KBSTAR", "KOSEF", "ARIRANG", "HANARO",
                    "SOL ", "ACE ", "PLUS ", "RISE ", "1Q ", "WON ")

    def _bad(code):
        nm = name_map.get(code, "")
        if not nm:
            return False  # 이름 모름 → 유지 (생존편향 방지)
        if nm.startswith(etf_prefixes):
            return True
        if nm.endswith(("우", "우B")) or "우선주" in nm or "스팩" in nm:
            return True
        return False

    bad_codes = {c for c in df["code"].unique() if _bad(str(c))}
    if bad_codes:
        n0 = len(df)
        df = df[~df["code"].isin(bad_codes)].copy()
        print(f"  [universe] ETF/우선주/스팩 {len(bad_codes)}종목 "
              f"({n0 - len(df):,}행) 제외")
    return df


def default_costs():
    """KOSDAQ 기준 비용 가정 (단타와 동일).

    참고: 증권거래세는 2025년부터 코스닥 0.15% 가 현행이나,
    여기서는 0.20% 를 유지해 보수적으로 잡음 (슬리피지 여유분 포함).
    """
    return {
        "fee_pct":   0.015 * 2,   # 매수+매도
        "tax_pct":   0.20,        # KOSDAQ 거래세 (보수적 — 현행 0.15%)
        "slip_pct":  0.05 * 2,    # 슬리피지 매수+매도
        "total_pct": 0.015*2 + 0.20 + 0.05*2,   # = 0.43%
    }
