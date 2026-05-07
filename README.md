# Daily Stock Briefing

매일 07:00 KST 텔레그램으로 국내 증시 브리핑 자동 발송.

## 내용
1. KOSPI/KOSDAQ 1주 변동률
2. 시총 1,000억 이상 종목 중 1주 변동률 절대값 Top 10
3. 향후 전망 — 한경/매경 증권 RSS 최근 기사

## 구성
- `scripts/generate_stock_briefing.py` — pykrx로 지수·종목 변동, RSS로 전망
- `scripts/send_telegram.py` — 텔레그램 전송기 (방산 브리핑과 동일)
- `.github/workflows/daily-stock-briefing.yml` — GitHub Actions cron `0 22 * * *` UTC = 07:00 KST
- `.env` — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (커밋 금지)

## 로컬 실행
```powershell
pip install -r requirements.txt
python scripts/generate_stock_briefing.py
```
