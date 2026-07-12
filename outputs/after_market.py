# -*- coding: utf-8 -*-
"""
after_market.py — 시간외 단일가 등락률 순위 수집 → stock.db 누적 (2026-06-28 신설).

배경: 장중시황 그리드(market_grid)는 정규장 순위 TR만 써서 '시간외(애프터마켓)'를
잡지 못하고, stock.db 일별 수집(pykrx EOD)도 정규장만 담는다 → 시간외는 시스템 공백.
이 수집기가 키움 REST `ka10098(시간외단일가등락율순위)`로 코스피·코스닥의 시간외
상승/하락 상위 N을 받아 **stock.db `after_market` 테이블에 날짜별 누적**한다.

- 데이터키(KiwoomMarket, 실전 도메인)로 '조회만'. 주문 키와 별개.
- 스케줄: 시간외 단일가 종료(18:00) 직후 ~18:10 1회 (run_after_market.bat).
- INSERT OR REPLACE (date,market,side,code) → 재실행 안전.
- 대시보드용 스냅샷 db/kiwoom/after_market.json 도 같이 기록.

⚠ ka10098 응답 필드명은 실서버에서만 확정 가능 → _pick 으로 방어 파싱 + `--probe`
   (원본 키 출력)로 18:10 실행 때 필드 확인 후 필요시 후보 추가.

사용:
  python after_market.py            # 수집 + DB 누적
  python after_market.py --probe    # 원본 응답 키만 출력(필드 확정용)
"""
from __future__ import annotations
import os
import sys
import json
import sqlite3
from datetime import datetime

# kiwoom_market 의 클라이언트·헬퍼 재사용
from kiwoom_market import KiwoomMarket, _first_list, _pick, _to_int, _to_float

_HERE = os.path.dirname(os.path.abspath(__file__))
_DB_PATHS = [
    os.path.join(_HERE, "..", "Stock_AI_Project", "data", "stock.db"),
    "C:/fin/Stock_AI_Project/data/stock.db",
]
_JSON_OUT = os.path.join(_HERE, "db", "kiwoom", "after_market.json")
TODAY = datetime.now().strftime("%Y%m%d")
TOPN = 50

# (mrkt_tp, 라벨)
_MARKETS = [("001", "KOSPI"), ("101", "KOSDAQ")]
# (sort_base, side)
_SIDES = [("1", "up"), ("3", "down")]
# 시간외 거래량용 시장 키 (kiwoom_market.volume_rank 는 'kospi'/'kosdaq' 키 사용)
_VOL_MKT = [("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")]
# ETF/ETN 노이즈 제외 휴리스틱 (ka10030 엔 ETF 제외 파라미터가 없어 이름 기반 — 완벽치 않음).
_ETF_PREFIXES = ("KODEX", "TIGER", "RISE", "KBSTAR", "ARIRANG", "KOSEF", "HANARO",
                 "SOL", "ACE", "PLUS", "KINDEX", "TIMEFOLIO", "FOCUS", "히어로즈",
                 "1Q", "WON", "BNK", "마이다스", "파워", "KIWOOM", "VITA")


def _is_etf(name):
    n = (name or "").strip().upper()
    return ("ETN" in n) or any(n.startswith(p.upper()) for p in _ETF_PREFIXES)


def _db_path():
    for p in _DB_PATHS:
        if os.path.exists(p):
            return p
    return _DB_PATHS[0]


def _parse_rows(rows, market, side):
    out = []
    for i, x in enumerate(rows[:TOPN], 1):
        code = str(_pick(x, "stk_cd", "mksc_shrn_iscd", "ovt_stk_cd")).zfill(6)
        if not code or code == "000000":
            continue
        out.append({
            "date": TODAY, "market": market, "side": side, "rank": i,
            "code": code,
            "name": _pick(x, "stk_nm", "hts_kor_isnm"),
            # ka10098 실측 필드(2026-06-28 probe 확정): cur_prc/flu_rt/acc_trde_qty
            # abs: 키움은 등락 방향을 가격 부호로 표기(-3050 = 하락 3050원) —
            # kiwoom_trader.get_positions 와 동일 정규화(2026-07-12, 하락측 전 행 음수 방지)
            "ovt_price": abs(_to_int(_pick(x, "cur_prc", "ovt_sigpric_cur_prc", "stck_prpr"))),
            "ovt_chg_rt": _to_float(_pick(x, "flu_rt", "ovt_sigpric_flu_rt", "prdy_ctrt")),
            "ovt_volume": _to_int(_pick(x, "acc_trde_qty", "ovt_sigpric_trde_qty",
                                        "trde_qty", "acml_vol")),
            # 정규장 종가 등락률 — 시간외와의 '괴리' 분석용(정규장 vs 시간외 반전 포착)
            "reg_close_chg_rt": _to_float(_pick(x, "tdy_close_pric_flu_rt", default="")),
        })
    return out


def _fetch(km, mrkt_tp, sort_base):
    """ka10098 시간외 단일가 등락율 순위 1회 호출 → 원본 rows."""
    # stk_cnd=16: ETF+ETN 제외(노이즈 컷). 0=전체 / 14=ETF만 / 15=스팩 / 17=ETN
    r = km.rank.after_market_price_change_rate_ranking_request_ka10098(
        mrkt_tp=mrkt_tp, sort_base=sort_base,
        stk_cnd="16", trde_qty_cnd="0", crd_cnd="0", trde_prica="0")
    _, rows = _first_list(r)
    return r, rows


def _save_db(records):
    if not records:
        return 0
    p = _db_path()
    con = sqlite3.connect(p, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("""
            CREATE TABLE IF NOT EXISTS after_market (
                date TEXT, market TEXT, side TEXT, rank INTEGER,
                code TEXT, name TEXT,
                ovt_price INTEGER, ovt_chg_rt REAL, ovt_volume INTEGER,
                reg_close_chg_rt REAL,
                PRIMARY KEY (date, market, side, code)
            )""")
        # [2026-07-12] 같은 (date,market,side) 스냅샷은 통째로 교체 — INSERT OR REPLACE 만으론
        # 재실행 시 순위권 이탈 종목의 옛 행(스테일 rank/가격)이 새 스냅샷과 혼재됨.
        for _d, _m, _s in {(r["date"], r["market"], r["side"]) for r in records}:
            con.execute("DELETE FROM after_market WHERE date=? AND market=? AND side=?",
                        (_d, _m, _s))
        con.executemany("""
            INSERT OR REPLACE INTO after_market
              (date, market, side, rank, code, name, ovt_price, ovt_chg_rt, ovt_volume, reg_close_chg_rt)
            VALUES (:date,:market,:side,:rank,:code,:name,:ovt_price,:ovt_chg_rt,:ovt_volume,:reg_close_chg_rt)
            """, records)
        con.commit()
        return len(records)
    finally:
        con.close()


def main():
    probe = "--probe" in sys.argv
    try:
        km = KiwoomMarket()
    except Exception as e:
        print(f"[after_market][ERROR] 키움 초기화 실패: {e}")
        sys.exit(1)

    if probe:
        # 필드 확정용 — 코스피 상승률 1회 원본 키 출력
        r, rows = _fetch(km, "001", "1")
        print("[probe] 응답 최상위 키:", list(r.keys()) if isinstance(r, dict) else type(r))
        if rows:
            print("[probe] 첫 행 키:", list(rows[0].keys()))
            print("[probe] 첫 행 값:", rows[0])
        else:
            print("[probe] 리스트 비어있음(장 시간/시간외 종료 여부 확인)")
        return

    all_recs = []
    snap = {"date": TODAY, "updated": datetime.now().strftime("%H:%M"), "markets": {}}
    for mrkt_tp, mlabel in _MARKETS:
        snap["markets"][mlabel] = {}
        for sort_base, side in _SIDES:
            try:
                _, rows = _fetch(km, mrkt_tp, sort_base)
                recs = _parse_rows(rows, mlabel, side)
                all_recs += recs
                snap["markets"][mlabel][side] = recs[:20]   # 스냅샷은 상위 20만
                print(f"  {mlabel} {side}: {len(recs)}건")
            except Exception as e:
                print(f"  [warn] {mlabel} {side} 조회 실패: {e}")
                snap["markets"][mlabel][side] = []

    # 시간외 거래량 상위 (ka10030 장후시간외=3, ETF/ETN 휴리스틱 제외) — side="vol"
    for mkey, mlabel in _VOL_MKT:
        try:
            rows = km.volume_rank(mkey, n=150, mrkt_open_tp="3")  # 넉넉히 받아 ETF 거른 뒤 top50
            recs = []
            for r in rows:
                if _is_etf(r.get("name", "")):
                    continue
                if len(recs) >= TOPN:
                    break
                recs.append({
                    "date": TODAY, "market": mlabel, "side": "vol", "rank": len(recs) + 1,
                    "code": str(r.get("code", "")).zfill(6), "name": r.get("name", ""),
                    "ovt_price": abs(_to_int(r.get("close", 0))),   # signed 가격 정규화(2026-07-12)
                    "ovt_chg_rt": _to_float(r.get("change_pct", 0.0)),
                    "ovt_volume": _to_int(r.get("value", 0)),
                    "reg_close_chg_rt": "",   # ka10030 미제공
                })
            all_recs += recs
            snap["markets"].setdefault(mlabel, {})["vol"] = recs[:20]
            print(f"  {mlabel} vol: {len(recs)}건 (ETF제외)")
        except Exception as e:
            print(f"  [warn] {mlabel} vol 조회 실패: {e}")

    n = _save_db(all_recs)
    print(f"[after_market] stock.db after_market 누적 {n}건 ({TODAY})")

    try:
        os.makedirs(os.path.dirname(_JSON_OUT), exist_ok=True)
        with open(_JSON_OUT, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        print(f"[after_market] 스냅샷 저장 → {_JSON_OUT}")
    except Exception as e:
        print(f"[after_market] 스냅샷 저장 실패(무시): {e}")


if __name__ == "__main__":
    main()
