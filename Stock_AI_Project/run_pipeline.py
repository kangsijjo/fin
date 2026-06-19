import subprocess
import sys
import argparse
from src.logger import get_logger

logger = get_logger('pipeline')

def run_module(module_name, description):
    """지정된 파이썬 모듈을 실행하고 에러 발생 시 파이프라인을 중단합니다."""
    logger.info(f"\n{'='*60}\n▶ [STEP] {description} 시작\n{'='*60}")
    try:
        # 띄어쓰기를 기준으로 명령어와 인자를 분리 ("src.models.train all" -> ["src.models.train", "all"])
        args = module_name.split()
        cmd = [sys.executable, "-m"] + args
        
        subprocess.run(cmd, check=True)
        logger.info(f"▷ [STEP] {description} 완료\n")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ [STEP] {description} 실패! (에러 코드: {e.returncode})")
        logger.error("데이터 무결성을 위해 파이프라인 가동을 전면 중단합니다.")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="AI 퀀트 마스터 파이프라인")
    parser.add_argument(
        '--step',
        type=str,
        default='all',
        choices=['all', 'scan', 'collect', 'indicators', 'train', 'backtest', 'trade'],
        help="실행할 단계를 선택하세요 (기본값: all)"
    )
    parser.add_argument(
        '--market',
        type=str,
        default='korea',
        choices=['korea', 'usa'],
        help="scan step 대상 시장 (기본값: korea)"
    )
    args = parser.parse_args()

    logger.info(f"🚀 AI 퀀트 마스터 파이프라인 가동 (모드: {args.step})")

    steps = {
    'scan':       (f"src.collector.scanner {args.market}", "0. 주도 섹터 스캔"),
    'collect':    ("src.collector.main_collector",  "1. 주가 데이터 수집"),
    'indicators': ("src.processor.indicators all",  "2. 지표 생성 (전 섹터)"),
    'train':      ("src.models.train all",          "3. 전 섹터 모델 학습"),
    'backtest':   ("src.trader.backtest all",       "4. 백테스트 및 FinRL 데이터 생성"),
    'trade':      ("src.trader.rule_trader",        "5. 모의투자 매매 실행"),
}
 
    if args.step == 'all':
        for key, (module, desc) in steps.items():
            run_module(module, desc)
    else:
        module, desc = steps[args.step]
        run_module(module, desc)

    # 💡 [수술 완료] 전체 실행이거나 스캔 모드일 때만 1위 섹터를 출력하고, 나머지는 부분 완료 메시지 출력
    if args.step == 'all' or args.step == 'scan':
        from src.collector.config import ACTIVE_SECTOR
        logger.info(f"🎉 파이프라인 완료! (현재 최적 타겟 섹터: [{ACTIVE_SECTOR}])")
    else:
        logger.info(f"🎉 파이프라인 부분 작업({args.step})이 오차 없이 일괄 완료되었습니다!")

if __name__ == "__main__":
    main()