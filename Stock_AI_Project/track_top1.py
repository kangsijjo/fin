"""
Top-1 paper-trade CLI.

사용 예
-------
    python track_top1.py record                # 오늘 한국 Top-1 기록
    python track_top1.py record --market usa   # 미국
    python track_top1.py settle                # 한국 pending 청산
    python track_top1.py settle --market usa
    python track_top1.py all                   # record + settle 한 번에 (한국)
    python track_top1.py report                # 누적 통계
"""
import argparse
import sys

from src.trader.top1_tracker import (
    record_today_top1, settle_pending, report
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('cmd', choices=['record', 'settle', 'all', 'report'])
    p.add_argument('--market', default='korea', choices=['korea', 'usa'])
    args = p.parse_args()

    if args.cmd == 'record':
        record_today_top1(market=args.market)
    elif args.cmd == 'settle':
        settle_pending(market=args.market)
    elif args.cmd == 'all':
        record_today_top1(market=args.market)
        settle_pending(market=args.market)
    elif args.cmd == 'report':
        report()


if __name__ == "__main__":
    main()
