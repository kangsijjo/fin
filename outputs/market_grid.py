"""
market_grid.py — 장중시황 2x6 그리드 데이터 생산기 (2026-06-22 신설)

kiwoom_market(실전 데이터키)으로 코스피·코스닥 각각 6개 지표를 모아 JSON 캐시로 저장.
  거래량 / 상승률 / 기관+외국인 순매수(합산) / 〃 순매도 / 신고가 / 업종등락
대시보드 장중시황 탭이 db/kiwoom/market_grid.json 을 읽어 표시.

실행: python market_grid.py   (장중 15분 스케줄, intraday 생산기 bat 에서 호출)
"""
import os, sys, json
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

OUT = "db/kiwoom/market_grid.json"


def main():
    out = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "markets": {}}
    try:
        from kiwoom_market import KiwoomMarket
        km = KiwoomMarket()
    except Exception as e:
        out["error"] = f"kiwoom_market 초기화 실패: {str(e)[:120]}"
        _write(out)
        print(out["error"])
        return

    for mk in ("kospi", "kosdaq"):
        m = {}
        try:
            nb, ns = km.inst_foreign_combined(mk, 20)
            m = {
                "volume":   km.volume_rank(mk, 20),
                "change":   km.change_rank(mk, 20),
                "netbuy":   nb,
                "netsell":  ns,
                "new_high": km.new_high_rank(mk, 20),
                "sector":   km.sector_change(mk, 20),
            }
        except Exception as e:
            m = {"error": str(e)[:120]}
        out["markets"][mk] = m

    _write(out)
    k = out["markets"].get("kospi", {})
    print(f"[market_grid] 저장: {OUT}  코스피 거래량 {len(k.get('volume', []))} / "
          f"순매수 {len(k.get('netbuy', []))} / 업종 {len(k.get('sector', []))}")


def _write(out):
    try:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception as e:
        print(f"[market_grid] 저장 실패: {e}")


if __name__ == "__main__":
    main()
