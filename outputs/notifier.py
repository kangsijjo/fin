"""
notifier.py — 공용 텔레그램 알림 전송기. 2026-06-20 신설.

매매 체결·일일 요약 등 어디서든 import 해서 send("메시지") 호출.
설정: outputs/.telegram.json {"token": "...", "chat_id": "..."}
      또는 환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
설정이 없으면 (False, '미설정') 반환하고 조용히 스킵 — 어디서 불러도 절대 예외로 죽지 않음.
"""
import os
import json
import urllib.request
import urllib.parse

_BASE = os.path.dirname(os.path.abspath(__file__))


def _cfg():
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    cid = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (tok and cid):
        p = os.path.join(_BASE, ".telegram.json")
        if os.path.exists(p):
            try:
                d = json.load(open(p, encoding="utf-8"))
                tok = tok or d.get("token", "")
                cid = cid or d.get("chat_id", "")
            except Exception:
                pass
    return (tok or None), (cid or None)


def send(text):
    """텔레그램 전송. 반환 (성공bool, 메시지). 설정/네트워크 문제는 예외 대신 (False, 사유)."""
    tok, cid = _cfg()
    if not tok or not cid:
        return False, "텔레그램 미설정"
    try:
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": cid, "text": str(text)[:4000]}).encode()
        urllib.request.urlopen(url, data=data, timeout=10)
        return True, "전송됨"
    except Exception as e:
        return False, str(e)[:80]


def safe_send(text):
    """예외 무시 버전 — 매매 코드 등 '절대 죽으면 안 되는' 곳에서 호출."""
    try:
        return send(text)
    except Exception:
        return False, "오류"


# ── 매매 체결 묶음 알림 ──────────────────────────────────────────────────────────
# 주문마다 보내면 10슬롯 차는 날 알림이 쏟아지므로, 한 실행에서 모아 1통으로 보낸다.
_QUEUE = []


def queue_fill(side, name, code, qty, price=0):
    """체결을 큐에 쌓기만 한다(전송 X). side: 'buy' | 'sell'. 예외 안전."""
    try:
        _QUEUE.append((side, str(name), str(code), int(qty or 0), int(price or 0)))
    except Exception:
        pass


def flush_fills(prefix=""):
    """큐에 쌓인 체결을 매수/매도로 묶어 1통 전송. 큐 비었으면 아무것도 안 함."""
    try:
        if not _QUEUE:
            return False, "체결 없음"
        buys = [q for q in _QUEUE if q[0] == "buy"]
        sells = [q for q in _QUEUE if q[0] == "sell"]
        lines = []
        if prefix:
            lines.append(prefix)
        if buys:
            lines.append(f"🟢 매수 {len(buys)}건: " + ", ".join(f"{n}({q}주)" for _, n, c, q, p in buys))
        if sells:
            lines.append(f"🔴 매도 {len(sells)}건: " + ", ".join(f"{n}({q}주)" for _, n, c, q, p in sells))
        _QUEUE.clear()
        return send("\n".join(lines))
    except Exception as e:
        return False, str(e)[:60]


if __name__ == "__main__":
    print(send("notifier.py 테스트 메시지 — 연결 OK"))
