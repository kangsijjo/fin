"""
intraday_server.py — 장중 실시간 대시보드 서버 (포트 5001)

역할:
  • intraday_dashboard.html 에 API 데이터 공급
  • GET  /api/data    → intraday_cache.json 반환 (캐시)
  • POST /api/refresh → intraday_monitor.run() 즉시 실행 후 최신 데이터 반환
  • GET  /            → intraday_dashboard.html 서빙

실행:
  python intraday_server.py          # 포트 5001, localhost
  python intraday_server.py --port 5002

브라우저에서 http://localhost:5001 접속.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

os.chdir(Path(__file__).parent)
sys.path.insert(0, str(Path(__file__).parent))

try:
    from flask import Flask, jsonify, send_from_directory
    from flask_cors import CORS
except ImportError:
    print("[ERROR] flask / flask-cors 필요: pip install flask flask-cors")
    sys.exit(1)

app = Flask(__name__, static_folder=".")
CORS(app)

CACHE_PATH = Path("db/kiwoom/intraday_cache.json")
DASHBOARD_HTML = Path("intraday_dashboard.html")


def _read_cache() -> dict:
    """intraday_cache.json 읽기. 없으면 빈 구조 반환."""
    if not CACHE_PATH.exists():
        return {
            "updated_at": None,
            "data_source": "no_cache",
            "volume_top20": [], "inst_top20": [], "foreign_top20": [],
            "exec_top20":   [], "positions":  [],
        }
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}


@app.route("/")
def index():
    """대시보드 HTML 서빙."""
    if DASHBOARD_HTML.exists():
        return send_from_directory(".", "intraday_dashboard.html")
    return "<h2>intraday_dashboard.html 파일이 없습니다.</h2>", 404


@app.route("/api/data")
def api_data():
    """캐시된 데이터 반환 (가볍고 빠름)."""
    return jsonify(_read_cache())


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """
    intraday_monitor.run() 즉시 실행 → 캐시 갱신 후 최신 데이터 반환.
    장시간 소요 가능 (10~30초) — 프론트엔드에서 로딩 표시 권장.
    """
    try:
        import intraday_monitor
        intraday_monitor.run()
        return jsonify({"ok": True, "refreshed_at": datetime.now().strftime("%H:%M:%S"),
                        **_read_cache()})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e),
                        "trace": traceback.format_exc()[-500:]}), 500


@app.route("/api/status")
def api_status():
    cache = _read_cache()
    return jsonify({
        "server": "intraday_server",
        "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cache_updated_at": cache.get("updated_at"),
        "data_source": cache.get("data_source"),
        "positions_count": len(cache.get("positions", [])),
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    print(f"\n{'='*50}")
    print(f"  장중 대시보드 서버 시작")
    print(f"  http://{args.host}:{args.port}")
    print(f"  캐시: {CACHE_PATH}")
    print(f"{'='*50}\n")

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
