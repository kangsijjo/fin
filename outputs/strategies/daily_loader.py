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
SIG_PATH   = f"{DATA_DIR}/_daily_cache.sig.json"   # 사이드카: 큰 피클을 읽기 전 유효성 판단용


# 로더 로직 버전 — 올리면 기존 캐시(_daily_cache.pkl)가 자동 무효화돼 재생성된다.
# v2: 중복 컬럼 coalesce 수정 (2026-06-19) — 이전 캐시는 trading_value/change_pct 가 깨져 있어 무효.
_LOADER_VERSION = 2


def _cache_signature(files):
    """로더버전 + 파일 개수 + 마지막 파일명 + 총 크기 — 변경 시 캐시 무효화."""
    total = 0
    for f in files:
        try:
            total += os.path.getsize(f)
        except OSError:
            pass
    return (_LOADER_VERSION, len(files),
            os.path.basename(files[-1]) if files else "", total)


def _read_sig():
    """사이드카 시그니처 로드 (없거나 깨지면 None)."""
    try:
        import json
        with open(SIG_PATH, "r", encoding="utf-8") as fh:
            return tuple(json.load(fh))
    except Exception:
        return None


def _write_sig(sig):
    """사이드카 시그니처 기록 (실패 무시)."""
    try:
        import json
        with open(SIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(list(sig), fh)
    except Exception:
        pass


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
        # 사이드카 시그니처로 먼저 판단 — 무효면 324MB 피클을 읽지 않고 바로 재생성.
        if _read_sig() == _cache_signature(files):
            try:
                import pickle
                with open(CACHE_PATH, "rb") as fh:
                    return pickle.load(fh)["df"]
            except Exception:
                pass
        else:
            print("  [loader] 캐시 무효 (데이터 변경) — 재생성")

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
    # 중복 컬럼 병합 — 옛 한글 파일(거래대금/등락률)과 최근 영문 파일(trading_value/change_pct)이
    # 섞이면 rename 후 같은 이름 컬럼이 둘 생긴다. 단순 중복제거(첫 컬럼 유지)는 한쪽만 채워진
    # 값을 버려 최근 trading_value/change_pct 가 NaN 이 되고 → 신호 필터(tv>=10억, 시장게이트)가
    # 전멸해 모든 전략이 0건이 된다 (2026-06-19 발견·수정).
    # → 같은 이름 컬럼들을 행별 coalesce(좌→우 첫 non-NaN)로 병합한다.
    if full.columns.duplicated().any():
        for _dup in full.columns[full.columns.duplicated()].unique():
            _cols = full.loc[:, full.columns == _dup]
            _merged = _cols.bfill(axis=1).iloc[:, 0]
            full = full.loc[:, full.columns != _dup]
            full[_dup] = _merged
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
            _write_sig(_cache_signature(files))   # 사이드카도 함께 기록
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
