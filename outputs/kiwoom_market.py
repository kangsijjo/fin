"""
kiwoom_market.py — 키움 REST '순위정보' 조회 전용 클라이언트 (2026-06-21 신설)

장중 시황(2x6 그리드)용 랭킹 데이터를 키움 신형 REST API 로 가져온다.
주문용 kiwoom_trader.py 와 별개 — 여기서는 '조회만' 하며, 시세/순위 데이터는
실전(데이터용) 앱키가 필요하므로 별도 환경변수를 쓴다(모의 주문키와 분리).

환경변수(.env):
  KIWOOM_DATA_APP_KEY / KIWOOM_DATA_APP_SECRET   # 실전 데이터용 앱키(무료)
  (없으면 KIWOOM_PROD_APP_KEY/SECRET → 그것도 없으면 모의키로 시도)

제공 메서드(모두 시장 분리 가능: 'kospi' | 'kosdaq' | 'all'):
  volume_rank(market, n)          거래량 상위
  change_rank(market, n)          전일대비 상승률 상위 (ka10027)
  inst_foreign_combined(market,n) 기관+외국인 합산 순매수/순매도 상위 (ka90009)  → (netbuy, netsell)
  new_high_rank(market, n)        신고가 (ka10016)        ※ 응답필드 장중 검증 필요
  sector_change(market, n)        업종 등락률 (ka20003)   ※ 응답필드 장중 검증 필요

단독 실행 = 프로브 모드: 각 TR 원본 응답 키를 출력 → 장중에 돌려 응답 필드명을 확정한다.
  python kiwoom_market.py            # 전체 프로브
  python kiwoom_market.py kospi      # 코스피만
"""
import os
import sys
import json

# ── 시장/거래소 코드 (키움 docstring 기준) ──────────────────────────────────────
_MRKT = {"all": "000", "kospi": "001", "kosdaq": "101"}
_STEX = "1"   # 1=KRX, 2=NXT, 3=통합


def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.split("#")[0].strip().strip('"').strip("'"))


def _to_int(v, default=0):
    try:
        return int(float(str(v).replace(",", "").replace("+", "").strip()))
    except Exception:
        return default


def _to_float(v, default=0.0):
    try:
        return round(float(str(v).replace(",", "").replace("+", "").strip()), 2)
    except Exception:
        return default


def _pick(d, *cands, default=""):
    """후보 키 중 첫 번째로 값이 있는 것 반환(응답 필드명이 TR마다 달라 방어적 파싱)."""
    for c in cands:
        if c in d and d[c] not in (None, ""):
            return d[c]
    return default


def _first_list(data):
    """KIS/키움 응답에서 첫 번째 비어있지 않은 리스트 필드를 찾아 (키, 리스트) 반환."""
    if not isinstance(data, dict):
        return None, []
    for k, v in data.items():
        if isinstance(v, list) and v:
            return k, v
    return None, []


class KiwoomMarket:
    def __init__(self):
        _load_dotenv()
        key = (os.getenv("KIWOOM_DATA_APP_KEY")
               or os.getenv("KIWOOM_PROD_APP_KEY")
               or os.getenv("KIWOOM_MOCK_APP_KEY", ""))
        sec = (os.getenv("KIWOOM_DATA_APP_SECRET")
               or os.getenv("KIWOOM_PROD_APP_SECRET")
               or os.getenv("KIWOOM_MOCK_APP_SECRET", ""))
        if not key or not sec:
            raise RuntimeError("키움 데이터 앱키 없음 (.env: KIWOOM_DATA_APP_KEY/SECRET)")
        # 라이브러리가 import 시점에 환경변수를 읽으므로 import 전에 주입.
        # 시세/순위 데이터는 실전 도메인 → USE_SANDBOX=false 고정.
        os.environ["KIWOOM_API_KEY"] = key
        os.environ["KIWOOM_API_SECRET"] = sec
        os.environ["KIWOOM_USE_SANDBOX"] = "false"

        from kiwoom_rest_api.config import get_base_url
        from kiwoom_rest_api.auth.token import TokenManager
        from kiwoom_rest_api.koreanstock import rank_info, sector, stockinfo

        base = get_base_url()
        tm = TokenManager()
        tm.get_token()

        def _cls(mod):
            import inspect
            for _, c in inspect.getmembers(mod, inspect.isclass):
                if c.__module__ == mod.__name__:
                    return c(base_url=base, token_manager=tm)
            raise RuntimeError(f"{mod.__name__} 클래스 없음")

        self.rank = _cls(rank_info)
        self.sector = _cls(sector)
        self.stock = _cls(stockinfo)

    # ── 거래량 상위 (ka10030) ────────────────────────────────────────────────
    def volume_rank(self, market="all", n=5):
        r = self.rank.top_trading_volume_today_request_ka10030(
            mrkt_tp=_MRKT[market], sort_tp="1", mang_stk_incls="0", crd_tp="0",
            trde_qty_tp="0", pric_tp="0", trde_prica_tp="0", mrkt_open_tp="0", stex_tp=_STEX)
        _, rows = _first_list(r)
        out = []
        for i, x in enumerate(rows[:n], 1):
            out.append({
                "rank": i,
                "code": str(_pick(x, "stk_cd", "mksc_shrn_iscd")).zfill(6),
                "name": _pick(x, "stk_nm", "hts_kor_isnm"),
                "close": _to_int(_pick(x, "cur_prc", "stck_prpr")),
                "change_pct": _to_float(_pick(x, "flu_rt", "prdy_ctrt")),
                "value": _to_int(_pick(x, "now_trde_qty", "trde_qty", "acml_vol")),
            })
        return out

    # ── 상승률 상위 (ka10027) ────────────────────────────────────────────────
    def change_rank(self, market="all", n=5):
        r = self.rank.top_day_over_day_change_rate_request_ka10027(
            mrkt_tp=_MRKT[market], sort_tp="1", trde_qty_cnd="0000", stk_cnd="0",
            crd_cnd="0", updown_incls="1", pric_cnd="0", trde_prica_cnd="0", stex_tp=_STEX)
        _, rows = _first_list(r)
        out = []
        for i, x in enumerate(rows[:n], 1):
            out.append({
                "rank": i,
                "code": str(_pick(x, "stk_cd", "mksc_shrn_iscd")).zfill(6),
                "name": _pick(x, "stk_nm", "hts_kor_isnm"),
                "close": _to_int(_pick(x, "cur_prc", "stck_prpr")),
                "change_pct": _to_float(_pick(x, "flu_rt", "prdy_ctrt")),
            })
        return out

    # ── 기관+외국인 합산 순매수/순매도 (ka90009) ──────────────────────────────
    def inst_foreign_combined(self, market="all", n=5):
        """외국인/기관 매매상위 → 종목별 (외인순매수+기관순매수) 합산해 매수/매도 양방향 상위 N.

        ka90009 응답은 외인/기관의 순매수·순매도 항목을 함께 준다(amt_qty_tp=1: 금액 천만원).
        응답 필드명이 환경에 따라 다를 수 있어 방어적으로 합산한다(프로브로 확정 권장).
        """
        r = self.rank.top_foreign_institution_trades_request_ka90009(
            mrkt_tp=_MRKT[market], amt_qty_tp="1", qry_dt_tp="0", stex_tp=_STEX)
        _, rows = _first_list(r)
        # ★ ka90009 실제 포맷(장중 프로브 확정): 한 행에 4개의 '서로 다른' 종목이
        #   side-by-side 로 들어온다 — 외인순매도/외인순매수/기관순매도/기관순매수 각 i위.
        #   따라서 행마다 4개 항목을 코드별로 집계해 (외인+기관) 순매수/순매도를 합산.
        agg = {}   # code -> {name, net}  (순매수 +, 순매도 -)

        def _add(code, name, amt):
            code = str(code or "").zfill(6)
            if not code or code == "000000":
                return
            e = agg.setdefault(code, {"name": name or "", "net": 0})
            e["net"] += amt
            if name and not e["name"]:
                e["name"] = name

        for x in rows:
            _add(x.get("for_netprps_stk_cd"), x.get("for_netprps_stk_nm"),
                 abs(_to_int(x.get("for_netprps_amt"))))      # 외인 순매수 (+)
            _add(x.get("orgn_netprps_stk_cd"), x.get("orgn_netprps_stk_nm"),
                 abs(_to_int(x.get("orgn_netprps_amt"))))     # 기관 순매수 (+)
            _add(x.get("for_netslmt_stk_cd"), x.get("for_netslmt_stk_nm"),
                 -abs(_to_int(x.get("for_netslmt_amt"))))     # 외인 순매도 (-)
            _add(x.get("orgn_netslmt_stk_cd"), x.get("orgn_netslmt_stk_nm"),
                 -abs(_to_int(x.get("orgn_netslmt_amt"))))    # 기관 순매도 (-)

        items = [{"code": c, **v} for c, v in agg.items()]
        buys = sorted([x for x in items if x["net"] > 0], key=lambda x: x["net"], reverse=True)[:n]
        sells = sorted([x for x in items if x["net"] < 0], key=lambda x: x["net"])[:n]
        for i, x in enumerate(buys, 1):
            x["rank"] = i
        for i, x in enumerate(sells, 1):
            x["rank"] = i
        return buys, sells

    # ── 신고가 (ka10016) ※ 파라미터/응답필드 장중 검증 필요 ────────────────────
    def new_high_rank(self, market="all", n=5):
        r = self.stock.reported_low_price_request_ka10016(
            market_type=_MRKT[market], report_type="1", high_low_close_type="1",
            stock_condition="0", trade_quantity_type="00000", credit_condition="0",
            updown_include="1", period="250", stock_exchange_type=_STEX)
        _, rows = _first_list(r)
        out = []
        for i, x in enumerate(rows[:n], 1):
            out.append({
                "rank": i,
                "code": str(_pick(x, "stk_cd", "mksc_shrn_iscd")).zfill(6),
                "name": _pick(x, "stk_nm", "hts_kor_isnm"),
                "close": _to_int(_pick(x, "cur_prc", "stck_prpr")),
                "change_pct": _to_float(_pick(x, "flu_rt", "prdy_ctrt")),
            })
        return out

    # ── 업종 등락률 (ka20003 전업종지수) ※ 응답필드 장중 검증 필요 ─────────────
    def sector_change(self, market="all", n=5):
        # inds_cd: 업종코드. 001=종합(KOSPI)/101=종합(KOSDAQ) 류 — 전업종 조회는 응답에서 리스트로 옴.
        r = self.sector.all_industries_index_request_ka20003(inds_cd="001")
        _, rows = _first_list(r)
        out = []
        for x in rows:
            out.append({
                "name": _pick(x, "inds_nm", "stk_nm", "hts_kor_isnm"),
                "close": _to_float(_pick(x, "cur_prc", "now_prc")),
                "change_pct": _to_float(_pick(x, "flu_rt", "prdy_ctrt")),
            })
        out = sorted(out, key=lambda z: z["change_pct"], reverse=True)[:n]
        for i, x in enumerate(out, 1):
            x["rank"] = i
        return out


# ── 프로브 모드: 원본 응답 키를 출력해 응답 필드명을 확정한다 ──────────────────
def _probe(market="all"):
    km = KiwoomMarket()
    print(f"[probe] market={market}\n")

    def dump(label, fn):
        try:
            raw = fn()
            print(f"== {label} ==")
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, list):
                        print(f"  {k}: list({len(v)})", "  첫행 키:", list(v[0].keys()) if v else "[]")
                    else:
                        print(f"  {k}: {str(v)[:50]}")
            print()
        except Exception as e:
            print(f"== {label} == 오류: {e}\n")

    m = _MRKT[market]
    dump("ka10030 거래량", lambda: km.rank.top_trading_volume_today_request_ka10030(
        mrkt_tp=m, sort_tp="1", mang_stk_incls="0", crd_tp="0", trde_qty_tp="0",
        pric_tp="0", trde_prica_tp="0", mrkt_open_tp="0", stex_tp=_STEX))
    dump("ka10027 상승률", lambda: km.rank.top_day_over_day_change_rate_request_ka10027(
        mrkt_tp=m, sort_tp="1", trde_qty_cnd="0000", stk_cnd="0", crd_cnd="0",
        updown_incls="1", pric_cnd="0", trde_prica_cnd="0", stex_tp=_STEX))
    dump("ka90009 외인기관매매상위", lambda: km.rank.top_foreign_institution_trades_request_ka90009(
        mrkt_tp=m, amt_qty_tp="1", qry_dt_tp="0", stex_tp=_STEX))
    dump("ka10016 신고가", lambda: km.stock.reported_low_price_request_ka10016(
        market_type=m, report_type="1", high_low_close_type="1", stock_condition="0",
        trade_quantity_type="00000", credit_condition="0", updown_include="1",
        period="250", stock_exchange_type=_STEX))
    dump("ka20003 전업종지수", lambda: km.sector.all_industries_index_request_ka20003(inds_cd="001"))


if __name__ == "__main__":
    mk = sys.argv[1] if len(sys.argv) > 1 else "all"
    _probe(mk if mk in _MRKT else "all")
