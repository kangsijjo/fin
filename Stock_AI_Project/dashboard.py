"""
Stock AI Project — 대시보드 생성기
실행: python dashboard.py  → dashboard.html 생성 후 브라우저로 열림
"""
import os
import sys
import json
import pickle
import sqlite3
import webbrowser
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'stock.db')
MODEL_DIR = os.path.join(os.path.dirname(__file__), 'src', 'models', 'saved')
OUT_PATH = os.path.join(os.path.dirname(__file__), 'dashboard.html')


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ── 1. 데이터 현황 ─────────────────────────────────────────────
def fetch_data_status():
    conn = get_conn()
    tables = {
        'korea_stocks':     '한국 주가',
        'usa_stocks':       '미국 주가',
        'supply_demand':    '외국인/기관 수급',
        'credit_balance':   '신용잔고',
        'stock_lending':    '대차잔고',
        'macro_indicators': '거시지표',
        'korea_indicators': '한국 학습피처',
        'usa_indicators':   '미국 학습피처',
    }
    rows = []
    for tbl, label in tables.items():
        try:
            r = conn.execute(
                f"SELECT COUNT(*) cnt, MIN(date) mn, MAX(date) mx FROM {tbl}"
            ).fetchone()
            rows.append({
                'table': label, 'rows': f"{r['cnt']:,}",
                'start': (r['mn'] or '-')[:10], 'end': (r['mx'] or '-')[:10]
            })
        except Exception:
            rows.append({'table': label, 'rows': '없음', 'start': '-', 'end': '-'})

    # 섹터별 종목수
    try:
        sector_rows = conn.execute("""
            SELECT sector, COUNT(DISTINCT ticker) cnt
            FROM korea_stocks
            WHERE sector IS NOT NULL
              AND sector NOT IN ('None','우량기업부','중견기업부','벤처기업부',
                                  '기술성장기업부','관리종목(소속부없음)',
                                  'SPAC(소속부없음)','투자주의환기종목(소속부없음)',
                                  '외국기업(소속부없음)')
            GROUP BY sector ORDER BY cnt DESC LIMIT 20
        """).fetchall()
        sectors = [{'sector': r['sector'], 'cnt': r['cnt']} for r in sector_rows]
    except Exception:
        sectors = []

    conn.close()
    return rows, sectors


# ── 2. 모델 성능 ────────────────────────────────────────────────
def fetch_model_stats():
    import numpy as np
    models = []
    all_fi = {}
    feature_names = []

    if not os.path.exists(MODEL_DIR):
        return models, []

    for fname in sorted(os.listdir(MODEL_DIR)):
        if not fname.endswith('_model.pkl'):
            continue
        sector = fname.replace('_model.pkl', '')
        try:
            with open(os.path.join(MODEL_DIR, fname), 'rb') as f:
                obj = pickle.load(f)
            model = obj['model']
            features = obj.get('features', [])
            fi = getattr(model, 'feature_importances_', None)
            n_est = getattr(model, 'n_estimators_',
                            getattr(model, 'n_estimators', 0))
            if fi is not None and features:
                if not feature_names:
                    feature_names = list(features)
                for feat, imp in zip(features, fi):
                    all_fi[feat] = all_fi.get(feat, 0) + imp
            models.append({
                'sector': sector,
                'features': len(features),
                'n_est': int(n_est),
                'status': '정상'
            })
        except Exception as e:
            models.append({'sector': sector, 'features': 0,
                           'n_est': 0, 'status': f'오류'})

    # 상위 15개 피처 중요도
    fi_sorted = sorted(all_fi.items(), key=lambda x: -x[1])[:15]
    fi_data = [{'feat': k, 'imp': round(float(v), 1)} for k, v in fi_sorted]
    return models, fi_data


# ── 3. 백테스트 결과 ────────────────────────────────────────────
def fetch_backtest():
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT * FROM backtest_results
            ORDER BY date DESC LIMIT 100
        """).fetchall()
        result = [dict(r) for r in rows]
    except Exception:
        result = []
    conn.close()
    return result


# ── 4. 매매 현황 ────────────────────────────────────────────────
def fetch_positions():
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT ticker, name, market, entry_price, quantity,
                   entry_date, stop_loss, take_profit, status
            FROM positions
            ORDER BY entry_date DESC LIMIT 50
        """).fetchall()
        positions = [dict(r) for r in rows]
    except Exception:
        positions = []
    conn.close()
    return positions



# ── 5. 로그 현황 ────────────────────────────────────────────────
def fetch_logs(max_lines=300):
    """오늘 날짜 로그 + failures.log 읽어 [{level, ts, msg}] 반환."""
    log_dir = os.path.join(os.path.dirname(__file__), 'logs')
    entries = []

    def _parse_file(fpath, tag=None):
        try:
            with open(fpath, encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            for line in lines[-max_lines:]:
                line = line.rstrip()
                if not line:
                    continue
                level = 'INFO'
                for lv in ('CRITICAL', 'ERROR', 'WARNING', 'DEBUG', 'INFO'):
                    if lv + ':' in line:
                        level = lv
                        break
                entries.append({'level': level, 'line': line, 'tag': tag or ''})
        except Exception:
            pass

    # 오늘 날짜 로그
    today_log = os.path.join(log_dir, f"{datetime.now().strftime('%Y-%m-%d')}.log")
    _parse_file(today_log)

    # failures.log (ERROR 전용 누적 파일)
    fail_log = os.path.join(log_dir, 'failures.log')
    _parse_file(fail_log, tag='FAIL')

    return entries[-max_lines:]


# ── HTML 생성 ────────────────────────────────────────────────────
def build_html(data_rows, sectors, models, fi_data, backtest, positions, log_entries):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 로그 현황 HTML
    def _row_class(e):
        lv = e['level']
        if lv in ('ERROR', 'CRITICAL') or e['tag'] == 'FAIL':
            return 'log-err'
        if lv == 'WARNING':
            return 'log-warn'
        if lv == 'DEBUG':
            return 'log-debug'
        return 'log-info'

    log_html = ''.join(
        f'<div class="log-line {_row_class(e)}">{e["line"]}</div>'
        for e in reversed(log_entries)
    ) or '<div class="log-info" style="color:#555">로그 없음</div>'

    data_rows_html = ''.join(
        f"<tr><td>{r['table']}</td><td>{r['rows']}</td>"
        f"<td>{r['start']}</td><td>{r['end']}</td></tr>"
        for r in data_rows
    )

    models_html = ''.join(
        f"<tr><td>{m['sector']}</td><td>{m['features']}</td>"
        f"<td>{m['n_est']}</td>"
        f"<td class='{'ok' if m['status']=='정상' else 'err'}'>{m['status']}</td></tr>"
        for m in models
    )

    backtest_html = ''
    if backtest:
        cols = list(backtest[0].keys())
        header = ''.join(f'<th>{c}</th>' for c in cols)
        backtest_html = f'<tr>{header}</tr>'
        for r in backtest[:20]:
            backtest_html += '<tr>' + ''.join(
                f'<td>{r.get(c,"")}</td>' for c in cols) + '</tr>'
    else:
        backtest_html = '<tr><td colspan="10" style="text-align:center;color:#888">백테스트 결과 없음</td></tr>'

    positions_html = ''
    if positions:
        pos_cols = ['ticker','name','market','entry_price','quantity','entry_date','status']
        positions_html = '<tr>' + ''.join(f'<th>{c}</th>' for c in pos_cols) + '</tr>'
        for p in positions:
            positions_html += '<tr>' + ''.join(
                f'<td>{p.get(c,"")}</td>' for c in pos_cols) + '</tr>'
    else:
        positions_html = '<tr><td colspan="7" style="text-align:center;color:#888">포지션 없음</td></tr>'

    sector_labels = json.dumps([s['sector'] for s in sectors], ensure_ascii=False)
    sector_data   = json.dumps([s['cnt'] for s in sectors])
    fi_labels     = json.dumps([d['feat'] for d in fi_data], ensure_ascii=False)
    fi_values     = json.dumps([d['imp'] for d in fi_data])

    total_models  = len(models)
    ok_models     = sum(1 for m in models if m['status'] == '정상')
    total_sectors = len(sectors)

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stock AI Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#0d1117; color:#e6edf3; font-family:'Segoe UI',sans-serif; }}
  header {{ background:#161b22; border-bottom:1px solid #30363d;
            padding:16px 32px; display:flex; align-items:center; justify-content:space-between; }}
  header h1 {{ font-size:1.3rem; color:#58a6ff; }}
  header span {{ font-size:0.8rem; color:#8b949e; }}
  .tabs {{ display:flex; background:#161b22; border-bottom:1px solid #30363d; padding:0 32px; }}
  .tab {{ padding:12px 20px; cursor:pointer; font-size:0.9rem; color:#8b949e;
          border-bottom:2px solid transparent; transition:.2s; }}
  .tab.active {{ color:#58a6ff; border-bottom-color:#58a6ff; }}
  .tab:hover {{ color:#e6edf3; }}
  .page {{ display:none; padding:24px 32px; }}
  .page.active {{ display:block; }}
  .cards {{ display:flex; gap:16px; margin-bottom:24px; flex-wrap:wrap; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:8px;
           padding:16px 20px; min-width:160px; }}
  .card .val {{ font-size:1.8rem; font-weight:700; color:#58a6ff; }}
  .card .lbl {{ font-size:0.75rem; color:#8b949e; margin-top:4px; }}
  .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
  .box {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:20px; }}
  .box h3 {{ font-size:0.9rem; color:#8b949e; margin-bottom:16px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.85rem; }}
  th {{ background:#21262d; color:#8b949e; padding:8px 12px; text-align:left;
        font-weight:500; border-bottom:1px solid #30363d; }}
  td {{ padding:8px 12px; border-bottom:1px solid #21262d; }}
  tr:hover td {{ background:#161b22; }}
  .ok {{ color:#3fb950; }}
  .err {{ color:#f85149; }}
  canvas {{ max-height:320px; }}
  @media(max-width:768px) {{ .grid2 {{ grid-template-columns:1fr; }} }}
  .log-box {{ background:#0d1117; border:1px solid #30363d; border-radius:8px;
              padding:16px; max-height:600px; overflow-y:auto; font-family:monospace; font-size:0.78rem; }}
  .log-line {{ padding:2px 0; border-bottom:1px solid #161b22; white-space:pre-wrap; word-break:break-all; }}
  .log-err   {{ color:#f85149; }}
  .log-warn  {{ color:#e3b341; }}
  .log-info  {{ color:#c9d1d9; }}
  .log-debug {{ color:#484f58; }}
</style>
</head>
<body>

<header>
  <h1>📈 Stock AI Dashboard</h1>
  <span>마지막 갱신: {now}</span>
</header>

<div class="tabs">
  <div class="tab active" onclick="show('data')">데이터 현황</div>
  <div class="tab" onclick="show('model')">모델 성능</div>
  <div class="tab" onclick="show('backtest')">백테스트</div>
  <div class="tab" onclick="show('trade')">매매 현황</div>
  <div class="tab" onclick="show('logs')">로그 현황</div>
</div>

<!-- 데이터 현황 -->
<div id="page-data" class="page active">
  <div class="cards">
    <div class="card"><div class="val">{total_sectors}</div><div class="lbl">학습 섹터 수</div></div>
    <div class="card"><div class="val">{ok_models}</div><div class="lbl">정상 모델 수</div></div>
    <div class="card"><div class="val">{total_models}</div><div class="lbl">전체 모델 수</div></div>
  </div>
  <div class="grid2">
    <div class="box">
      <h3>테이블별 데이터 현황</h3>
      <table>
        <tr><th>테이블</th><th>행수</th><th>시작일</th><th>종료일</th></tr>
        {data_rows_html}
      </table>
    </div>
    <div class="box">
      <h3>섹터별 종목수</h3>
      <canvas id="chartSector"></canvas>
    </div>
  </div>
</div>

<!-- 모델 성능 -->
<div id="page-model" class="page">
  <div class="grid2">
    <div class="box">
      <h3>섹터별 모델 현황</h3>
      <table>
        <tr><th>섹터</th><th>피처수</th><th>트리수</th><th>상태</th></tr>
        {models_html}
      </table>
    </div>
    <div class="box">
      <h3>피처 중요도 Top 15 (전 섹터 평균)</h3>
      <canvas id="chartFI"></canvas>
    </div>
  </div>
</div>

<!-- 백테스트 -->
<div id="page-backtest" class="page">
  <div class="box">
    <h3>백테스트 결과</h3>
    <table>{backtest_html}</table>
  </div>
</div>

<!-- 매매 현황 -->
<div id="page-trade" class="page">
  <div class="box">
    <h3>포지션 현황</h3>
    <table>{positions_html}</table>
  </div>
</div>

<!-- 로그 현황 -->
<div id="page-logs" class="page">
  <div class="box">
    <h3>로그 현황 <span style="font-size:0.75rem;color:#484f58;">(최신순 · ERROR=빨강 WARNING=노랑 INFO=흰색)</span></h3>
    <div class="log-box">{log_html}</div>
  </div>
</div>


<script>
function show(page) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-'+page).classList.add('active');
  event.target.classList.add('active');
}}

// 섹터별 종목수 차트
new Chart(document.getElementById('chartSector'), {{
  type: 'bar',
  data: {{
    labels: {sector_labels},
    datasets: [{{ label: '종목수', data: {sector_data},
      backgroundColor: 'rgba(88,166,255,0.7)', borderRadius: 4 }}]
  }},
  options: {{
    indexAxis: 'y', plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ color:'#8b949e' }}, grid: {{ color:'#21262d' }} }},
      y: {{ ticks: {{ color:'#e6edf3', font:{{size:11}} }}, grid: {{ display:false }} }}
    }}
  }}
}});

// 피처 중요도 차트
new Chart(document.getElementById('chartFI'), {{
  type: 'bar',
  data: {{
    labels: {fi_labels},
    datasets: [{{ label: '중요도', data: {fi_values},
      backgroundColor: 'rgba(63,185,80,0.7)', borderRadius: 4 }}]
  }},
  options: {{
    indexAxis: 'y', plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ color:'#8b949e' }}, grid: {{ color:'#21262d' }} }},
      y: {{ ticks: {{ color:'#e6edf3', font:{{size:11}} }}, grid: {{ display:false }} }}
    }}
  }}
}});
</script>
</body>
</html>"""
    return html


def main():
    print("대시보드 데이터 수집 중...")
    data_rows, sectors   = fetch_data_status()
    models, fi_data      = fetch_model_stats()
    backtest             = fetch_backtest()
    positions            = fetch_positions()
    log_entries          = fetch_logs()

    print("HTML 생성 중...")
    html = build_html(data_rows, sectors, models, fi_data, backtest, positions, log_entries)

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"완료: {OUT_PATH}")
    webbrowser.open(f'file:///{OUT_PATH.replace(os.sep, "/")}')


if __name__ == '__main__':
    main()
