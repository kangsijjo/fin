"""
데이터 수집 메인 스크립트 (30분 단위 누적 스냅숏 버전)

변경 사항:
  - 'ranking' 및 'today' 명령어 실행 시, 기존 파일을 덮어쓰지 않고
    30분 단위(예: 0900, 0930, 1000...)로 실시간 상위 30개 종목을 조회하여 하나의 파일에 '누적(Append)'합니다.
  - 백테스트 시점(10:00 등)까지 누적된 모든 종목 풀을 대상으로 분봉을 수집하므로,
    장중 돌발 주도주까지 놓치지 않고 생존 편향(Look-ahead Bias)을 완벽히 제거합니다.
  - load_ranking() 은 종목별 '첫 등장 시각(first_seen_at)'을 함께 반환하여,
    백테스트에서 "이 종목은 11:00 부터만 진입 가능" 같은 룰을 적용할 수 있습니다.

사용법:
  # 1) 현재 시점의 거래대금 상위 30개를 당일 ranking 파일에 누적 저장 (배치로 30분마다 호출 권장)
  python data_collector.py ranking

  # 2) 특정 날짜의 분봉 데이터 수집 (저장된 그날의 30분 단위 누적 ranking 사용)
  python data_collector.py minutes 20260515

  # 3) 당일 실시간 ranking 누적 + 장 마감 후 분봉 수집 프로세스
  python data_collector.py today
"""

import sys
import time
import csv
import os
from datetime import datetime

import config
from kis_api import (
    get_volume_ranking,
    get_full_day_minutes,
    get_foreign_institution_trading,
    get_stock_investor,
    get_daily_chart,
    get_index_current,
    get_index_minute_chart,
)


def _month_dir(base_dir, date_str):
    """date_str(YYYYMMDD) → base_dir/YYYY-MM/ 경로 생성 후 반환."""
    yyyy_mm = f"{date_str[:4]}-{date_str[4:6]}"
    path = f"{base_dir}/{yyyy_mm}"
    os.makedirs(path, exist_ok=True)
    return path


def _is_excluded(name, code):
    """우선주/ETF/관리종목 등 필터링 (간단한 휴리스틱)"""
    if config.EXCLUDE_PREFERRED:
        if name and (name.endswith("우") or name.endswith("우B") or "우선주" in name):
            return True
    if config.EXCLUDE_ETF:
        etf_prefixes = ("KODEX", "TIGER", "KBSTAR", "KOSEF", "ARIRANG", "HANARO",
                        "SOL ", "ACE ", "PLUS ", "RISE ", "1Q ", "WON ")
        if name and any(name.startswith(p) for p in etf_prefixes):
            return True
        if code and len(code) == 6 and code.startswith("5"):
            return True
    return False


def save_ranking(date_str=None, market="ALL"):
    """거래대금 raw 상위 종목을 조회 후, 시간대별 스냅숏으로 CSV에 누적(Append) 저장.

    KIS volume-rank API 가 한 호출당 max 30개 → KOSPI/KOSDAQ 별도 호출 후 merge.
    ETF/우선주는 여기서 거르지 않고 raw 로 저장 (백테스트에서 ENABLE_ETF_FILTER 로 제외).
    market 인자는 후방 호환을 위해 유지하지만 내부적으로는 항상 두 시장 합쳐서 처리.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    now = datetime.now()
    current_time = now.strftime("%H:%M:%S")
    # 파일 내 정렬 및 구분을 위한 시간 키 (예: 09:32 -> 0930, 10:05 -> 1000)
    time_key = f"{now.hour:02d}{(now.minute // 30) * 30:02d}"

    print(f"[ranking] {date_str} {current_time} ({time_key}) KOSPI+KOSDAQ 거래대금 상위 조회 중...")

    # KOSPI / KOSDAQ 각각 호출 후 머지 (한 호출 max 30개 제한 우회)
    merged = []
    for mkt in ("KOSPI", "KOSDAQ"):
        try:
            rows = get_volume_ranking(market=mkt, by="trading_value")
            merged.extend(rows)
        except Exception as e:
            print(f"  [warn] {mkt} 조회 실패: {e}")

    # 중복 종목 제거 (시장 분리 호출이라 보통 없지만 방어)
    seen = set()
    deduped = []
    for r in merged:
        c = r.get("code")
        if not c or c in seen:
            continue
        seen.add(c)
        deduped.append(r)

    # 거래대금 내림차순 정렬 후 raw top N 슬라이싱 (ETF 포함)
    # save_ranking 함수 내부 슬라이싱 부분 수정

    # 거래대금 내림차순 정렬
    deduped.sort(key=lambda r: r.get("trading_value", 0), reverse=True)
    
    # [수정] 자르기 전에 미리 필터링해서 순수 일반 종목으로만 TOP_N을 꽉 채움
    filtered = []
    for r in deduped:
        if _is_excluded(r.get("name", ""), r.get("code", "")):
            continue
        filtered.append(r)
        if len(filtered) >= config.TOP_N_STOCKS:
            break

    # snapshot_time + 머지 후 새 rank 부여 (1~N)
    for i, r in enumerate(filtered, 1):
        r["snapshot_time"] = time_key
        r["rank"] = i

    out_path = f"{config.RANKING_DIR}/{date_str}.csv"
    file_exists = os.path.exists(out_path)

    with open(out_path, "a" if file_exists else "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["snapshot_time", "rank", "code", "name", "current_price",
                        "change_pct", "volume", "trading_value"],
        )
        if not file_exists:
            writer.writeheader()
        for r in filtered:
            writer.writerow({
                "snapshot_time": r["snapshot_time"],
                "rank": r.get("rank", ""),
                "code": r["code"],
                "name": r["name"],
                "current_price": r.get("current_price", 0),
                "change_pct": r.get("change_pct", 0.0),
                "volume": r.get("volume", 0),
                "trading_value": r.get("trading_value", 0)
            })

    print(f"[ranking] {len(filtered)}개 누적 완료 (시간대: {time_key}): {out_path}")
    return filtered


# ============================================================
# 시장지수 수집 (KOSPI / KOSDAQ)
# ============================================================

def save_index_snapshot(date_str=None):
    """KOSPI/KOSDAQ 지수 현재가를 30분 단위 누적 스냅숏으로 저장.

    경로: db/index/YYYY-MM/YYYYMMDD.csv
    스키마: snapshot_time, market, value, change, change_pct, volume, trading_value
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    now = datetime.now()
    time_key = f"{now.hour:02d}{(now.minute // 30) * 30:02d}"

    rows = []
    for mkt in ("KOSPI", "KOSDAQ"):
        try:
            r = get_index_current(market=mkt)
            r["snapshot_time"] = time_key
            rows.append(r)
        except Exception as e:
            print(f"  [warn] 지수 {mkt} 조회 실패: {e}")

    if not rows:
        return

    out_dir = _month_dir(config.DB_INDEX_DIR, date_str)
    out_path = f"{out_dir}/{date_str}.csv"
    file_exists = os.path.exists(out_path)
    fieldnames = [
        "snapshot_time", "market", "value", "change", "change_pct",
        "volume", "trading_value",
        "open", "high", "low",
        "up_count", "down_count", "stnr_count", "upper_limit", "lower_limit",
    ]
    with open(out_path, "a" if file_exists else "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})
    print(f"[index] {len(rows)}개 누적 (시간대 {time_key}): {out_path}")


def save_index_minutes(date_str=None):
    """KOSPI/KOSDAQ 지수의 당일 분봉 전체 저장 (EOD 1회 호출).

    경로: db/index/YYYY-MM/YYYYMMDD_minute.csv
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    out_dir = _month_dir(config.DB_INDEX_DIR, date_str)
    out_path = f"{out_dir}/{date_str}_minute.csv"
    if os.path.exists(out_path):
        print(f"[index_min] 이미 있음, 스킵: {out_path}")
        return

    all_bars = []
    for mkt in ("KOSPI", "KOSDAQ"):
        # 30분 단위로 역순 호출 (종목 분봉과 동일 패턴)
        times = []
        h, m = 15, 30
        while h > 9 or (h == 9 and m >= 30):
            times.append(f"{h:02d}{m:02d}00")
            if m == 30:
                m = 0
            else:
                m = 30
                h -= 1
        times.append("090000")

        seen = set()
        for et in times:
            try:
                bars = get_index_minute_chart(market=mkt, target_time=et)
                for b in bars:
                    if b["date"] != date_str:
                        continue
                    key = (mkt, b["date"], b["time"])
                    if key in seen:
                        continue
                    seen.add(key)
                    b["market"] = mkt
                    all_bars.append(b)
                time.sleep(0.4)
            except Exception as e:
                print(f"  [warn] {mkt} 분봉 {et}: {e}")

    if not all_bars:
        print(f"[index_min] {date_str} 데이터 없음")
        return

    all_bars.sort(key=lambda x: (x["market"], x["date"], x["time"]))
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["market", "date", "time", "open", "high", "low", "close",
                        "volume", "trading_value"],
        )
        writer.writeheader()
        for b in all_bars:
            writer.writerow(b)
    print(f"[index_min] {len(all_bars)}행 저장: {out_path}")


def load_ranking(date_str):
    """저장된 ranking CSV에서 중복을 제거한 종목 목록 반환

    Returns:
        list[tuple[str, str, str]]: (code, name, first_seen_at) 튜플 리스트
            - first_seen_at: 그 종목이 풀에 처음 등장한 시간 키 (예: "0930")
              백테스트에서 "이 종목은 이 시각 이후부터 진입 가능" 룰에 사용
    """
    path = f"{config.RANKING_DIR}/{date_str}.csv"
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} 없음. 먼저 `python data_collector.py ranking`로 수집하세요."
        )

    seen = {}  # code -> (name, first_seen_at)
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row["code"]
            if code not in seen:
                seen[code] = (row["name"], row.get("snapshot_time", ""))
    return [(code, name, first_seen) for code, (name, first_seen) in seen.items()]


def save_minutes(date_str, codes=None):
    """특정 날짜의 분봉 데이터 수집 후 CSV 저장 (종목별 파일)

    Args:
        codes: (code, name, first_seen_at) 튜플 리스트.
               None 이면 load_ranking() 으로 자동 로드.
               하위 호환: (code, name) 2-튜플도 허용.
    """
    if codes is None:
        codes = load_ranking(date_str)

    # 신규 저장 경로: db/minute/YYYY-MM/YYYYMMDD/CODE.csv
    minute_day_dir = f"{_month_dir(config.DB_MINUTE_DIR, date_str)}/{date_str}"
    os.makedirs(minute_day_dir, exist_ok=True)

    total = len(codes)
    for i, item in enumerate(codes, 1):
        # 2-튜플 / 3-튜플 모두 허용
        if len(item) == 3:
            code, name, _first_seen = item
        else:
            code, name = item

        out_path = f"{minute_day_dir}/{code}.csv"
        # 백테스트 호환 fallback: 옛 경로에도 동일 파일 확인
        legacy_path = f"{config.MINUTE_DIR}/{code}_{date_str}.csv"
        if os.path.exists(out_path) or os.path.exists(legacy_path):
            print(f"  [{i}/{total}] {code} {name} - 이미 있음, 스킵")
            continue

        print(f"  [{i}/{total}] {code} {name} 분봉 수집 중...")
        try:
            bars = get_full_day_minutes(code, date_str)
        except Exception as e:
            print(f"    [error] {code}: {e}")
            continue

        if not bars:
            print(f"    [warn] {code} {date_str} 데이터 없음 (휴장/신규상장 가능)")
            continue

        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["date", "time", "open", "high", "low", "close", "volume"],
            )
            writer.writeheader()
            writer.writerows(bars)

        print(f"    저장: {out_path} ({len(bars)}개 분봉)")
        time.sleep(0.3)

    print(f"[minutes] {date_str} 분봉 수집 완료")


def load_top_codes_by_trading_value(date_str, top_n):
    """그날 누적 ranking CSV 에서 종목별 max trading_value 기준 상위 N 종목 반환.

    Returns: list of dict
      {code, name, trading_value, current_price, change_pct, snapshot_time}
      - trading_value: 그날 본 최대 거래대금
      - current_price/change_pct: 그 최대치 찍힌 스냅숏 시점의 값 (수집시점 가격/등락률)
      - snapshot_time: 그 시점 (예: "1100")
    파일 없으면 빈 리스트.
    """
    path = f"{config.RANKING_DIR}/{date_str}.csv"
    if not os.path.exists(path):
        return []

    def _to_int(v, default=0):
        try:
            return int(v or 0)
        except (ValueError, TypeError):
            return default

    def _to_float(v, default=0.0):
        try:
            return float(v or 0)
        except (ValueError, TypeError):
            return default

    by_code = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row.get("code", "")
            if not code:
                continue
            tv = _to_int(row.get("trading_value"))
            if code not in by_code or tv > by_code[code]["trading_value"]:
                by_code[code] = {
                    "code": code,
                    "name": row.get("name", ""),
                    "trading_value": tv,
                    "current_price": _to_int(row.get("current_price")),
                    "change_pct":    _to_float(row.get("change_pct")),
                    "snapshot_time": row.get("snapshot_time", ""),
                }

    sorted_codes = sorted(by_code.values(), key=lambda x: -x["trading_value"])
    return sorted_codes[:top_n]


INVESTOR_FIELDS = [
    "date", "code", "name",
    "current_price", "change_pct", "trading_value", "snapshot_time",
    "foreign_net_qty", "foreign_net_value",
    "institution_net_qty", "institution_net_value",
    "personal_net_qty", "personal_net_value",
]


def _zero_investor_row(date_str, pool_item):
    """투자자 데이터 못 받았을 때 0 으로 채운 행. 가격/등락률은 ranking 에서 가져온 값 유지."""
    return {
        "date": date_str,
        "code": pool_item["code"],
        "name": pool_item["name"],
        "current_price": pool_item["current_price"],
        "change_pct":    pool_item["change_pct"],
        "trading_value": pool_item["trading_value"],
        "snapshot_time": pool_item["snapshot_time"],
        "foreign_net_qty": 0, "foreign_net_value": 0,
        "institution_net_qty": 0, "institution_net_value": 0,
        "personal_net_qty": 0, "personal_net_value": 0,
    }


def save_investor(date_str=None):
    """거래대금 TOP_N_INVESTOR 종목의 외인/기관/개인 순매수 + 수집시점 가격 → db/investor/YYYY-MM/YYYYMMDD.csv

    종목 풀: ranking CSV (max trading_value 기준 상위 N), 부족 시 실시간 volume-rank 로 보강.
    실패/데이터 없는 종목은 0 으로 채움. 가격/등락률은 ranking 에 있던 값 유지.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    target_n = config.TOP_N_INVESTOR
    print(f"[investor] {date_str} 외인/기관/개인 매매 데이터 수집 (목표 TOP {target_n})")

    # ---- 1) 종목 풀 ----
    pool = load_top_codes_by_trading_value(date_str, top_n=target_n)
    src = "ranking CSV"
    if len(pool) < target_n:
        print(f"  [info] ranking CSV 종목 {len(pool)}개로 부족 → 실시간 보강 시도")
        try:
            live_rows = get_volume_ranking()
            seen = {p["code"] for p in pool}
            for r in live_rows:
                code = r.get("code", "")
                if not code or code in seen:
                    continue
                pool.append({
                    "code": code,
                    "name": r.get("name", ""),
                    "trading_value": int(r.get("trading_value", 0) or 0),
                    "current_price": int(r.get("current_price", 0) or 0),
                    "change_pct":    float(r.get("change_pct", 0) or 0),
                    "snapshot_time": "live",
                })
                seen.add(code)
                if len(pool) >= target_n:
                    break
            src = "ranking CSV + 실시간 보강"
        except Exception as e:
            print(f"  [warn] 실시간 보강 실패: {e}")

    if not pool:
        print(f"[investor] 종목 풀 비어 있음. 저장 생략.")
        return

    print(f"  대상 종목: {len(pool)}개 (출처: {src})")

    # ---- 2) 종목별 투자자 매매 동향 ----
    results = []
    for i, item in enumerate(pool, 1):
        code, name = item["code"], item["name"]
        try:
            inv = get_stock_investor(code, target_date=date_str)
            if inv:
                row = {
                    "date": date_str, "code": code, "name": name,
                    "current_price": item["current_price"],
                    "change_pct":    item["change_pct"],
                    "trading_value": item["trading_value"],
                    "snapshot_time": item["snapshot_time"],
                    "foreign_net_qty":       inv["foreign_net_qty"],
                    "foreign_net_value":     inv["foreign_net_value"],
                    "institution_net_qty":   inv["institution_net_qty"],
                    "institution_net_value": inv["institution_net_value"],
                    "personal_net_qty":      inv["personal_net_qty"],
                    "personal_net_value":    inv["personal_net_value"],
                }
                results.append(row)
                if i <= 3 or i % 20 == 0:
                    print(f"  [{i}/{len(pool)}] {code} {name} OK "
                          f"(matched_date={inv.get('matched_date','?')})")
            else:
                results.append(_zero_investor_row(date_str, item))
                print(f"  [{i}/{len(pool)}] {code} {name} - 데이터 없음, 0 저장")
        except Exception as e:
            results.append(_zero_investor_row(date_str, item))
            print(f"  [{i}/{len(pool)}] {code} {name} [error] {e} → 0 저장")
        time.sleep(0.5)

    # ---- 3) 저장 ----
    out_path = f"{_month_dir(config.DB_INVESTOR_DIR, date_str)}/{date_str}.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=INVESTOR_FIELDS)
        writer.writeheader()
        for r in results:
            writer.writerow({k: r.get(k, 0) for k in INVESTOR_FIELDS})
    print(f"[investor] {len(results)}개 종목 저장: {out_path}")


# ============================================================
# 일봉 (Daily OHLCV)
# ============================================================
DAILY_FIELDS = [
    "date", "code", "name",
    "bar_date", "open", "high", "low", "close",
    "volume", "trading_value", "change_pct",
]


def save_daily(date_str=None):
    """그날 풀의 TOP_N_INVESTOR 종목 각각에 대해 일봉 DAILY_LOOKBACK_DAYS 일치 수집
    → db/daily/YYYY-MM/YYYYMMDD.csv (한 파일에 모든 종목 누적)

    'date' 컬럼은 수집 기준일, 'bar_date' 는 그 일봉의 실제 거래일.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    target_n = config.TOP_N_INVESTOR
    lookback = config.DAILY_LOOKBACK_DAYS
    print(f"[daily] {date_str} 일봉 수집 (TOP {target_n} × 최근 {lookback}일)")

    pool = load_top_codes_by_trading_value(date_str, top_n=target_n)
    if len(pool) < target_n:
        try:
            live_rows = get_volume_ranking()
            seen = {p["code"] for p in pool}
            for r in live_rows:
                code = r.get("code", "")
                if not code or code in seen:
                    continue
                pool.append({"code": code, "name": r.get("name", ""),
                             "trading_value": 0, "current_price": 0,
                             "change_pct": 0.0, "snapshot_time": "live"})
                seen.add(code)
                if len(pool) >= target_n:
                    break
        except Exception as e:
            print(f"  [warn] 실시간 보강 실패: {e}")

    if not pool:
        print(f"[daily] 종목 풀 없음. 저장 생략.")
        return

    print(f"  대상 종목: {len(pool)}개")

    rows = []
    for i, item in enumerate(pool, 1):
        code, name = item["code"], item["name"]
        try:
            bars = get_daily_chart(code, period_days=lookback)
        except Exception as e:
            print(f"  [{i}/{len(pool)}] {code} {name} [error] {e}")
            time.sleep(0.5)
            continue

        if not bars:
            print(f"  [{i}/{len(pool)}] {code} {name} - 일봉 데이터 없음")
            time.sleep(0.5)
            continue

        for b in bars:
            rows.append({
                "date": date_str, "code": code, "name": name,
                "bar_date":      b["date"],
                "open":  b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
                "volume":        b["volume"],
                "trading_value": b["trading_value"],
                "change_pct":    b["change_pct"],
            })
        if i <= 3 or i % 20 == 0:
            print(f"  [{i}/{len(pool)}] {code} {name} OK ({len(bars)}일)")
        time.sleep(0.5)

    out_path = f"{_month_dir(config.DB_DAILY_DIR, date_str)}/{date_str}.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=DAILY_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    print(f"[daily] {len(rows)}행 저장: {out_path}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "ranking":
        save_ranking()

    elif cmd == "minutes":
        if len(sys.argv) < 3:
            print("사용법: python data_collector.py minutes YYYYMMDD [YYYYMMDD ...]")
            sys.exit(1)
        for date_str in sys.argv[2:]:
            save_minutes(date_str)

    elif cmd == "investor":
        date_str = sys.argv[2] if len(sys.argv) >= 3 else None
        save_investor(date_str)

    elif cmd == "daily":
        date_str = sys.argv[2] if len(sys.argv) >= 3 else None
        save_daily(date_str)

    elif cmd == "today":
        today = datetime.now().strftime("%Y%m%d")

        # 장중(15:30 이전)에 실행하면 ranking + 지수 스냅숏만 누적
        now_hour_min = int(datetime.now().strftime("%H%M"))
        if now_hour_min < 1530:
            save_ranking(today)
            save_index_snapshot(today)
            print("[안내] 장중이므로 30분 단위 ranking + 지수 스냅숏만 누적했습니다.")
            print("       최종 하루치 분봉 + 외인/기관/개인 + 일봉 + 지수분봉 수집은 장 마감(15:30) 이후 수행됩니다.")
        else:
            # 장 마감 후 EOD: 마지막 스냅숏 + 종목분봉 + 투자자 + 일봉 + 지수분봉
            save_ranking(today)
            save_index_snapshot(today)
            save_minutes(today)
            save_investor(today)
            save_daily(today)
            save_index_minutes(today)

    elif cmd == "index":
        # 지수 스냅숏 단독 호출 (디버그/수동 테스트용)
        date_str = sys.argv[2] if len(sys.argv) >= 3 else None
        save_index_snapshot(date_str)

    elif cmd == "index_minutes":
        date_str = sys.argv[2] if len(sys.argv) >= 3 else None
        save_index_minutes(date_str)

    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()