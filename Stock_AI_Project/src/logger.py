import logging
import os
import sys
from datetime import datetime

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# ── ANSI 색상 (Windows 10+ / 모던 터미널에서 동작) ──────────────────
_COLORS = {
    'DEBUG':    '\033[90m',    # 회색
    'INFO':     '\033[0m',     # 기본
    'WARNING':  '\033[33m',    # 노랑
    'ERROR':    '\033[91m',    # 밝은 빨강
    'CRITICAL': '\033[1;91m',  # 굵은 빨강
}
_RESET = '\033[0m'

# Windows 에서 ANSI 활성화
if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


class _ColorFormatter(logging.Formatter):
    """터미널 전용: 레벨별 색상 적용"""
    def format(self, record):
        color = _COLORS.get(record.levelname, '')
        msg = super().format(record)
        if color and record.levelname != 'INFO':
            return f"{color}{msg}{_RESET}"
        return msg


class _PlainFormatter(logging.Formatter):
    """파일 전용: ANSI 코드 없이 저장"""
    pass


def get_logger(name: str) -> logging.Logger:
    """레벨별 색상 + 날짜별 파일 저장 로거."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    fmt = '[%(asctime)s] [%(name)s] %(levelname)s: %(message)s'
    date_fmt = '%Y-%m-%d %H:%M:%S'

    # ── 파일 핸들러 (색상 없음, DEBUG 이상 전부) ──────────────────────
    log_file = os.path.join(LOG_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.log")
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_PlainFormatter(fmt, datefmt=date_fmt))
    logger.addHandler(fh)

    # ── 실패 전용 파일 핸들러 (ERROR 이상만 failures.log 에 누적) ─────
    fail_file = os.path.join(LOG_DIR, 'failures.log')
    efh = logging.FileHandler(fail_file, encoding='utf-8')
    efh.setLevel(logging.ERROR)
    efh.setFormatter(_PlainFormatter(fmt, datefmt=date_fmt))
    logger.addHandler(efh)

    # ── 콘솔 핸들러 (색상 있음, INFO 이상) ───────────────────────────
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.INFO)
    ch.setFormatter(_ColorFormatter(fmt, datefmt=date_fmt))
    logger.addHandler(ch)

    return logger
