import yaml
import os
from dotenv import load_dotenv

load_dotenv()

# YAML 설정 파일 로드
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'config.yaml'
)

with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
    _config = yaml.safe_load(f)

# 설정값 가져오기
ACTIVE_SECTOR   = _config['active_sector']
START_DATE      = _config['data']['start_date']
KOREA_SECTORS   = _config['korea_sectors']
USA_SECTORS     = _config['usa_sectors']
NEWS_WEIGHT = _config.get('news', {}).get('weight', 0.5)  # config.yaml news.weight 에서 로드

# 모델 설정
MODEL_CONFIG    = _config['model']
TRADING_CONFIG  = _config['trading']

# KRX API 키
KRX_API_KEY     = os.getenv('KRX_API_KEY')

def update_active_sector(sector):
    """YAML 파일에서 active_sector 변경"""
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    config['active_sector'] = sector
    
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
    
    print(f"섹터 변경 완료: {sector}")