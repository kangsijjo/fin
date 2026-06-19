import requests
from bs4 import BeautifulSoup
import pandas as pd
import sqlite3
import os
import time
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from dotenv import load_dotenv
from transformers import pipeline

load_dotenv()

# 보스의 기존 DB 경로 로직 유지
DB_PATH = os.path.join(os.path.dirname(__file__), '../../data/stock.db')
DART_API_KEY = os.getenv('DART_API_KEY')

def get_connection():
    return sqlite3.connect(DB_PATH)

# ==========================================
# 1. FinBERT 하이브리드 감성 분석 로직
# ==========================================
print("FinBERT 모델 로딩 중...")
kr_sentiment = pipeline("sentiment-analysis", model="snunlp/KR-FinBert-SC", truncation=True, max_length=512)
en_sentiment = pipeline("sentiment-analysis", model="ProsusAI/finbert", truncation=True, max_length=512)
print("FinBERT 모델 로딩 완료!")

def is_korean(text):
    korean_chars = sum(1 for c in text if '\uAC00' <= c <= '\uD7A3')
    return korean_chars > len(text) * 0.2

def analyze_sentiment(texts, sources):
    """FinBERT + DART 전용 룰베이스 감성분석 (업데이트)"""
    dart_rules = {
        '유상증자결정': -0.8, '감자결정': -1.0, '상장폐지': -1.0, '영업정지': -1.0, 
        '전환사채권발행결정': -0.5, '무상증자결정': 0.8, '단일판매ㆍ공급계약체결': 0.8, 
        '주식소각결정': 0.9, '자기주식취득결정': 0.7, '소유상황보고서': 0.0, '기업설명회': 0.0
    }
    
    scores = []
    for i, text in enumerate(texts):
        source = sources[i]
        rule_matched = False
        
        if source == 'DART':
            clean_text = text.replace(' ', '')
            for keyword, score in dart_rules.items():
                if keyword in clean_text:
                    scores.append(score)
                    rule_matched = True
                    break
                    
        if rule_matched: continue
            
        try:
            model = kr_sentiment if is_korean(text) else en_sentiment
            result = model(text)[0]
            scores.append(result['score'] if result['label'] == 'positive' else -result['score'] if result['label'] == 'negative' else 0.0)
        except:
            scores.append(0.0)
    return scores

# ==========================================
# 2. DART 고유번호 맵핑 (0개 수집 해결 로직)
# ==========================================
DART_CORP_CODES = {}

def load_dart_corp_codes():
    """DART XML 파일을 다운받아 메모리에 적재"""
    global DART_CORP_CODES
    if DART_CORP_CODES: return
        
    print("DART 고유번호 맵핑 파일 다운로드 중...")
    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    try:
        res = requests.get(url, params={'crtfc_key': DART_API_KEY}, timeout=10)
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            with z.open('CORPCODE.xml') as f:
                tree = ET.parse(f)
                for list_node in tree.getroot().findall('list'):
                    corp_code = list_node.find('corp_code').text
                    stock_code = list_node.find('stock_code')
                    if stock_code is not None and stock_code.text and len(stock_code.text.strip()) == 6:
                        DART_CORP_CODES[stock_code.text.strip()] = corp_code
        print(f"DART 맵핑 완료 (총 {len(DART_CORP_CODES)}개 기업)")
    except Exception as e:
        print(f"DART 맵핑 실패: {e}")

def get_corp_code(ticker):
    load_dart_corp_codes()
    return DART_CORP_CODES.get(ticker)

# ==========================================
# 3. 데이터 수집 및 저장 파이프라인
# ==========================================
def fetch_dart_historical(ticker, name, start_date='20150101'):
    """과거 공시 10년치 대량 수집 (업데이트)"""
    corp_code = get_corp_code(ticker)
    if not corp_code: return []
    
    news_list = []
    page = 1
    while True:
        url = "https://opendart.fss.or.kr/api/list.json"
        params = {'crtfc_key': DART_API_KEY, 'corp_code': corp_code, 'bgn_de': start_date, 'page_no': page, 'page_count': 100}
        try:
            res = requests.get(url, params=params, timeout=10).json()
            if res.get('status') != '000' or not res.get('list'): break
            
            for item in res['list']:
                news_list.append({
                    'ticker': ticker, 'name': name, 'title': item.get('report_nm', ''),
                    'link': f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item.get('rcept_no')}",
                    'pubDate': item.get('rcept_dt', ''),
                    'collected_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'source': 'DART' # 새 규격
                })
            if page >= int(res.get('total_page', 1)): break
            page += 1
            time.sleep(0.3)
        except:
            break
    return news_list

def save_news(news_list):
    """DB 저장 (업데이트: sentiment 및 source 반영)"""
    if not news_list: return
    df = pd.DataFrame(news_list)
    print(f"  감성분석 중 ({len(df)}개)...")
    df['sentiment'] = analyze_sentiment(df['title'].tolist(), df['source'].tolist())
    
    conn = get_connection()
    df.to_sql('news', conn, if_exists='append', index=False)
    conn.close()
    print(f"  {len(df)}개 저장완료")

def get_sector_tickers(sector):
    """기존 보스 로직 유지"""
    conn = get_connection()
    try:
        df = pd.read_sql("SELECT DISTINCT ticker, name FROM korea_stocks WHERE sector=?", conn, params=[sector])
        conn.close()
        if len(df) > 0: return df[['ticker', 'name']].values.tolist(), 'korea'
    except:
        conn.close()
    return [], None

def collect_historical_news(sector=None):
    from src.collector.config import ACTIVE_SECTOR, KOREA_SECTORS
    
    # 전체 섹터 처리
    if sector == 'all':
        sectors = KOREA_SECTORS
    else:
        sectors = [sector if sector else ACTIVE_SECTOR]
    
    print(f"\n=== 총 {len(sectors)}개 섹터 10년치 DART 공시 수집 시작 ===")
    
    for s in sectors:
        print(f"\n=== [{s}] 섹터 공시 수집 시작 ===")
        tickers, market = get_sector_tickers(s)
        
        if not tickers:
            print(f"  {s} 종목 없음, 스킵")
            continue
            
        if market != 'korea':
            print(f"  {s} 미국 섹터, 스킵")
            continue
        
        for i, (ticker, name) in enumerate(tickers):
            print(f"  [{i+1}/{len(tickers)}] {name}({ticker}) 수집 중...")
            news = fetch_dart_historical(ticker, name)
            if news:
                save_news(news)
        
        print(f"=== [{s}] 완료 ===")
    
    print(f"\n전체 {len(sectors)}개 섹터 공시 수집 완료!")

if __name__ == "__main__":
    import sys
    # 인자로 historical 반도체 등을 주면 해당 섹터 과거 데이터 싹쓸이
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'historical'
    sec = sys.argv[2] if len(sys.argv) > 2 else None
    
    if cmd == 'historical':
        collect_historical_news(sec)
    elif cmd == 'realtime':
        # 30분 주기 호출: 오늘 날짜 기준 최신 공시 수집 (INSERT OR IGNORE로 중복 안전)
        collect_historical_news(sec)