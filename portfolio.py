import streamlit as st
import pandas as pd
import FinanceDataReader as fdr
import yfinance as yf
from datetime import datetime, timedelta
from streamlit_option_menu import option_menu
from streamlit_gsheets import GSheetsConnection
from textwrap import dedent

# --- 기본 설정 ---
ACCOUNT_NAMES = ["ISA", "Pension", "IRP", "ETF", "US", "사주", "LV"]

# --- 엑셀 파일 경로 설정 ---
conn = st.connection("gsheets", type=GSheetsConnection)

# --- 데이터 불러오기 ---
try:
    # 입출금 시트
    cash_df = conn.read(worksheet="입출금")
    cash_df.columns = cash_df.columns.str.strip()
    cash_df["거래일"] = pd.to_datetime(cash_df["거래일"])

    # WRAP 시트에서 환율(O열, 첫 번째 행) 읽기
    exchange_rate_df = conn.read(worksheet="WRAP", usecols=[14], nrows=1, header=None)
    exchange_rate = float(exchange_rate_df.iloc[0, 0]) if not exchange_rate_df.empty else 1400

    # 각 계좌 시트 불러오기
    TRADE_SHEET_NAMES = [name for name in ACCOUNT_NAMES if name not in ["LV"]]

    trade_dfs = {
        acct: conn.read(worksheet=acct)
        for acct in TRADE_SHEET_NAMES
    }
    for acct, df in trade_dfs.items():
        df.columns = df.columns.str.strip()

        # ISA, Pension, 사주만 종목코드 특별 처리
        if acct in ["ISA", "Pension", "사주"]:
            df['종목코드'] = df['종목코드'].astype(str).str.split('.').str[0].str.zfill(6)

        df["거래일"] = pd.to_datetime(df["거래일"])
        df["제세금"] = pd.to_numeric(df["제세금"], errors="coerce").fillna(0)
        df["단가"] = pd.to_numeric(df["단가"], errors="coerce").fillna(0)
        df["수량"] = pd.to_numeric(df["수량"], errors="coerce").fillna(0)
        df["거래금액"] = pd.to_numeric(df["거래금액"], errors="coerce").fillna(0)

        # 유형 열이 있는 경우에만 처리
        if "유형" in df.columns:
            df["유형"] = df["유형"].fillna("미분류")
        else:
            df["유형"] = "미분류"

    # 배당 시트 불러오기
    df_dividend = conn.read(worksheet="배당")
    df_dividend.columns = df_dividend.columns.str.strip()
    df_dividend["배당금"] = pd.to_numeric(df_dividend["배당금"], errors="coerce").fillna(0).astype(int)

    # WRAP 시트에서 K1(원금), M1(평가액) 셀 읽기
    wrap_capital_df = conn.read(worksheet="WRAP", usecols=[10], nrows=1, header=None)
    wrap_capital_usd = float(wrap_capital_df.iloc[0, 0]) if not wrap_capital_df.empty else 0

    wrap_value_df = conn.read(worksheet="WRAP", usecols=[12], nrows=1, header=None)
    wrap_value_usd = float(wrap_value_df.iloc[0, 0]) if not wrap_value_df.empty else 0

    # 원화 환산
    wrap_capital = wrap_capital_usd * exchange_rate
    wrap_value = wrap_value_usd * exchange_rate

except Exception as e:
    st.error(f"엑셀 파일을 읽는 중 오류 발생: {e}")
    st.stop()

# --- 계산 함수 정의 ---
@st.cache_data(ttl=300)
def get_price_data(code: str, source: str = "fdr"):
    if source == "fdr":
        return fdr.DataReader(code)
    else:
        return yf.download(code, period="5d")


def calculate_account_summary(df_trade, df_cash, df_dividend, is_us_stock=False):
    summary_list = []
    realized_total = 0
    today_profit = 0

    for code, group in df_trade.groupby("종목코드"):
        group = group.sort_values("거래일").copy()
        name = group["종목명"].iloc[0]
        asset_type = group["유형"].iloc[0]

        avg_price = 0
        hold_qty = 0
        realized_profit = 0

        for _, row in group.iterrows():
            qty = row["수량"]
            price = row["단가"]
            fee = row["제세금"]
            amt = row["거래금액"]

            if row["구분"] == "매수":
                total_cost = avg_price * hold_qty + amt + fee
                hold_qty += qty
                avg_price = total_cost / hold_qty if hold_qty != 0 else 0
            else:
                profit = (price - avg_price) * qty - fee
                realized_profit += profit
                hold_qty -= qty

        if hold_qty > 0:
            try:
                if str(code) == "펀드":
                    current_price = group["현재가"].dropna().iloc[-1] if "현재가" in group.columns else 0
                    prev_close = current_price
                else:
                    try:
                        price_data = get_price_data(str(code), source="fdr")
                        current_price = price_data.iloc[-1]["Close"]
                        prev_close = price_data.iloc[-2]["Close"]
                    except:
                        current_price = 0
                        prev_close = 0
            except:
                current_price = 0
                prev_close = 0

            current_value = current_price * hold_qty
            buy_cost = avg_price * hold_qty
            profit = current_value - buy_cost
            profit_rate = profit / buy_cost * 100 if buy_cost else 0
            today_profit += (current_price - prev_close) * hold_qty 

            summary_list.append({
                "종목코드": code,
                "종목명": name,
                "유형": asset_type,
                "보유수량": hold_qty,
                "평균단가": round(avg_price),
                "현재가": round(current_price),
                "평가금액": round(current_value),
                "매입금액": round(buy_cost),
                "평가손익": round(profit),
                "수익률(%)": round(profit_rate, 2)
            })

        realized_total += realized_profit

    df_summary = pd.DataFrame(summary_list)

    # 배당금 계산 - NaN 처리 추가
    dividend_total = 0
    if not df_trade.empty and "계좌명" in df_trade.columns:
        account_names = df_trade["계좌명"].unique()
        dividend_sum = df_dividend[df_dividend["계좌명"].isin(account_names)]["배당금"].sum()
        dividend_total = dividend_sum if pd.notna(dividend_sum) else 0

    # 빈 DataFrame 처리
    if df_summary.empty:
        current_value = 0
        current_profit = 0
    else:
        current_value = df_summary["평가금액"].sum()
        current_profit = df_summary["평가손익"].sum()

    # 계산된 값들의 NaN 처리
    capital = (df_cash[df_cash["구분"] == "입금"]["금액"].sum() - df_cash[df_cash["구분"] == "출금"]["금액"].sum())
    capital = capital if pd.notna(capital) else 0
    
    actual_profit = realized_total + dividend_total
    actual_profit = actual_profit if pd.notna(actual_profit) else 0
    
    total_balance = capital + current_profit + actual_profit
    cash = total_balance - current_value
    total_profit_rate = (total_balance - capital) / capital * 100 if capital else 0

    # NaN 체크를 추가한 summary 딕셔너리
    summary = {
        "capital": round(capital) if pd.notna(capital) else 0,
        "current_value": round(current_value) if pd.notna(current_value) else 0,
        "current_profit": round(current_profit) if pd.notna(current_profit) else 0,
        "actual_profit": round(actual_profit) if pd.notna(actual_profit) else 0,
        "total_balance": round(total_balance) if pd.notna(total_balance) else 0,
        "cash": round(cash) if pd.notna(cash) else 0,
        "total_profit": round(current_profit + actual_profit + dividend_total) if pd.notna(current_profit + actual_profit + dividend_total) else 0,
        "total_profit_rate": round(total_profit_rate, 2) if pd.notna(total_profit_rate) else 0,
        "today_profit": round(today_profit) if pd.notna(today_profit) else 0
    }

    return df_summary, summary

# --- Streamlit 구성시작 ---
st.set_page_config(layout="wide")

# --- 스타일 정의 ---
st.markdown("""
<link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css" rel="stylesheet">

<style>
html, body, .stApp, * {
    font-family: 'Pretendard', sans-serif !important;
}
               
.block-container {
    max-width: 1200px !important;
    padding-top: 3rem !important;
    padding-left: 2rem;
    padding-right: 2rem;
    margin: auto;
    background-color: #F5F5F5;
    font-size: 28px;
}           
.card {
    background-color: white;
    border-radius: 16px;
    padding: 28px;
    box-shadow: 0 4px 8px rgba(0,0,0,0.08);
    margin-bottom: 20px;
    margin-right: 10px;
}
.card-title {
    font-size: 24px;
    font-weight: 600;
    color: #444;
}
.card-value {
    font-size: 32px;
    font-weight: bold;
    color: black;
}
            
.card-item { 
    background: #EDEDE9;
    border-radius: 12px;
    padding: 16px 20px;
    margin-top: 12px;
    margin-bottom: 16px;
}            
.item-label {font-weight:bold; font-size: 20px; color: #333;}
.item-return {font-weight:bold; font-size: 24px;}           
            
.stock-item {
    background: transparent;
    border: none; 
    border-bottom: 1.5px solid #ddd; 
    padding: 10px 15px;
    margin-bottom: 10px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.stock-label {font-weight:bold; font-size:16px; color=#555}
.stock-return { font-weight: bold; color: #2E7850; font-size: 20px;}
.stock-value { font-weight: 555; color: #555; font-size: 20px;}           
.stock-return-ratio {
    font-size: 14px;
    color: #5BA17B;
    font-weight: normal;
}
            
.badge {
    background: #3A866A;
    color: white;
    font-weight: bold;
    font-size: 16px;
    border-radius: 12px;
    padding: 4px 15px;
    display: inline-block;
    margin-top: 8px;
}
.custom-divider {
    height: 1px;
    background-color: #eee;   
    border: none;             
    margin: 20px 0;
}
            
/* 성과 탭 전용 CSS */
.total-value-card {
    background: #778AD5;
    color: white;
    border-radius: 16px;
    padding: 28px;
    box-shadow: 0 4px 8px rgba(0,0,0,0.08);
    margin-bottom: 20px;
}
.total-value-title { font-size: 20px; font-weight: 500; opacity: 0.95; margin-bottom: 12px; }
.total-value-amount { font-size: 36px; font-weight: 700; margin-bottom: 24px; }
.value-divider { height: 1px; background-color: rgba(255, 255, 255, 0.3); margin: 24px 0; }
.profit-section { 
    display: flex; 
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
}
.profit-label { font-size: 20px; font-weight: 500; opacity: 0.9; }
.profit-row {
    display: flex;
    align-items: center;
    gap: 16px;
}
.profit-amount { font-size: 32px; font-weight: 700; }
.profit-badge { 
    background-color: rgba(255, 255, 255, 0.25); 
    color: white; 
    font-size: 18px; 
    font-weight: 700; 
    padding: 6px 16px; 
    border-radius: 20px; 
}

.gauge-container { position: relative; width: 200px; height: 120px; }
.gauge { width: 240px; height: 120px; border-radius: 240px 240px 0 0; position: relative; }
.gauge::after { content: ''; position: absolute; width: 170px; height: 85px; background-color: white; border-radius: 170px 170px 0 0; bottom: 0; left: 35px; }
.gauge-value { position: absolute; bottom: 15px; left: 62%; transform: translateX(-50%); font-size: 28px; font-weight: 700; color: #0F2F76; z-index: 10; }
.gauge-label { position: absolute; bottom: 5px; left: 62%; transform: translateX(-50%); font-size: 14px; font-weight: 600; color: #0F2F76; z-index: 10; }

.allocation-section { display: flex; flex-direction: column; align-items: center; gap: 20px; margin-bottom: 32px; margin-top: 24px; }
.allocation-donut { position: relative; width: 180px; height: 180px; border-radius: 50%; }
.allocation-donut::after { content: ''; position: absolute; width: 120px; height: 120px; background-color: white; border-radius: 50%; top: 30px; left: 30px; }
.allocation-donut-value { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); font-size: 24px; font-weight: 700; color: #0f2f76; z-index: 10; }
.allocation-donut-label { position: absolute; top: 60%; left: 50%; transform: translate(-50%, 0); font-size: 14px; font-weight: 500; color: #0f2f76; z-index: 10; }
            
.section-divider { height: 1px; background-color: #E8EEF5; margin: 24px 0; }

.country-allocation { display: flex; justify-content: space-around; align-items: center; padding-top: 12px; margin-top: 24px; margin-bottom: 24px;}
.country-item { display: flex; flex-direction: column; align-items: center; gap: 12px; }
.country-name { font-size: 16px; font-weight: 600; color: #2C3E50; }
.country-amount { font-size: 16px; font-weight: 700; color: #666; }

.strategy-donut-container { display: flex; justify-content: center; padding: 20px 0; }
.strategy-donut { position: relative; width: 240px; height: 240px; border-radius: 50%; }
.strategy-donut::after { content: ''; position: absolute; width: 160px; height: 160px; background-color: white; border-radius: 50%; top: 40px; left: 40px; }
.strategy-list { display: flex; flex-direction: column; gap: 12px; }
.strategy-item { border: 3px solid; border-radius: 16px; padding: 20px 24px; display: flex; justify-content: space-between; align-items: center; transition: all 0.3s ease; }
.strategy-item:hover { transform: translateY(-2px); }
.strategy-name { font-size: 16px; font-weight: 500; color: #0f2f76; }
.strategy-values { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
.strategy-amount { font-size: 28px; font-weight: 700; }
.strategy-profit { font-size: 16px; font-weight: 600; opacity: 0.8; }            

</style>
""", unsafe_allow_html=True)

# --- Streamlit 탭 구성 ---
st.markdown("""
<div style='font-size:32px; font-weight:bold; margin-bottom:16px;'>Dashboard</div>
""", unsafe_allow_html=True)

# --- 커스텀 탭 디자인  ---
ACCOUNT_NAMES = ["성과", "전체", "ISA", "Pension", "IRP", "ETF", "US"]
green_color = "#3A866A"
red_color = "#C54E4A"

# 옵션 메뉴 사용
selected_tab = option_menu(
    menu_title=None,
    options=ACCOUNT_NAMES,
    icons=["back", "back", "geo-alt-fill","geo-alt-fill","geo-alt-fill","geo-alt-fill","geo-alt-fill"],
    orientation="horizontal",
    styles={
        "container": {"padding": "0!important", "background-color": "#E0E0E0"},
        "nav-link": {
            "font-family": "'Pretendard', sans-serif",
            "font-size": "20px",
            "font-weight": "700",
            "color": "#444",
            "padding": "10px 24px",
            "border-radius": "12px",
        },
        "nav-link-selected": {
            "background-color": green_color,
            "color": "white",
        },
    }
)

ACCOUNT_COLORS = {
    "Overview": "#EDE5D9",
    "ISA": "#B9CCD9",      # 블루그레이
    "Pension": "#F6CD7D",  # 머스타드
    "IRP": "#C8D9A2",      # 올리브
    "ETF": "#F6C793",      # 살구
    "전체": "#EDE5D9",      # 기본 회색
    "US": "#F7B7A3",
}

theme_color = ACCOUNT_COLORS.get(selected_tab, "#EDEDE9")

# 선택된 계좌에 따라 데이터 처리
acct = selected_tab
currency_symbol = "$ " if selected_tab == "US" else ""
        
# 계좌별 데이터 불러오기
# --- 항상 실행되는 영역
local_accounts = ["ISA", "Pension", "IRP", "ETF"]
local_total_summary = {
    "capital": 0,
    "current_value": 0,
    "current_profit": 0,
    "actual_profit": 0,
    "total_balance": 0,
    "cash": 0,
    "today_profit": 0,
}
df_summary_list = []

for acct_name in local_accounts:
    df_trade = trade_dfs[acct_name]
    df_cash = cash_df[cash_df["계좌명"] == acct_name]
    df_s, s = calculate_account_summary(df_trade, df_cash, df_dividend)
    df_summary_list.append(df_s)
    for key in local_total_summary:
        local_total_summary[key] += s[key]

local_total_summary["total_profit_rate"] = (
    (local_total_summary["total_balance"] - local_total_summary["capital"]) / local_total_summary["capital"] * 100
    if local_total_summary["capital"] else 0
)

local_summary = {k: round(v) if k != "total_profit_rate" else round(v, 2) for k, v in local_total_summary.items()}
local_total_summary["total_profit"] = local_total_summary["current_profit"] + local_total_summary["actual_profit"]

# 탭 선택에 따른 데이터 분기 설정
if acct == "전체":
    df_summary = pd.concat(df_summary_list, ignore_index=True)
    summary = local_summary

elif acct == "성과":
    df_summary = pd.DataFrame()
    summary = local_summary

else:
    df_trade = trade_dfs[acct]
    df_cash = cash_df[cash_df["계좌명"] == acct]
    df_summary, summary = calculate_account_summary(df_trade, df_cash, df_dividend)


# summary 값 사용
total_profit = summary["current_profit"] + summary["actual_profit"]
total_profit_rate = summary["total_profit_rate"]
today_profit = summary["today_profit"] 

current_profit = summary["current_profit"]

# 빈 DataFrame 체크 추가
if df_summary.empty:
    buy_cost_total = 0
    current_profit_rate = 0
else:
    buy_cost_total = df_summary["매입금액"].sum()
    current_profit_rate = current_profit / buy_cost_total * 100 if buy_cost_total > 0 else 0

actual_profit = summary["actual_profit"]
actual_profit_rate = actual_profit / summary["capital"] * 100 if summary["capital"] else 0

current_value = summary["current_value"]
cash = summary["cash"]
capital = summary["capital"]
operated_ratio = current_value / summary["total_balance"] * 100 if summary["total_balance"] != 0 else 0
cash_ratio = cash / summary["total_balance"] * 100 if summary["total_balance"] != 0 else 0
capital_ratio = capital / summary["total_balance"] * 100 if summary["total_balance"] != 0 else 0

# --- 레이아웃 시작 ---
    
# 1. Profit 카드
icon_book = "https://cdn-icons-png.flaticon.com/128/16542/16542648.png"
icon_wallet = "https://cdn-icons-png.flaticon.com/128/19011/19011999.png"

card_html_profit = f"""
<div class="card">
    <div class="card-title"><span style= "color: {theme_color}";>●</span><span style="margin-left: 6px;">Total Profit</span></div>
    <div class="card-value">{currency_symbol}{total_profit:,.0f}</div>
    <div class="badge">+{total_profit_rate:.2f}%</div>
    <div style="display:flex; justify-content:space-between; margin-top: 15px; ">
        <div class="card-item" style="width: 47%; background: {theme_color};">
            <div style="display: flex; align-items: center; gap: 6px;">
                <img src="{icon_book}" width="20" height="20" />          
                <span class="item-label" >Current</span>
            </div>
            <div class="item-return" >{currency_symbol}{current_profit:,.0f}</div>
            <div class="badge">+{current_profit_rate:.2f}%</div>
        </div>
        <div class="card-item" style="width: 47%;">
            <div style="display: flex; align-items: center; gap: 6px;">
                <img src="{icon_wallet}" width="20" height="20" />
                <span class="item-label">Actual</span>
            </div>
            <div class="item-return">{currency_symbol}{actual_profit:,.0f}</div>
            <div class="badge">+{actual_profit_rate:.2f}%</div>
        </div>
    </div>
</div>
"""

# 2. --- balance 카드 ---
def get_bar(percent, color="#2E7850"):
    return f"<div style='width:100%; background:#F5F5F5; height:20px; border-radius:5px; margin-top:8px; margin-bottom:12px;'><div style='width:{percent:.1f}%; background:{color}; height:20px; border-radius:5px;'></div></div>"

current_year = datetime.now().year

LIMITS = {
    "ISA": 60000000,
    "Pension": 3000000,
    "IRP": 7200000
}

# 납입액 계산
if acct == "ISA":
    # 전체 기간 누적 입금액
    deposit_df = cash_df[
        (cash_df["계좌명"] == acct) &
        (cash_df["구분"] == "입금")
    ]
else:
    # 올해 입금액
    deposit_df = cash_df[
        (cash_df["계좌명"] == acct) &
        (cash_df["구분"] == "입금") &
        (cash_df["거래일"].dt.year == current_year)
    ]
    
limit = LIMITS.get(acct, 0)
paid_amount = deposit_df["금액"].sum()
remaining_amount = max(limit - paid_amount, 0)
paid_ratio = (paid_amount / limit) * 100 if limit > 0 else 0

bar1 = get_bar(operated_ratio, color=theme_color)

# 조건부 추가 HTML
if acct in ["ISA", "Pension", "IRP"] and limit > 0:
    bar2_html = get_bar(paid_ratio, color=theme_color)
    limit_html = f"""
        <div class="custom-divider"></div>
        <div style="display:flex; justify-content:space-between; align-items:center; font-weight:555; font-size:18px; color:#555; margin-top:8px;">
            <div style="margin-left:5px;">Limit</div>
            <div style="margin-right:5px;">{limit:,.0f}</div>
        </div>
        {bar2_html}
        <div style="display:flex; justify-content:space-between; font-size:14px; margin-top:-8px; margin-bottom:8px;">
            <div style="color:#555; font-weight:600; margin-left:5px;">{paid_amount:,.0f}</div>
            <div style="color:#555; margin-right:5px;">{remaining_amount:,.0f}</div>
        </div>
    """.strip()
else:
    limit_html = "<div style='height:0;'></div>"

icon_capital = "https://cdn-icons-png.flaticon.com/128/7928/7928113.png"
icon_cash = "https://cdn-icons-png.flaticon.com/128/13794/13794238.png"

# 항상 완성된 HTML 구조 유지
card_html_balance = f"""
<div class="card">
    <div class="card-title"><span style="color:{theme_color};">●</span><span style="margin-left:6px;">Balance</span></div>
    <div class="card-value">{currency_symbol}{summary['total_balance']:,.0f}</div>
        <div style="display:flex; justify-content:space-between;">
            <div class="card-item" style="width:47%;">
                <div style="display:flex; align-items:center; gap:6px;">
                <img src="{icon_capital}" width="20" height="20" />
                <span class="item-label">Invested</span>
                </div>
                <div class="item-return">{currency_symbol}{capital:,.0f}</div>
            </div>
            <div class="card-item" style="width:47%;">
                <div style="display:flex; align-items:center; gap:6px;">
                <img src="{icon_cash}" width="20" height="20" />
                <span class="item-label">Profit</span>
                </div>
                <div class="item-return">{currency_symbol}{total_profit:,.0f}</div>
            </div>
        </div>
        <div style="display:flex; justify-content:space-between; align-items:center; font-weight:555; font-size:18px; color:#555; margin-top:8px;">
            <div style="margin-left:5px;">Operated</div>
            <div style="margin-right:5px;">{currency_symbol}{current_value:,.0f}</div>
        </div>
        <div style="display:flex; justify-content:space-between; align-items:center; font-weight:555; font-size:18px; color:#555;">
            <div style="margin-left:5px;">Cash</div>
            <div style="margin-right:5px;">{currency_symbol}{cash:,.0f}</div>
        </div>
        {bar1}
        <div style="display:flex; justify-content:space-between; font-size:14px; margin-top:-8px; margin-bottom:8px;">
            <div style="color:#555; font-weight:600; margin-left:5px;">{operated_ratio:.0f}%</div>
            <div style="color:#555; margin-right:5px;">{cash_ratio:.0f}%</div>
        </div>
        {limit_html}
</div>
""".strip()
    
# 3. --- Holdings 카드 ---
icon_today = "https://cdn-icons-png.flaticon.com/128/876/876754.png"
icon_total = "https://cdn-icons-png.flaticon.com/128/13110/13110858.png"

today_profit_plus = f"{today_profit:,.0f}" if today_profit > 0 else "&nbsp;"

# 상단 수익 요약 카드
card_html_stock = dedent(f"""
<div class="card">
    <div class="card-title"><span style= "color: {theme_color}";>●</span><span style="margin-left: 6px;">Holdings</span></div>
    <div class="card-value" style="display: flex; justify-content: space-between; align-items: center;">
        <div>{currency_symbol}{current_value:,.0f}</div>
    </div>
    <div class="card-item" style="padding: 5px 15px; background: #EDEDE9;">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 10px; margin-bottom: 10px;">
        <div style="line-height: 24px;">
            <img src="{icon_today}" width="20" height="20" style="vertical-align: -3px; margin-left: 5px;"/>
            <span style="margin-left:5px; font-size: 20px; color: #2E7850; font-weight:600;">{currency_symbol}{today_profit_plus}</span>
        </div>
        <div style="text-align: right; line-height: 24px;">
                <div style="display: flex; align-items: flex-start;">
                    <img src="{icon_total}" width="20" height="20" style="margin-right:15px;"/>
                    <div style="display: flex; flex-direction: column; justify-content: center; line-height: 20px; gap:4px; margin-right: 3px;">
                        <span style="font-size: 20px; font-weight: bold; color:{green_color if current_profit >= 0 else red_color};">
                            {currency_symbol}{current_profit:,.0f}
                        </span>
                    </div>
                </div>
        </div>
        </div>
    </div>
""").strip()

def icon_up(size=16, color=green_color):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16V8"/><path d="m8 12 4-4 4 4"/></svg>"""

def icon_down(size=16, color=red_color):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v8"/><path d="m8 12 4 4 4-4"/></svg>"""

# 종목별 수익률 바인딩
if not df_summary.empty:
    for _, row in df_summary.sort_values("평가금액", ascending=False).iterrows():
        name = row["종목명"]
        profit = row["평가손익"]
        profit_rate = row["수익률(%)"]
        stock_value = row["평가금액"]
        purchase_value = row["매입금액"]
        qty = row["보유수량"]
        avg_price = row["평균단가"]
        current_price = row["현재가"]

        icon_html = icon_up(size=24) if profit >=0 else icon_down(size=24)

        card_html_stock += dedent(f"""
        <div class="stock-item" style="display: flex; justify-content: space-between; align-items: center; margin-bottom:10px;">
            <div style="flex: 3.5; display: flex; align-items: center; gap: 10px; min-width: 0;" >
                {icon_html}
                <div>
                    <div class="stock-label" style="font-weight:600;">{name}</div>
                    <div style="font-size: 14px; font-weight: 500; color:#666; margin-top:2px;">
                        {qty:,.0f}주
                    </div>
                </div>
            </div>
            <div style="flex: 1; text-align: right; display: flex; flex-direction: column; justify-content: center; gap:4px;">
                <div style="font-size: 14px; font-weight: 500; color:#666; line-height: 22px;">
                    @ {currency_symbol}{current_price:,.0f}
                </div>
                <div style="font-size: 14px; font-weight: 500; color:#666; line-height: 22px;">
                    @ {currency_symbol}{avg_price:,.0f}
                </div>
            </div>
            <div style="flex: 1.5; text-align: right; display: flex; flex-direction: column; justify-content: center; gap:4px;">
                <div style="font-size: 14px; font-weight: 500; color:#666; line-height: 22px;">
                    {currency_symbol}{stock_value:,.0f}
                </div>
                <div style="font-size: 14px; font-weight: 500; color:#666; line-height: 22px;">
                    {currency_symbol}{purchase_value:,.0f}
                </div>
            </div>
            <div style="flex: 1.7; text-align: right; display: flex; flex-direction: column; justify-content: center; gap:4px;">
                <div style="font-size: 18px; font-weight: bold; color:{green_color if profit >= 0 else red_color}; line-height: 22px;">
                    {currency_symbol}{profit:,.0f}
                </div>
                <div style="font-size: 16px; font-weight: 500; color:{'#5BA17B' if profit >= 0 else red_color}; line-height: 22px;">
                    {profit_rate:.1f}%
                </div>
            </div>
        </div>
        """)
else:
    card_html_stock += """
    <div style="text-align: center; padding: 40px; color: #999; font-size: 18px;">
        보유중인 종목이 없습니다
    </div>
    """

# IRP 탭 : 종목별 막대 그래프 HTML
if selected_tab == "IRP":
    df_summary_sorted = df_summary.sort_values("평가금액", ascending=False).copy()
    
    # TDF 종목들을 합치기
    tdf_mask = df_summary_sorted["종목명"].str.contains("KB온국민TDF2055|TIGER TDF2045", na=False)
    
    if tdf_mask.any():
        # TDF 종목들의 데이터 합치기
        tdf_rows = df_summary_sorted[tdf_mask]
        tdf_total_value = tdf_rows["평가금액"].sum()
        
        # TDF 종목들 제거하고 합친 데이터 추가
        df_summary_sorted = df_summary_sorted[~tdf_mask].copy()
        
        # TDF(안전자산) 행 추가
        tdf_combined_row = pd.DataFrame({
            "종목코드": ["TDF"],
            "종목명": ["TDF(안전자산)"],
            "보유수량": [0],
            "평균단가": [0],
            "현재가": [0],
            "평가금액": [tdf_total_value],
            "매입금액": [tdf_rows["매입금액"].sum()],
            "평가손익": [tdf_rows["평가손익"].sum()],
            "수익률(%)": [0]
        })
        
        df_summary_sorted = pd.concat([df_summary_sorted, tdf_combined_row], ignore_index=True)
        df_summary_sorted = df_summary_sorted.sort_values("평가금액", ascending=False)
    
    df_summary_sorted["비중"] = df_summary_sorted["평가금액"] / df_summary_sorted["평가금액"].sum() * 100

    # 색상 리스트
    color_list = ["#375534", "#6B9071", "#aec3b0", "#e3eed4", "#6D6875"]
    total_eval = df_summary_sorted["평가금액"].sum()
    df_summary_sorted["color"] = [color_list[i % len(color_list)] for i in range(len(df_summary_sorted))]

    # 바(segment) 생성
    bar_segments = ""
    for i, row in df_summary_sorted.iterrows():
        percent = row["비중"]
        color = row["color"]
        bar_segments += f'<div style="width:{percent:.2f}%; background-color:{color};"></div>'

    # 범례 생성
    legend_html = ""
    for i, row in df_summary_sorted.iterrows():
        name = row["종목명"]
        percent = row["비중"]
        color = row["color"]
        legend_html += (
            f'<div style="display:flex; align-items:center; margin-right:16px; margin-bottom:4px;">'
            f'<div style="width:12px; height:12px; background-color:{color}; border-radius:3px; margin-right:6px;"></div>'
            f'<div style="font-size:14px; color:#666;">{name}</div>'
            f'<div style="font-size:14px; color:#444; margin-left:6px;">{percent:.0f}%</div>'
            f'</div>'
        )

    card_html_stock += dedent(f"""
        <div class="card-item" style="background: white;">
                <div style="display:flex; height:24px; border-radius:8px; overflow:hidden; margin-top:12px; margin-bottom:12px;">
                    {bar_segments}
                </div>
                <div style="display:flex; flex-wrap:wrap; justify-content:flex-start;">
                    {legend_html}
                </div>
            </div>
    """).strip()

# 마무리 태그
card_html_stock += "</div>"


# 성과 탭 레이아웃 구성 #
def clean_html(html_string):
    return ''.join(line.strip() for line in html_string.splitlines())

# ========================================
# 성과 탭 - 실제 데이터 연결 코드
# 기존 코드의 "성과 탭 레이아웃 구성" 부분을 아래 코드로 교체
# ========================================

if selected_tab == "성과":
    # --- 1. 전략별 데이터 계산 ---
    
    # Strategy 1: US Market Index (S&P, 나스닥)
    us_market_value = 0
    us_market_profit = 0
    us_market_buy_cost = 0
    
    for acct_name in ["ISA", "Pension", "IRP", "US"]:
        df_trade = trade_dfs[acct_name]
        df_cash = cash_df[cash_df["계좌명"] == acct_name]
        
        # S&P 또는 나스닥 유형 필터링
        sp_nasdaq_mask = df_trade["유형"].isin(["S&P", "나스닥"])
        df_filtered = df_trade[sp_nasdaq_mask]
        
        if not df_filtered.empty:
            df_s, _ = calculate_account_summary(df_filtered, df_cash, df_dividend, is_us_stock=(acct_name == "US"))
            if not df_s.empty:
                # US 계좌는 달러 단위이므로 환율 적용
                if acct_name == "US":
                    us_market_value += df_s["평가금액"].sum() * exchange_rate
                    us_market_profit += df_s["평가손익"].sum() * exchange_rate
                    us_market_buy_cost += df_s["매입금액"].sum() * exchange_rate
                else:
                    us_market_value += df_s["평가금액"].sum()
                    us_market_profit += df_s["평가손익"].sum()
                    us_market_buy_cost += df_s["매입금액"].sum()
    
    us_market_return = (us_market_profit / us_market_buy_cost * 100) if us_market_buy_cost > 0 else 0
    
    # Strategy 2: US AI Utility & Grid (전력)
    us_ai_value = 0
    us_ai_profit = 0
    us_ai_buy_cost = 0
    
    for acct_name in ["ISA", "Pension", "IRP", "US"]:
        df_trade = trade_dfs[acct_name]
        df_cash = cash_df[cash_df["계좌명"] == acct_name]
        
        # 전력 유형 필터링
        power_mask = df_trade["유형"] == "전력"
        df_filtered = df_trade[power_mask]
        
        if not df_filtered.empty:
            df_s, _ = calculate_account_summary(df_filtered, df_cash, df_dividend, is_us_stock=(acct_name == "US"))
            if not df_s.empty:
                # US 계좌는 달러 단위이므로 환율 적용
                if acct_name == "US":
                    us_ai_value += df_s["평가금액"].sum() * exchange_rate
                    us_ai_profit += df_s["평가손익"].sum() * exchange_rate
                    us_ai_buy_cost += df_s["매입금액"].sum() * exchange_rate
                else:
                    us_ai_value += df_s["평가금액"].sum()
                    us_ai_profit += df_s["평가손익"].sum()
                    us_ai_buy_cost += df_s["매입금액"].sum()
    
    us_ai_return = (us_ai_profit / us_ai_buy_cost * 100) if us_ai_buy_cost > 0 else 0
    
    # Strategy 3: US Managed WRAP
    wrap_value = wrap_value_usd * exchange_rate
    wrap_profit = (wrap_value_usd - wrap_capital_usd) * exchange_rate
    wrap_return = ((wrap_value_usd - wrap_capital_usd) / wrap_capital_usd * 100) if wrap_capital_usd > 0 else 0
    
    # Strategy 4: KR Index Leverage (LV 탭)
    try:
        lv_df = conn.read(worksheet="LV")
        lv_df.columns = lv_df.columns.str.strip()
        
        # C열(손익) 합계
        lv_profit = pd.to_numeric(lv_df.iloc[1:, 2], errors="coerce").sum()  # C2부터 (index 1부터)
        lv_capital = 10000000
        lv_value = lv_profit + lv_capital
        lv_return = (lv_profit / lv_capital * 100) if lv_capital > 0 else 0
    except Exception as e:
        st.warning(f"LV 데이터 로드 실패: {e}")
        lv_value = 0
        lv_profit = 0
        lv_return = 0
    
    # Strategy 5: KR Sector ETFs (ETF 탭)
    df_trade_etf = trade_dfs["ETF"]
    df_cash_etf = cash_df[cash_df["계좌명"] == "ETF"]
    df_s_etf, s_etf = calculate_account_summary(df_trade_etf, df_cash_etf, df_dividend)
    
    etf_value = s_etf["total_balance"]
    etf_profit = s_etf["current_profit"] + s_etf["actual_profit"]
    etf_return = s_etf["total_profit_rate"]
    
    # --- 2. 전략 리스트 생성 ---
    strategies = [
        {
            "name": "US Market Index",
            "value": int(us_market_value),
            "profit": int(us_market_profit),
            "rate": round(us_market_return, 1),
            "color": "#412f95"
        },
        {
            "name": "US AI Utility & Grid",
            "value": int(us_ai_value),
            "profit": int(us_ai_profit),
            "rate": round(us_ai_return, 1),
            "color": "#7875f4"
        },
        {
            "name": "US Managed WRAP",
            "value": int(wrap_value),
            "profit": int(wrap_profit),
            "rate": round(wrap_return, 1),
            "color": "#ffb601"
        },
        {
            "name": "KR Index Leverage",
            "value": int(lv_value),
            "profit": int(lv_profit),
            "rate": round(lv_return, 1),
            "color": "#ff7f05"
        },
        {
            "name": "KR Sector ETFs",
            "value": int(etf_value),
            "profit": int(etf_profit),
            "rate": round(etf_return, 1),
            "color": "#ff76a6"
        }
    ]
    
    # 총합 계산
    total_strategy_value = sum(s["value"] for s in strategies)
    total_strategy_profit = sum(s["profit"] for s in strategies)
    
    # 비중 계산 (value 기준)
    for strategy in strategies:
        strategy["weight"] = round((strategy["value"] / total_strategy_value * 100), 1) if total_strategy_value > 0 else 0
    
    # --- 3. HTML 생성 ---
    
    # 좌측 상단: Total Portfolio Value
    total_portfolio_value = total_strategy_value
    total_profit_ov = total_strategy_profit
    total_profit_rate_ov = round((total_profit_ov / (total_portfolio_value - total_profit_ov) * 100), 1) if (total_portfolio_value - total_profit_ov) > 0 else 0
    
    total_value_html = clean_html(f"""
    <div class="total-value-card">
        <div class="total-value-title">Total Portfolio Value</div>
        <div class="total-value-amount">{total_portfolio_value:,}</div>
        <div class="value-divider"></div>
        <div class="profit-section">
            <div class="profit-label">Total Profit</div>
            <div class="profit-row">
                <div class="profit-amount">+{total_profit_ov:,}</div>
                <div class="profit-badge">+{total_profit_rate_ov}%</div>
            </div>
        </div>
    </div>
    """)
    
    # 좌측 하단: Asset & Country Allocation (옵션 4 - 실제 데이터)
    
    # Stock = 5가지 전략의 Value 총합
    stock_value_ov = total_strategy_value
    
    # Cash 계산
    # 1. 전체 탭(ISA+Pension+IRP+ETF) Cash
    local_cash = local_summary["cash"]
    
    # 2. US 탭 Cash (달러 -> 원화 환산)
    df_trade_us = trade_dfs["US"]
    df_cash_us = cash_df[cash_df["계좌명"] == "US"]
    _, s_us = calculate_account_summary(df_trade_us, df_cash_us, df_dividend, is_us_stock=True)
    us_cash = s_us["cash"] * exchange_rate
    
    # 3. 입출금 탭 I1 셀 (별도 현금)
    try:
        separate_cash_df = conn.read(worksheet="입출금", usecols=[8], nrows=1, header=None)
        separate_cash = float(separate_cash_df.iloc[0, 0]) if not separate_cash_df.empty else 0
    except:
        separate_cash = 0
    
    cash_value_ov = local_cash + us_cash + separate_cash
    
    # Asset Allocation 비중
    total_asset = stock_value_ov + cash_value_ov
    stock_ratio_ov = (stock_value_ov / total_asset * 100) if total_asset > 0 else 0
    cash_ratio_ov = (cash_value_ov / total_asset * 100) if total_asset > 0 else 0
    
    # US = Strategy 1 + 2 + 3
    us_value = strategies[0]["value"] + strategies[1]["value"] + strategies[2]["value"]
    
    # KR = Strategy 4 + 5
    kr_value = strategies[3]["value"] + strategies[4]["value"]
    
    # Country Allocation 비중
    total_country = us_value + kr_value
    us_ratio = (us_value / total_country * 100) if total_country > 0 else 0
    kr_ratio = (kr_value / total_country * 100) if total_country > 0 else 0
    
    allocation_html = clean_html(f"""
    <div class="card">
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
            <!-- Asset Allocation Card -->
            <div style="background: linear-gradient(135deg, #6374b908 0%, #6374b902 100%);
                        border-radius: 12px; padding: 24px;">
                <div style="font-size: 13px; font-weight: 600; color: #7F8C8D; margin-bottom: 16px;">
                    ASSET ALLOCATION
                </div>
                
                <div style="display: flex; flex-direction: column; gap: 16px;">
                    <!-- Stock -->
                    <div>
                        <div style="display: flex; justify-content: space-between; margin-bottom: 6px;">
                            <span style="font-size: 14px; font-weight: 500; color: #555;">Stock</span>
                            <span style="font-size: 14px; font-weight: 700; color: #6374b9;">{stock_ratio_ov:.1f}%</span>
                        </div>
                        <div style="font-size: 22px; font-weight: 700; color: #0f2f76;">{int(stock_value_ov):,}</div>
                    </div>
                    
                    <div style="height: 1px; background: #e5e5e5;"></div>
                    
                    <!-- Cash -->
                    <div>
                        <div style="display: flex; justify-content: space-between; margin-bottom: 6px;">
                            <span style="font-size: 14px; font-weight: 500; color: #555;">Cash</span>
                            <span style="font-size: 14px; font-weight: 700; color: #95a5a6;">{cash_ratio_ov:.1f}%</span>
                        </div>
                        <div style="font-size: 22px; font-weight: 700; color: #0f2f76;">{int(cash_value_ov):,}</div>
                    </div>
                </div>
            </div>
            
            <!-- Country Allocation Card -->
            <div style="background: linear-gradient(135deg, #778AD508 0%, #778AD502 100%);
                        border-radius: 12px; padding: 24px;">
                <div style="font-size: 13px; font-weight: 600; color: #7F8C8D; margin-bottom: 16px;">
                    COUNTRY ALLOCATION
                </div>
                
                <div style="display: flex; flex-direction: column; gap: 16px;">
                    <!-- US -->
                    <div>
                        <div style="display: flex; justify-content: space-between; margin-bottom: 6px;">
                            <span style="font-size: 14px; font-weight: 500; color: #555;">US</span>
                            <span style="font-size: 14px; font-weight: 700; color: #778AD5;">{us_ratio:.1f}%</span>
                        </div>
                        <div style="font-size: 22px; font-weight: 700; color: #0f2f76;">{int(us_value):,}</div>
                    </div>
                    
                    <div style="height: 1px; background: #e5e5e5;"></div>
                    
                    <!-- KR -->
                    <div>
                        <div style="display: flex; justify-content: space-between; margin-bottom: 6px;">
                            <span style="font-size: 14px; font-weight: 500; color: #555;">KR</span>
                            <span style="font-size: 14px; font-weight: 700; color: #5BA17B;">{kr_ratio:.1f}%</span>
                        </div>
                        <div style="font-size: 22px; font-weight: 700; color: #0f2f76;">{int(kr_value):,}</div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    """)
    
    # 우측: Strategy Performance (최종 디자인 적용)
    strategy_items = ""
    for strategy in strategies:
        strategy_items += f"""
            <div style="display: grid; grid-template-columns: 100px 2fr 2fr 1fr;
                        padding: 18px 20px; align-items: center;
                        border-bottom: 1px solid #f0f0f0;
                        transition: background 0.2s ease; cursor: pointer;"
                 onmouseover="this.style.background='#f8f9fa'"
                 onmouseout="this.style.background='transparent'">
                
                <div style="position: relative; width: 80px; height: 80px; flex-shrink: 0;">
                    <div style="position: absolute; width: 80px; height: 80px; border-radius: 50%;
                                background: conic-gradient(from 0deg, {strategy['color']} 0deg {strategy['weight'] * 3.6}deg, #e5e5e5 {strategy['weight'] * 3.6}deg 360deg);"></div>
                    <div style="position: absolute; width: 56px; height: 56px; background: white;
                                border-radius: 50%; top: 12px; left: 12px;"></div>
                    <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
                                font-size: 14px; font-weight: 700; color: {strategy['color']}; z-index: 10;">
                        {strategy['weight']}%
                    </div>
                </div>
                
                <div>
                    <div style="font-size: 15px; font-weight: 600; color: #2C3E50;">
                        {strategy['name']}
                    </div>
                </div>
                
                <div style="text-align: right; display: flex; flex-direction: column; gap: 6px;">
                    <div style="font-size: 17px; font-weight: 600; color: #2C3E50;">
                        {strategy['value']:,}
                    </div>
                    <div style="font-size: 15px; font-weight: 600; color: {'#27AE60' if strategy['profit'] >= 0 else '#C54E4A'};">
                        {'+' if strategy['profit'] >= 0 else ''}{strategy['profit']:,}
                    </div>
                </div>
                
                <div style="text-align: center;">
                    <div style="background: {strategy['color']}20; color: {strategy['color']};
                                font-size: 14px; font-weight: 700;
                                padding: 6px 12px; border-radius: 8px; display: inline-block;">
                        {'+' if strategy['rate'] >= 0 else ''}{strategy['rate']}%
                    </div>
                </div>
            </div>
        """
    
    strategy_html = clean_html(f"""
    <div class="card" style="height: 100%;">
        <div class="card-title">Strategy Performance</div>
        
        <div style="display: grid; grid-template-columns: 100px 2fr 2fr 1fr;
                    padding: 16px 20px; margin-top: 20px;
                    background: #f8f9fa; border-radius: 8px;
                    font-size: 13px; font-weight: 600; color: #6c757d;">
            <div></div>
            <div>Strategy</div>
            <div style="text-align: center;">Value / Profit</div>
            <div style="text-align: center;">Return</div>
        </div>
        
        <div style="display: flex; flex-direction: column; gap: 2px; margin-top: 8px;">
            {strategy_items}
        </div>
    </div>
    """)
    
    # 레이아웃 (좌측: Total Value + Asset Allocation, 우측: Strategy Performance)
    col_left, col_right = st.columns([1, 1.3])
    with col_left:
        st.markdown(total_value_html, unsafe_allow_html=True)
        st.markdown(allocation_html, unsafe_allow_html=True)
    with col_right:
        st.markdown(strategy_html, unsafe_allow_html=True)

else:
    # 기존 레이아웃 (그대로 유지)
    with st.container():
        col_left, col_right = st.columns([1, 1.2])
        with col_left:
            st.markdown(card_html_profit, unsafe_allow_html=True)
            st.markdown(card_html_balance, unsafe_allow_html=True)
        with col_right:
            st.markdown(card_html_stock, unsafe_allow_html=True)