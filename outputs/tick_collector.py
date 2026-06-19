"""
실시간 체결 틱(Tick) 데이터 수집 및 DB 적재 파이프라인 (자동 종료 패치 완료)
"""

import os
import json
import sqlite3
import asyncio
import requests
import websockets
from datetime import datetime
from config import KIS_APP_KEY, KIS_APP_SECRET, KIS_ENV, DB_DIR

if KIS_ENV == "prod":
    REST_BASE_URL = "https://openapi.koreainvestment.com:9443"
    WS_BASE_URL = "ws://ops.koreainvestment.com:21000"
else:
    REST_BASE_URL = "https://openapivts.koreainvestment.com:29443"
    WS_BASE_URL = "ws://ops.koreainvestment.com:31000"

DB_PATH = f"{DB_DIR}/ticks.db"

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tick_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            price INTEGER NOT NULL,
            volume INTEGER NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_date ON tick_data (code, date)")
    conn.commit()
    conn.close()

def get_approval_key():
    url = f"{REST_BASE_URL}/oauth2/Approval"
    headers = {"content-type": "application/json; utf-8"}
    body = {
        "grant_type": "client_credentials",
        "appkey": KIS_APP_KEY,
        "secretkey": KIS_APP_SECRET
    }
    res = requests.post(url, headers=headers, data=json.dumps(body))
    res.raise_for_status()
    return res.json().get("approval_key")

async def db_worker(queue):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    batch_data = []

    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=1.0)
            if item is None: # 독약(None)을 먹으면 안전하게 루프 종료
                break
            batch_data.append(item)
            queue.task_done()
        except asyncio.TimeoutError:
            pass

# tick_collector.py (수정할 부분: db_worker 함수 내부 루프)

        # 버퍼에 50개 이상 쌓였거나 타임아웃 시 저장 시도
        if len(batch_data) >= 50 or (len(batch_data) > 0 and queue.empty()):
            # [수정 코드] SQLite 쓰기 잠금 에러 발생 시 시스템 셧다운을 막는 예외 처리 추가
            try:
                cursor.executemany(
                    "INSERT INTO tick_data (code, date, time, price, volume) VALUES (?, ?, ?, ?, ?)",
                    batch_data
                )
                conn.commit()
                print(f"[DB] {len(batch_data)}건의 틱 데이터 적재 완료.")
                batch_data.clear() # 성공했을 때만 버퍼를 비움
            except sqlite3.OperationalError as e:
                # 에러가 나면 clear()를 하지 않으므로 큐 데이터가 소실되지 않고 다음 루프에서 재시도됨
                print(f"[DB 경고] DB가 사용 중입니다. 데이터를 보존하고 재시도합니다: {e}")



    # 남은 찌꺼기 털어넣기
    if batch_data:
        cursor.executemany(
            "INSERT INTO tick_data (code, date, time, price, volume) VALUES (?, ?, ?, ?, ?)",
            batch_data
        )
        conn.commit()
    conn.close()
    print("[DB] 저장 워커가 안전하게 종료되었습니다.")

async def connect_websocket(codes, queue):
    approval_key = get_approval_key()
    today_str = datetime.now().strftime("%Y%m%d")
    
    try:
        async with websockets.connect(WS_BASE_URL, ping_interval=None) as websocket:
            print(f"[웹소켓] 연결 성공. 대상 종목: {codes}")
            
            for code in codes:
                subscribe_msg = {
                    "header": {
                        "approval_key": approval_key,
                        "custtype": "P",
                        "tr_type": "1",
                        "content-type": "utf-8"
                    },
                    "body": {
                        "input": {
                            "tr_id": "H0STCNT0",
                            "tr_key": code
                        }
                    }
                }
                await websocket.send(json.dumps(subscribe_msg))
                await asyncio.sleep(0.1)
            
            while True:
                # [안전장치] 15:30이 되면 알아서 루프를 탈출
                now_time = datetime.now().strftime("%H%M")
                if now_time >= "1530":
                    print("[웹소켓] 15:30 장 마감 도달. 웹소켓 수신을 종료합니다.")
                    break
                
                try:
                    # [안전장치] 무한 대기 방지
                    data = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                    
                    if data.startswith('{'):
                        parsed = json.loads(data)
                        if parsed.get("header", {}).get("tr_id") == "PINGPONG":
                            await websocket.send(data)
                        continue
                    
                    parts = data.split('|')
                    if len(parts) >= 4 and parts[1] == "H0STCNT0":
                        trade_data = parts[3].split('^')
                        code = trade_data[0]
                        time_str = trade_data[1]
                        price = int(trade_data[2])
                        volume = int(trade_data[12])
                        
                        await queue.put((code, today_str, time_str, price, volume))
                        
                except asyncio.TimeoutError:
                    continue # 10초간 거래가 없으면 다시 확인
                except websockets.exceptions.ConnectionClosed:
                    print("[웹소켓] 연결 종료됨.")
                    break
                except Exception as e:
                    print(f"[오류] 데이터 파싱 중 에러: {e}")
                    break
    finally:
        # [안전장치] 웹소켓이 끊기거나 시간이 되면 db_worker에게 종료 신호를 던짐
        await queue.put(None)

async def main():
    TARGET_CODES = ["005930", "005380", "000660"] # 테스트용: 삼성전자, 현대차, SK하이닉스
    init_db()
    queue = asyncio.Queue()
    
    ws_task = asyncio.create_task(connect_websocket(TARGET_CODES, queue))
    db_task = asyncio.create_task(db_worker(queue))
    
    await asyncio.gather(ws_task, db_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[시스템] 강제 종료되었습니다.")