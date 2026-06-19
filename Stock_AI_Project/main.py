import os
import sys
import sqlite3
import subprocess
import pandas as pd

DB_PATH = 'data/stock.db'

def get_available_sectors():
    """DB에서 사용 가능한 섹터 목록 가져오기"""
    conn = sqlite3.connect(DB_PATH)

    korea_sectors = pd.read_sql(
        """SELECT DISTINCT sector, COUNT(DISTINCT ticker) as 종목수
           FROM korea_stocks
           WHERE sector IS NOT NULL
           GROUP BY sector
           ORDER BY 종목수 DESC""",
        conn
    )
    usa_sectors = pd.read_sql(
        """SELECT DISTINCT sector, COUNT(DISTINCT ticker) as 종목수
           FROM usa_stocks
           WHERE sector IS NOT NULL
           GROUP BY sector
           ORDER BY 종목수 DESC""",
        conn
    )
    conn.close()
    return korea_sectors, usa_sectors

def update_config(sector):
    """config.yaml ACTIVE_SECTOR 자동 변경"""
    from src.collector.config import update_active_sector
    update_active_sector(sector)

def run_pipeline(sector, market):
    """지표생성 → 모델학습 → 백테스트 자동 실행"""
    print(f"\n{'='*50}")
    print(f"섹터: {sector} / 시장: {market}")
    print(f"{'='*50}")

    # 1. config 업데이트
    update_config(sector)

    # 2. 지표 생성 (새 파이프라인)
    print("\n[1/3] 지표 생성 중...")
    subprocess.run([sys.executable, "-m", "src.processor.indicators"], check=False)

    # 3. 모델 학습
    print("\n[2/3] 모델 학습 중...")
    subprocess.run([sys.executable, "-m", "src.models.train"], check=False)

    # 4. 백테스트
    print("\n[3/3] 백테스트 실행 중...")
    subprocess.run([sys.executable, "-m", "src.trader.backtest"], check=False)

def main():
    print("\n" + "="*50)
    print("      Stock AI - 섹터 분석 시스템")
    print("="*50)

    korea_sectors, usa_sectors = get_available_sectors()

    all_sectors = []

    print("\n[한국 섹터]")
    for _, row in korea_sectors.iterrows():
        all_sectors.append((row['sector'], 'korea'))
        print(f"  {len(all_sectors)}. {row['sector']} ({row['종목수']}개 종목)")

    print("\n[미국 섹터]")
    for _, row in usa_sectors.iterrows():
        all_sectors.append((row['sector'], 'usa'))
        print(f"  {len(all_sectors)}. {row['sector']} ({row['종목수']}개 종목)")

    print("\n  0. 종료")

    while True:
        try:
            choice = int(input("\n섹터 번호 선택: "))
            if choice == 0:
                print("종료합니다.")
                break
            elif 1 <= choice <= len(all_sectors):
                sector, market = all_sectors[choice - 1]
                run_pipeline(sector, market)
                break
            else:
                print("올바른 번호를 입력해주세요.")
        except ValueError:
            print("숫자를 입력해주세요.")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == 'all':
        # 전체 섹터 자동 실행
        print("\n전체 섹터 학습 시작!")
        korea_sectors_list = [
            '반도체', '방산', '조선', '2차전지', '바이오',
            '엔터', '제약', '자동차', '게임', '소프트웨어'
        ]
        usa_sectors_list = [
            'Information Technology', 'Industrials',
            'Health Care', 'Financials', 'Energy'
        ]

        for sector in korea_sectors_list:
            update_config(sector)
            print(f"\n{'='*50}")
            print(f"[한국] {sector} 섹터 처리 중...")
            subprocess.run([sys.executable, "-m", "src.processor.indicators"], check=False)
            subprocess.run([sys.executable, "-m", "src.models.train"], check=False)

        for sector in usa_sectors_list:
            update_config(sector)
            print(f"\n{'='*50}")
            print(f"[미국] {sector} 섹터 처리 중...")
            subprocess.run([sys.executable, "-m", "src.processor.indicators"], check=False)
            subprocess.run([sys.executable, "-m", "src.models.train"], check=False)

        print("\n전체 섹터 학습 완료!")

    elif len(sys.argv) > 1 and sys.argv[1] == 'scan':
        # 스캐너 실행 후 자동 파이프라인
        from src.collector.scanner import scan_best_sector
        from src.collector.config import USA_SECTORS

        print("\n시장 주도 섹터 스캔 중...")
        best = scan_best_sector(market='korea')

        if best:
            market = 'usa' if best in USA_SECTORS else 'korea'
            run_pipeline(best, market)
        else:
            print("scan failed")

    else:
        main()
