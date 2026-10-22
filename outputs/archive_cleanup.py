# -*- coding: utf-8 -*-
"""
archive_cleanup.py — 안 쓰는 대용량 파일을 C:\\fin\\_archive\\YYYYMMDD\\ 로 **이동**(삭제 아님). (2026-10-18)

왜: outputs 가 2GB 를 넘었는데 대부분이 '매일 통째로 새로 쓰는 결과의 옛날 판'과 일회성 산출물이다.
    지우지 않고 보관함으로 옮긴 뒤 MANIFEST.csv 를 남긴다 → 확인 후 사용자가 직접 비우거나 --restore 로 되돌린다.

대상(--sets, 쉼표 구분)
  snapshots : results/ 의 trades_{A,B,AB}_*, strategy_lab_trades_*, strategy_equity_* — 종류별 **최신 1개는 남김**
              (대시보드는 각 종류의 최신 파일만 읽는다)
  oneoff    : trades_history_v3_{final,ind,new,now}.csv, backup/trades_history_v3_*.csv,
              calculator_data_*.csv, results/_base_trades_cache_*.pkl (전부 재생성 가능)
  ticks     : db/ticks.db (+wal/shm) — 읽는 곳 없음. 옮기기 전에 KIS_Tick_Collector 작업을 꺼야 다시 안 쌓인다.
  legacy_scripts / research / olddocs : [2026-10-23 §66] 안 쓰는 옛 bat·ps1, 끝난 연구 스크립트(+결과 csv), 옛 문서·db_export

사용
  python archive_cleanup.py --dry-run                         # 무엇을 옮길지 목록만
  python archive_cleanup.py --sets snapshots,oneoff,ticks     # 이동
  python archive_cleanup.py --restore C:\\fin\\_archive\\20261018  # 되돌리기(원위치에 파일이 없을 때만)
"""
import argparse
import csv
import os
import re
import shutil
import sys
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))          # C:\fin\outputs
ROOT = os.path.dirname(HERE)                               # C:\fin
ARCHIVE_ROOT = os.path.join(ROOT, "_archive")
RECENT_SEC = 600          # 10분 안에 바뀐 파일은 쓰는 중일 수 있어 건너뜀

# (그룹 키 정규식) — 그룹 1 = 종류, 그룹 2 = 정렬용 날짜. 종류별 최신 1개 유지.
_SNAP_RE = [re.compile(r"^(trades_(?:AB|A|B)_\d{8}-)(\d{8})\.csv$"),
            re.compile(r"^(strategy_lab_trades_)(\d{8})\.csv$"),
            re.compile(r"^(strategy_equity_)(\d{8})\.csv$")]
_ONEOFF_ROOT = re.compile(r"^(trades_history_v3_(?:final|ind|new|now)\.csv|calculator_data_\d{8}_\d{8}\.csv)$")


def _files(d):
    try:
        return sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))
    except FileNotFoundError:
        return []


def pick_snapshots(results_dir):
    groups = {}
    for f in _files(results_dir):
        for rx in _SNAP_RE:
            m = rx.match(f)
            if m:
                groups.setdefault(m.group(1), []).append((m.group(2), f))
                break
    out = []
    for _, v in sorted(groups.items()):
        v.sort()
        out += [os.path.join(results_dir, f) for _, f in v[:-1]]      # 최신 1개 제외
    return out


def pick_oneoff(here):
    out = [os.path.join(here, f) for f in _files(here) if _ONEOFF_ROOT.match(f)]
    out += [os.path.join(here, "backup", f) for f in _files(os.path.join(here, "backup"))
            if re.match(r"^trades_history_v3_\d{8}_\d{4}\.csv$", f)]
    out += [os.path.join(here, "results", f) for f in _files(os.path.join(here, "results"))
            if re.match(r"^_base_trades_cache_.*\.pkl$", f)]
    return out


def pick_ticks(here):
    base = os.path.join(here, "db", "ticks.db")
    return [p for p in (base, base + "-wal", base + "-shm", base + "-journal") if os.path.isfile(p)]


# [2026-10-23] 2차 정리(§66) — 자동작업·대시보드·트레이더·테스트 어디서도 실행/import 하지 않음을 참조 검사로 확인한 것만.
LEGACY_SCRIPTS = ["register_task.bat", "unregister_task.bat",              # 옛 KISDataCollector_* 작업 등록/해제
                  "cleanup_legacy_tasks.bat", "cleanup_legacy_tasks.ps1",  # 2026-07 1회 정리 완료
                  "rebuild_after_backfill.bat",          # run_verify_kis.bat/verify_kis_account.py 는 KIS 조회 점검 도구라 유지
                  "start_monitor.bat", "monitor.ps1", "taillog.ps1",
                  "start_dashboard.bat"]                                  # run_dashboard.bat 로 통합
RESEARCH = ["apply_trades_rebuild.py", "backfill_strength.py", "backfill_investor_breakdown.py",
            "breaker_sweep.py", "build_stop_counterfactual.py", "flow_form_diagnostic.py",
            "foreign_ratio_backtest.py", "improvement_map.py", "investor_stock_ic.py", "kospi_beta_control.py",
            "mkt_flow_gate.py", "mkt_flow_gate2.py", "mkt_regime_2018.py", "portfolio_fixes.py",
            "profit_sweep.py", "regime_feature_ic.py", "regime_gate_capital.py", "regime_gate_final.py",
            "regime_gate_live_sim.py", "regime_on_live_filter.py", "regime_strength_sim.py", "shortfund_ic.py",
            "signal_count.py", "stop_sweep.py", "strategy_verify.py", "strength_per_strategy_cut.py",
            "strength_relative_threshold.py", "strength_threshold_study.py", "strength_year_sim.py",
            "trades_rebuild_compare.py", "unused_feature_scan.py", "xover_exit_grid.py", "xover_exit_matrix.py",
            "xover_stop_sweep.py", "xover_variants.py",
            "breaker_sweep_result.csv", "profit_sweep_result.csv", "stop_sweep_result.csv", "strength_year_trades.csv"]
OLD_DOCS = ["AUTOMATION_GUIDE.md", "BACKTEST_GUIDE.md", "pt_dashboard.html"]


def pick_list(names, subdirs=()):
    out = [os.path.join(HERE, f) for f in names if os.path.isfile(os.path.join(HERE, f))]
    for d in subdirs:
        out += [os.path.join(HERE, d, f) for f in _files(os.path.join(HERE, d))]
    return out


PICKERS = {"snapshots": lambda: pick_snapshots(os.path.join(HERE, "results")),
           "legacy_scripts": lambda: pick_list(LEGACY_SCRIPTS),
           "research": lambda: pick_list(RESEARCH),
           "olddocs": lambda: pick_list(OLD_DOCS, subdirs=("db_export",)),
           "oneoff": lambda: pick_oneoff(HERE),
           "ticks": lambda: pick_ticks(HERE)}


def archive(sets, dry=False, stamp=None, now=None):
    stamp = stamp or datetime.now().strftime("%Y%m%d")
    now = now or time.time()
    dest_root = os.path.join(ARCHIVE_ROOT, stamp)
    plan = []
    for s in sets:
        for p in PICKERS[s]():
            plan.append((s, p))
    moved, skipped, total = [], [], 0
    for s, src in plan:
        rel = os.path.relpath(src, ROOT)
        dst = os.path.join(dest_root, rel)
        size = os.path.getsize(src)
        if now - os.path.getmtime(src) < RECENT_SEC:
            skipped.append((rel, "최근 10분 안에 변경 — 쓰는 중일 수 있음"))
            continue
        if os.path.exists(dst):
            skipped.append((rel, "보관함에 같은 이름이 이미 있음"))
            continue
        if not dry:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.move(src, dst)
            except OSError as e:                     # 다른 프로그램이 열고 있는 파일(윈도우) 등
                skipped.append((rel, f"이동 실패: {e}"))
                continue
        moved.append((s, rel, size))
        total += size
    if not dry:                                       # 통째로 비워진 폴더(db_export 등)는 빈 폴더만 치움(파일 삭제 아님)
        for d in {os.path.dirname(src) for _, src in plan}:
            if d != HERE and os.path.isdir(d) and not os.listdir(d):
                try:
                    os.rmdir(d)
                except OSError:
                    pass
    if moved and not dry:
        man = os.path.join(dest_root, "MANIFEST.csv")
        new = not os.path.exists(man)
        with open(man, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["set", "original_relpath", "bytes", "moved_at"])
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for s, rel, size in moved:
                w.writerow([s, rel, size, ts])
    return moved, skipped, total, dest_root


def restore(dest_root):
    man = os.path.join(dest_root, "MANIFEST.csv")
    back, skip = 0, 0
    with open(man, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            src = os.path.join(dest_root, r["original_relpath"])
            dst = os.path.join(ROOT, r["original_relpath"])
            if not os.path.exists(src) or os.path.exists(dst):
                skip += 1
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            back += 1
    print(f"[restore] 복원 {back}개, 건너뜀 {skip}개(보관함에 없거나 원위치에 이미 있음)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default="snapshots,oneoff")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", default=None)
    a = ap.parse_args()
    if a.restore:
        return restore(a.restore)
    sets = [s.strip() for s in a.sets.split(",") if s.strip()]
    bad = [s for s in sets if s not in PICKERS]
    if bad:
        sys.exit(f"알 수 없는 대상: {bad} (가능: {list(PICKERS)})")
    moved, skipped, total, dest = archive(sets, dry=a.dry_run)
    by = {}
    for s, _, size in moved:
        n, b = by.get(s, (0, 0))
        by[s] = (n + 1, b + size)
    head = "[dry-run] 옮길 예정" if a.dry_run else f"[archive] → {dest}"
    print(f"{head}: {len(moved)}개, {total / 1e6:,.0f} MB")
    for s, (n, b) in by.items():
        print(f"  {s:10s} {n:4d}개 {b / 1e6:8,.0f} MB")
    for rel, why in skipped:
        print(f"  [skip] {rel} — {why}")


if __name__ == "__main__":
    main()
