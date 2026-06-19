"""
종목별 신용잔고 수집기 — KIS API (probe 방식).

배경:
  - pykrx 는 신용잔고 미지원 (KRX 가 화면 개편하며 제거).
  - KIS 오픈API 의 "신용잔고 일별추이" 로 종목 단위 조회를 시도한다.
  - ⚠️ TR ID/경로는 공식 문서로 재확인 못 한 후보값 — 첫 실행 시 자동 검증(probe)
    하고, 실패하면 로그에 명확히 남긴다. 실패 시 KIS 개발자센터
    (apiportal.koreainvestment.com) 에서 "[국내주식] 시세분석 > 신용잔고" 항목의
    URL/TR ID 를 확인해 아래 CANDIDATES 에 추가하면 된다.

수집 대상 (전 종목 X — 호출량 절약):
  1. paper_signals.csv 최근 60일 신호 종목 (보유 가능성)
  2. data/rankings 최신 CSV 의 상위 거래대금 종목

저장: db/credit/YYYY-MM/YYYYMMDD.csv  (응답 스키마 그대로 + code 컬럼)

실행:
  python credit_collector.py            # 수집
  python credit_collector.py probe      # 엔드포인트 검증만 (1종목)
"""

import os
import sys
import csv
import glob
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

import config
from kis_api import get_access_token, BASE_URL

CREDIT_DIR = "./db/credit"

# 후보 (URL path, TR ID) — 위에서부터 순서대로 probe
CANDIDATES = [
    ("/uapi/domestic-stock/v1/quotations/daily-credit-balance", "FHPST04760000"),
]
PROBE_CACHE = f"{CREDIT_DIR}/.endpoint_ok"   # 검증 성공한 인덱스 저장


def _headers(tr_id):
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {get_access_token()}",
        "appkey": config.KIS_APP_KEY,
        "appsecret": config.KIS_APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }


def _call(path, tr_id, code, date_str):
    """신용잔고 일별 조회 — 응답 JSON 그대로 반환 (스키마 비의존)."""
    url = f"{BASE_URL}{path}"
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": code,
        "fid_input_date_1": date_str,
        "fid_cond_scr_div_code": "20476",
    }
    r = requests.get(url, headers=_headers(tr_id), params=params, timeout=10)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    data = r.json()
    if data.get("rt_cd") not in ("0", 0):
        return None, f"rt_cd={data.get('rt_cd')} msg={data.get('msg1', '')[:120]}"
    rows = data.get("output") or data.get("output1") or []
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        return None, "빈 응답 (output 없음)"
    return rows, None


def probe(date_str):
    """후보 엔드포인트 검증. 성공 인덱스 반환, 전부 실패 시 None."""
    test_code = "247540"  # 에코프로비엠 (유동성 큰 KOSDAQ)
    for i, (path, tr) in enumerate(CANDIDATES):
        rows, err = _call(path, tr, test_code, date_str)
        if rows:
            print(f"[probe] OK — {path} ({tr}), 필드: {list(rows[0].keys())[:8]}")
            os.makedirs(CREDIT_DIR, exist_ok=True)
            with open(PROBE_CACHE, "w") as f:
                f.write(str(i))
            return i
        print(f"[probe] 실패 — {path} ({tr}): {err}")
    print("[probe] 모든 후보 실패. KIS 개발자센터에서 '신용잔고' API 의 "
          "URL/TR ID 확인 후 CANDIDATES 에 추가 필요.")
    return None


def _load_endpoint(date_str):
    if os.path.exists(PROBE_CACHE):
        try:
            i = int(open(PROBE_CACHE).read().strip())
            if 0 <= i < len(CANDIDATES):
                return i
        except Exception:
            pass
    return probe(date_str)


def target_codes():
    """수집 대상 종목: 최근 60일 신호 + 최신 ranking 상위."""
    codes = []
    if os.path.exists("./paper_signals.csv"):
        s = pd.read_csv("./paper_signals.csv", dtype={"code": str})
        s["signal_date"] = s["signal_date"].astype(str)
        cutoff = (datetime.today() - timedelta(days=90)).strftime("%Y%m%d")
        codes += s[s["signal_date"] >= cutoff]["code"].str.zfill(6).tolist()
    ranks = sorted(glob.glob("./data/rankings/*.csv"))
    if ranks:
        try:
            r = pd.read_csv(ranks[-1], dtype=str)
            col = "code" if "code" in r.columns else r.columns[0]
            codes += r[col].astype(str).str.zfill(6).head(50).tolist()
        except Exception:
            pass
    seen, out = set(), []
    for c in codes:
        if c not in seen and len(c) == 6:
            seen.add(c)
            out.append(c)
    return out


def main():
    today = datetime.today().strftime("%Y%m%d")
    month_dir = f"{CREDIT_DIR}/{datetime.today():%Y-%m}"
    os.makedirs(month_dir, exist_ok=True)
    save_path = f"{month_dir}/{today}.csv"

    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        probe(today)
        return

    if os.path.exists(save_path):
        print(f"[credit] {save_path} 이미 존재 (멱등). 종료.")
        return

    idx = _load_endpoint(today)
    if idx is None:
        sys.exit(1)
    path, tr = CANDIDATES[idx]

    codes = target_codes()
    if not codes:
        print("[credit] 수집 대상 없음 (신호/랭킹 데이터 없음)")
        return
    print(f"[credit] {len(codes)} 종목 수집 시작 ({path})")

    all_rows, fails = [], 0
    for i, code in enumerate(codes):
        rows, err = _call(path, tr, code, today)
        if rows:
            r0 = dict(rows[0])      # 최신 1행 (당일 또는 최근 영업일)
            r0["code"] = code
            all_rows.append(r0)
        else:
            fails += 1
            if fails <= 3:
                print(f"  [warn] {code}: {err}")
        time.sleep(0.06)            # 유량 제한 (초당 ~15회)

    if not all_rows:
        print("[credit] 수집 결과 0건 — 실패로 처리")
        sys.exit(1)

    fields = ["code"] + [k for k in all_rows[0].keys() if k != "code"]
    with open(save_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"[credit] {len(all_rows)}종목 저장 ({fails}건 실패): {save_path}")


if __name__ == "__main__":
    main()
