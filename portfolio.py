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
    margin-right: 10px;
            
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
ACCOUNT_NAMES = ["전체", "ISA", "Pension", "IRP", "ETF", "US", "성과"]
green_color = "#3A866A"
red_color = "#C54E4A"

# 옵션 메뉴 사용
selected_tab = option_menu(
    menu_title=None,
    options=ACCOUNT_NAMES,
    icons=["back", "geo-alt-fill","geo-alt-fill","geo-alt-fill","geo-alt-fill","geo-alt-fill","pie-chart-fill"],
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


# ========================================
# 성과 탭 - 실제 데이터 연결 코드
# ========================================

def clean_html(html_string):
    return ''.join(line.strip() for line in html_string.splitlines())


if selected_tab == "성과":
    # --- 1. 전략별 데이터 계산 ---
    
    # Strategy 1: US Market Index
    us_market_value = 0
    us_market_current_profit = 0
    us_market_actual_profit = 0  # 매매손익만
    us_market_buy_cost = 0

    # 빈 배당 데이터프레임 생성
    empty_dividend = pd.DataFrame(columns=df_dividend.columns)

    for acct_name in ["ISA", "Pension", "IRP", "US"]:
        df_trade = trade_dfs[acct_name]
        df_cash = cash_df[cash_df["계좌명"] == acct_name]
        
        sp_nasdaq_mask = df_trade["유형"].isin(["S&P", "나스닥", "TDF"])
        df_filtered = df_trade[sp_nasdaq_mask]
        
        if not df_filtered.empty:
            # 배당 데이터를 빈 DataFrame으로 전달
            df_s, s = calculate_account_summary(df_filtered, df_cash, empty_dividend, is_us_stock=(acct_name == "US"))
            if not df_s.empty:
                if acct_name == "US":
                    us_market_value += df_s["평가금액"].sum() * exchange_rate
                    us_market_current_profit += df_s["평가손익"].sum() * exchange_rate
                    us_market_buy_cost += df_s["매입금액"].sum() * exchange_rate
                    us_market_actual_profit += s["actual_profit"] * exchange_rate
                else:
                    us_market_value += df_s["평가금액"].sum()
                    us_market_current_profit += df_s["평가손익"].sum()
                    us_market_buy_cost += df_s["매입금액"].sum()
                    us_market_actual_profit += s["actual_profit"]

    us_market_profit = us_market_current_profit + us_market_actual_profit
    us_market_return = (us_market_profit / us_market_buy_cost * 100) if us_market_buy_cost > 0 else 0


    # Strategy 2: US AI Power & Grid
    us_ai_value = 0
    us_ai_current_profit = 0
    us_ai_actual_profit = 0  # 매매손익만
    us_ai_buy_cost = 0

    for acct_name in ["ISA", "Pension", "IRP", "US"]:
        df_trade = trade_dfs[acct_name]
        df_cash = cash_df[cash_df["계좌명"] == acct_name]
        
        power_mask = df_trade["유형"] == "전력"
        df_filtered = df_trade[power_mask]
        
        if not df_filtered.empty:
            # 배당 데이터를 빈 DataFrame으로 전달
            df_s, s = calculate_account_summary(df_filtered, df_cash, empty_dividend, is_us_stock=(acct_name == "US"))
            if not df_s.empty:
                if acct_name == "US":
                    us_ai_value += df_s["평가금액"].sum() * exchange_rate
                    us_ai_current_profit += df_s["평가손익"].sum() * exchange_rate
                    us_ai_buy_cost += df_s["매입금액"].sum() * exchange_rate
                    us_ai_actual_profit += s["actual_profit"] * exchange_rate
                else:
                    us_ai_value += df_s["평가금액"].sum()
                    us_ai_current_profit += df_s["평가손익"].sum()
                    us_ai_buy_cost += df_s["매입금액"].sum()
                    us_ai_actual_profit += s["actual_profit"]

    us_ai_profit = us_ai_current_profit + us_ai_actual_profit
    us_ai_return = (us_ai_profit / us_ai_buy_cost * 100) if us_ai_buy_cost > 0 else 0
    
    # Strategy 3: US Managed WRAP
    wrap_value = wrap_value_usd * exchange_rate
    wrap_profit = (wrap_value_usd - wrap_capital_usd) * exchange_rate
    wrap_return = ((wrap_value_usd - wrap_capital_usd) / wrap_capital_usd * 100) if wrap_capital_usd > 0 else 0
    
    # Strategy 4: KR Index Leverage (LV 탭)
    try:
        lv_df = conn.read(worksheet="LV")
        lv_df.columns = lv_df.columns.str.strip()
        
        # 손익 컬럼 합계
        lv_profit = pd.to_numeric(lv_df["손익"], errors="coerce").sum()
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
    
    etf_value = s_etf["current_value"]
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
            "name": "US AI Power & Grid",
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
        <div class="card-title">Allocation</div>
        
        <!-- Asset Allocation -->
        <div style="margin-top: 20px; margin-bottom: 24px;">
            <div style="font-size: 14px; font-weight: 600; color: #7F8C8D; margin-bottom: 12px; padding-left: 8px;">
                ASSET ALLOCATION
            </div>
            
            <div style="display: flex; flex-direction: column; gap: 8px;">
                <!-- Stock -->
                <div style="display: grid; grid-template-columns: 80px 1fr auto;
                            align-items: center; gap: 16px;
                            background: #f8f9fa; padding: 14px 16px; border-radius: 10px;">
                    <div style="font-size: 13px; font-weight: 600; color: #555;">Stock</div>
                    <div style="font-size: 18px; font-weight: 700; color: #0f2f76;">{int(stock_value_ov):,}</div>
                    <div style="background: #778ad5; color: white;
                                padding: 6px 14px; border-radius: 8px;
                                font-size: 13px; font-weight: 700; width: 70px; text-align: center;">
                        {stock_ratio_ov:.1f}%
                    </div>
                </div>
                
                <!-- Cash -->
                <div style="display: grid; grid-template-columns: 80px 1fr auto;
                            align-items: center; gap: 16px;
                            background: #f8f9fa; padding: 14px 16px; border-radius: 10px;">
                    <div style="font-size: 13px; font-weight: 600; color: #555;">Cash</div>
                    <div style="font-size: 18px; font-weight: 700; color: #0f2f76;">{int(cash_value_ov):,}</div>
                    <div style="background: #b2c2ff; color: white;
                                padding: 6px 14px; border-radius: 8px;
                                font-size: 13px; font-weight: 700; width: 70px; text-align: center;">
                        {cash_ratio_ov:.1f}%
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Country Allocation -->
        <div style="margin-top: 24px;">
            <div style="font-size: 14px; font-weight: 600; color: #7F8C8D; margin-bottom: 12px; padding-left: 8px;">
                COUNTRY ALLOCATION
            </div>
            
            <div style="display: flex; flex-direction: column; gap: 8px;">
                <!-- US -->
                <div style="display: grid; grid-template-columns: 80px 1fr auto;
                            align-items: center; gap: 16px;
                            background: #f8f9fa; padding: 14px 16px; border-radius: 10px;">
                    <div style="font-size: 13px; font-weight: 600; color: #555;">US</div>
                    <div style="font-size: 18px; font-weight: 700; color: #0f2f76;">{int(us_value):,}</div>
                    <div style="background: #778ad5; color: white;
                                padding: 6px 14px; border-radius: 8px;
                                font-size: 13px; font-weight: 700; width: 70px; text-align: center;">
                        {us_ratio:.1f}%
                    </div>
                </div>
                
                <!-- KR -->
                <div style="display: grid; grid-template-columns: 80px 1fr auto;
                            align-items: center; gap: 16px;
                            background: #f8f9fa; padding: 14px 16px; border-radius: 10px;">
                    <div style="font-size: 13px; font-weight: 600; color: #555;">KR</div>
                    <div style="font-size: 18px; font-weight: 700; color: #0f2f76;">{int(kr_value):,}</div>
                    <div style="background: #b2c2ff; color: white;
                                padding: 6px 14px; border-radius: 8px;
                                font-size: 13px; font-weight: 700; width: 70px; text-align: center;">
                        {kr_ratio:.1f}%
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
            <div style="display: grid; grid-template-columns: 100px 2fr 2fr 1.5fr;
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
                    <div style="font-size: 15px; font-weight: 600; color: {'#3A866A' if strategy['profit'] >= 0 else '#C54E4A'};">
                        {'+' if strategy['profit'] >= 0 else ''}{strategy['profit']:,}
                    </div>
                </div>
                
                <div style="text-align: right;">
                    <div style="background: {strategy['color']}20; color: {strategy['color']};
                                font-size: 14px; font-weight: 700;
                                padding: 6px 12px; border-radius: 8px; display: inline-block;">
                        {'+' if strategy['rate'] >= 0 else ''}{strategy['rate']}%
                    </div>
                </div>
            </div>
        """
    
    strategy_html = clean_html(f"""
    <div class="card" style="height: 785px;">
        <div class="card-title">Strategy Performance</div>
        
        <div style="display: grid; grid-template-columns: 100px 2fr 2fr 1.5fr;
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

    # --- 월간 성과 데이터 불러오기 ---
    try:
        performance_df = conn.read(worksheet="성과")
        performance_df.columns = performance_df.columns.str.strip()   
        performance_df["기준일"] = pd.to_datetime(performance_df["기준일"])
        
        performance_df = performance_df.sort_values("기준일", ascending=False)
        
        monthly_totals = performance_df.groupby("기준일").agg({
            "평가액": "sum",
            "누적수익": "sum",
            "손익변동": "sum"
        }).reset_index().sort_values("기준일", ascending=False)

        recent_3_months = monthly_totals.head(3)
        recent_6_months = monthly_totals.head(6)
        
    except Exception as e:
        st.error(f"성과 데이터 로드 실패: {e}")
        recent_6_months = pd.DataFrame()
        recent_3_months = pd.DataFrame()

    # --- 당월 신규 매수액 계산 함수 ---
    def calculate_monthly_purchase(month_date):
        """당월(month_date) 신규 매수액을 전략별로 계산"""
        month_start = month_date.replace(day=1)
        if month_date.month == 12:
            month_end = month_date.replace(year=month_date.year + 1, month=1, day=1) - pd.Timedelta(days=1)
        else:
            month_end = month_date.replace(month=month_date.month + 1, day=1) - pd.Timedelta(days=1)
        
        purchases = {
            "US Market": 0,
            "US AI Power": 0,
            "US WRAP": 0,
            "KR Leverage": 0,
            "KR Sector": 0
        }
        
        # 1. US Market Index (S&P, 나스닥, TDF)
        for acct_name in ["ISA", "Pension", "IRP", "US"]:
            df_trade = trade_dfs[acct_name]
            monthly_trades = df_trade[
                (df_trade["거래일"] >= month_start) & 
                (df_trade["거래일"] <= month_end) &
                (df_trade["유형"].isin(["S&P", "나스닥", "TDF"]))
            ]
            
            if not monthly_trades.empty:
                # 매수 - 매도
                buy_amt = monthly_trades[monthly_trades["구분"] == "매수"]["거래금액"].sum()
                sell_amt = monthly_trades[monthly_trades["구분"] == "매도"]["거래금액"].sum()
                net_amt = buy_amt - sell_amt
                
                if acct_name == "US":
                    purchases["US Market"] += net_amt * exchange_rate
                else:
                    purchases["US Market"] += net_amt
        
        # 2. US AI Power (전력)
        for acct_name in ["ISA", "Pension", "IRP", "US"]:
            df_trade = trade_dfs[acct_name]
            monthly_trades = df_trade[
                (df_trade["거래일"] >= month_start) & 
                (df_trade["거래일"] <= month_end) &
                (df_trade["유형"] == "전력")
            ]
            
            if not monthly_trades.empty:
                buy_amt = monthly_trades[monthly_trades["구분"] == "매수"]["거래금액"].sum()
                sell_amt = monthly_trades[monthly_trades["구분"] == "매도"]["거래금액"].sum()
                net_amt = buy_amt - sell_amt
                
                if acct_name == "US":
                    purchases["US AI Power"] += net_amt * exchange_rate
                else:
                    purchases["US AI Power"] += net_amt
        
        # 3. US WRAP - 성과 시트에서
        wrap_data = performance_df[
            (performance_df["기준일"] == month_date) &
            (performance_df["전략"] == "US Wrap")
        ]
        if not wrap_data.empty:
            wrap_purchase = wrap_data["운용증가"].values[0]
            purchases["US WRAP"] = wrap_purchase if pd.notna(wrap_purchase) else 0
        
        # 4. KR Leverage - 0 (고려 안함)
        purchases["KR Leverage"] = 0
        
        # 5. KR Sector (ETF 계좌 전체)
        df_trade = trade_dfs["ETF"]
        monthly_trades = df_trade[
            (df_trade["거래일"] >= month_start) & 
            (df_trade["거래일"] <= month_end)
        ]
        
        if not monthly_trades.empty:
            buy_amt = monthly_trades[monthly_trades["구분"] == "매수"]["거래금액"].sum()
            sell_amt = monthly_trades[monthly_trades["구분"] == "매도"]["거래금액"].sum()
            purchases["KR Sector"] = buy_amt - sell_amt
        
        return purchases

    # --- 통합 카드: 3개월 카드 + 테이블 ---
    if not recent_3_months.empty:
        monthly_performance_html = '<div class="card" style="margin-top: 24px;">'
        
        # 제목
        monthly_performance_html += """
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
            <div style="font-size: 20px; font-weight: 600; color: #444;">Monthly Performance Detail</div>
            <div style="font-size: 13px; color: #95a5a6;">Recent 3 months</div>
        </div>
        """
        
        # 상단: 최근 3개월 카드
        monthly_performance_html += '<div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;">'
        
        colors = ["#95a5a6", "#95a5a6", "#778AD5"]
        
        for idx, row in recent_3_months.iterrows():
            month_str = row["기준일"].strftime("%B %Y")
            
            # 첫 번째(최근) 월만 Strategy Performance에서
            if idx == recent_3_months.index[0]:
                total_asset = total_strategy_value
                
                # MoM 계산: 이전 달이 있으면
                if len(recent_3_months) > 1:
                    prev_month_value = int(recent_3_months.iloc[1]["평가액"])
                    
                    # 당월 신규 매수액 계산
                    monthly_purchases = calculate_monthly_purchase(row["기준일"])
                    total_monthly_purchase = sum(monthly_purchases.values())
                    
                    # 순수익 = (당월 - 전월) - 신규 매수액
                    mom_change = total_asset - prev_month_value - total_monthly_purchase
                else:
                    mom_change = 0
            else:
                # 나머지는 성과 시트에서
                total_asset = int(row["평가액"])
                mom_change = int(row["손익변동"]) if pd.notna(row["손익변동"]) else 0
            
            sign = "+" if mom_change >= 0 else ""
            color_idx = min(idx, 2)
            
            monthly_performance_html += f"""
            <div style="background: {colors[color_idx]};
                        border-radius: 12px; padding: 20px; color: white;">
                <div style="font-size: 13px; opacity: 0.9; margin-bottom: 8px;">{month_str}</div>
                <div style="font-size: 28px; font-weight: 700; margin-bottom: 16px;">{total_asset:,}</div>
                <div style="display: flex; align-items: center; gap: 8px;">
                    <div style="background: rgba(255,255,255,0.2); padding: 4px 10px; border-radius: 6px; font-size: 13px; font-weight: 600;">
                        {sign}{mom_change:,.0f}
                    </div>
                    <div style="font-size: 13px; opacity: 0.9;">MoM</div>
                </div>
            </div>
            """
        
        monthly_performance_html += '</div>'
        
        # 구분선
        monthly_performance_html += '<div style="border-top: 1px solid #e5e5e5; margin: 32px 0;"></div>'
        
        # 하단: 테이블
        if not recent_6_months.empty:
            strategy_monthly = performance_df[performance_df["전략"] != "Total"].copy()
            latest_dates = monthly_totals.head(6)["기준일"].tolist()
            strategy_monthly = strategy_monthly[strategy_monthly["기준일"].isin(latest_dates)]
            
            # 테이블 헤더
            monthly_performance_html += """
            <div style="display: grid; grid-template-columns: 100px repeat(6, 1fr);
                        padding: 12px 16px; background: #f8f9fa; border-radius: 8px;
                        font-size: 12px; font-weight: 600; color: #6c757d; margin-bottom: 8px;">
                <div>Month</div>
                <div style="text-align: right;">US Market</div>
                <div style="text-align: right;">US AI</div>
                <div style="text-align: right;">US WRAP</div>
                <div style="text-align: right;">KR Leverage</div>
                <div style="text-align: right;">KR ETF</div>
                <div style="text-align: right;">Total</div>
            </div>
            """
            
            # 월별 데이터 행
            for idx, month_date in enumerate(latest_dates):
                month_str = month_date.strftime("%Y-%m")
                
                # 최근월(첫 번째)만 Strategy Performance에서
                if idx == 0:
                    us_market_val = strategies[0]["value"]
                    us_ai_val = strategies[1]["value"]
                    us_wrap_val = strategies[2]["value"]
                    kr_leverage_val = strategies[3]["value"]
                    kr_sector_val = strategies[4]["value"]
                    total_val = sum(s["value"] for s in strategies)
                else:
                    # 나머지는 성과 시트에서
                    month_strategies = strategy_monthly[strategy_monthly["기준일"] == month_date]
                    
                    us_market_val = int(month_strategies[month_strategies["전략"] == "US Market"]["평가액"].values[0]) if len(month_strategies[month_strategies["전략"] == "US Market"]) > 0 else 0
                    us_ai_val = int(month_strategies[month_strategies["전략"] == "US AI Power"]["평가액"].values[0]) if len(month_strategies[month_strategies["전략"] == "US AI Power"]) > 0 else 0
                    us_wrap_val = int(month_strategies[month_strategies["전략"] == "US Wrap"]["평가액"].values[0]) if len(month_strategies[month_strategies["전략"] == "US Wrap"]) > 0 else 0
                    kr_leverage_val = int(month_strategies[month_strategies["전략"] == "KR Leverage"]["평가액"].values[0]) if len(month_strategies[month_strategies["전략"] == "KR Leverage"]) > 0 else 0
                    kr_sector_val = int(month_strategies[month_strategies["전략"] == "KR Sector"]["평가액"].values[0]) if len(month_strategies[month_strategies["전략"] == "KR Sector"]) > 0 else 0
                    
                    total_row = monthly_totals[monthly_totals["기준일"] == month_date]
                    total_val = int(total_row["평가액"].values[0]) if not total_row.empty else 0
                
                # 이전 달과 비교 (인디케이터 표시)
                def get_indicator(current_val, prev_val):
                    """이전월 대비 손익을 이전월 평가액의 %로 계산하여 인디케이터 반환"""
                    if prev_val == 0:
                        return ' <span style="color: #95a5a6; font-size: 18px;">●</span>'  # 이전 데이터 없음 → 회색
                    
                    change = current_val - prev_val
                    change_rate = (change / prev_val) * 100
                    
                    if change_rate > 1:
                        return ' <span style="color: #3A866A; font-size: 18px;">●</span>'
                    elif change_rate < -1:
                        return ' <span style="color: #C54E4A; font-size: 18px;">●</span>'
                    else:
                        return ' <span style="color: #95a5a6; font-size: 18px;">●</span>'

                # 이전 달 데이터 가져오기 (인디케이터용)
                us_market_indicator = ' <span style="color: #ffffff; font-size: 18px;">●</span>'  # 기본값 회색
                us_ai_indicator = ' <span style="color: #ffffff; font-size: 18px;">●</span>'
                us_wrap_indicator = ' <span style="color: #ffffff; font-size: 18px;">●</span>'
                kr_leverage_indicator = ' <span style="color: #ffffff; font-size: 18px;">●</span>'
                kr_sector_indicator = ' <span style="color: #ffffff; font-size: 18px;">●</span>'

                if idx < len(latest_dates) - 1:  # 이전 달이 있는 경우
                    prev_date = latest_dates[idx + 1]
                    prev_strategies = strategy_monthly[strategy_monthly["기준일"] == prev_date]
                    
                    prev_us_market = int(prev_strategies[prev_strategies["전략"] == "US Market"]["평가액"].values[0]) if len(prev_strategies[prev_strategies["전략"] == "US Market"]) > 0 else 0
                    prev_us_ai = int(prev_strategies[prev_strategies["전략"] == "US AI Power"]["평가액"].values[0]) if len(prev_strategies[prev_strategies["전략"] == "US AI Power"]) > 0 else 0
                    prev_us_wrap = int(prev_strategies[prev_strategies["전략"] == "US Wrap"]["평가액"].values[0]) if len(prev_strategies[prev_strategies["전략"] == "US Wrap"]) > 0 else 0
                    prev_kr_leverage = int(prev_strategies[prev_strategies["전략"] == "KR Leverage"]["평가액"].values[0]) if len(prev_strategies[prev_strategies["전략"] == "KR Leverage"]) > 0 else 0
                    prev_kr_sector = int(prev_strategies[prev_strategies["전략"] == "KR Sector"]["평가액"].values[0]) if len(prev_strategies[prev_strategies["전략"] == "KR Sector"]) > 0 else 0
                    
                    us_market_indicator = get_indicator(us_market_val, prev_us_market)
                    us_ai_indicator = get_indicator(us_ai_val, prev_us_ai)
                    us_wrap_indicator = get_indicator(us_wrap_val, prev_us_wrap)
                    kr_leverage_indicator = get_indicator(kr_leverage_val, prev_kr_leverage)
                    kr_sector_indicator = get_indicator(kr_sector_val, prev_kr_sector)
                
                bg_color = "#fafafa" if idx % 2 == 1 else "transparent"
                
                monthly_performance_html += f"""
                <div style="display: grid; grid-template-columns: 100px repeat(6, 1fr);
                            padding: 14px 16px; align-items: center; border-bottom: 1px solid #f0f0f0;
                            background: {bg_color};">
                    <div style="font-weight: 600; color: #2C3E50;">{month_str}</div>
                    <div style="text-align: right; font-size: 14px; color: #555;">{us_market_val/1000000:.1f}M{us_market_indicator}</div>
                    <div style="text-align: right; font-size: 14px; color: #555;">{us_ai_val/1000000:.1f}M{us_ai_indicator}</div>
                    <div style="text-align: right; font-size: 14px; color: #555;">{us_wrap_val/1000000:.1f}M{us_wrap_indicator}</div>
                    <div style="text-align: right; font-size: 14px; color: #555;">{kr_leverage_val/1000000:.1f}M{kr_leverage_indicator}</div>
                    <div style="text-align: right; font-size: 14px; color: #555;">{kr_sector_val/1000000:.1f}M{kr_sector_indicator}</div>
                    <div style="text-align: right; font-size: 16px; font-weight: 700; color: #0f2f76;">{total_val/1000000:.1f}M</div>
                </div>
                """
            
            # MoM Change 행
            if len(latest_dates) >= 2:
                # 당월 신규 매수액
                monthly_purchases = calculate_monthly_purchase(latest_dates[0])
                
                # 전월 데이터
                prev_month_strategies = strategy_monthly[strategy_monthly["기준일"] == latest_dates[1]]
                prev_month_totals = monthly_totals[monthly_totals["기준일"] == latest_dates[1]]
                
                def calc_mom(strategy_name, current_val, prev_val, purchase):
                    """MoM = (당월 - 전월) - 신규 매수액"""
                    return current_val - prev_val - purchase
                
                # 각 전략별 MoM
                prev_us_market = int(prev_month_strategies[prev_month_strategies["전략"] == "US Market"]["평가액"].values[0]) if len(prev_month_strategies[prev_month_strategies["전략"] == "US Market"]) > 0 else 0
                prev_us_ai = int(prev_month_strategies[prev_month_strategies["전략"] == "US AI Power"]["평가액"].values[0]) if len(prev_month_strategies[prev_month_strategies["전략"] == "US AI Power"]) > 0 else 0
                prev_us_wrap = int(prev_month_strategies[prev_month_strategies["전략"] == "US Wrap"]["평가액"].values[0]) if len(prev_month_strategies[prev_month_strategies["전략"] == "US Wrap"]) > 0 else 0
                prev_kr_leverage = int(prev_month_strategies[prev_month_strategies["전략"] == "KR Leverage"]["평가액"].values[0]) if len(prev_month_strategies[prev_month_strategies["전략"] == "KR Leverage"]) > 0 else 0
                prev_kr_sector = int(prev_month_strategies[prev_month_strategies["전략"] == "KR Sector"]["평가액"].values[0]) if len(prev_month_strategies[prev_month_strategies["전략"] == "KR Sector"]) > 0 else 0
                
                us_market_mom = calc_mom("US Market", strategies[0]["value"], prev_us_market, monthly_purchases["US Market"])
                us_ai_mom = calc_mom("US AI", strategies[1]["value"], prev_us_ai, monthly_purchases["US AI Power"])
                us_wrap_mom = calc_mom("US WRAP", strategies[2]["value"], prev_us_wrap, monthly_purchases["US WRAP"])
                kr_leverage_mom = calc_mom("KR Leverage", strategies[3]["value"], prev_kr_leverage, monthly_purchases["KR Leverage"])
                kr_sector_mom = calc_mom("KR Sector", strategies[4]["value"], prev_kr_sector, monthly_purchases["KR Sector"])
                
                # Total MoM
                prev_total = int(prev_month_totals["평가액"].values[0]) if not prev_month_totals.empty else 0
                total_purchase = sum(monthly_purchases.values())
                total_mom = total_strategy_value - prev_total - total_purchase
                
                def get_mom_color(val):
                    return "#3A866A" if val >= 0 else "#C54E4A"
                
                def get_mom_sign(val):
                    return "+" if val >= 0 else ""
                
                # 투명 동그라미 (정렬용)
                invisible_dot = ' <span style="color: #f0f7ff; font-size: 18px;">●</span>'
                
                monthly_performance_html += f"""
                <div style="display: grid; grid-template-columns: 100px repeat(6, 1fr);
                            padding: 14px 16px; align-items: center; background: #f0f7ff; border-radius: 8px; margin-top: 8px;">
                    <div style="font-weight: 700; color: #0f2f76;">MoM Change</div>
                    <div style="text-align: right; font-size: 14px; font-weight: 600; color: {get_mom_color(us_market_mom)};">{get_mom_sign(us_market_mom)}{us_market_mom/1000000:.1f}M{invisible_dot}</div>
                    <div style="text-align: right; font-size: 14px; font-weight: 600; color: {get_mom_color(us_ai_mom)};">{get_mom_sign(us_ai_mom)}{us_ai_mom/1000000:.1f}M{invisible_dot}</div>
                    <div style="text-align: right; font-size: 14px; font-weight: 600; color: {get_mom_color(us_wrap_mom)};">{get_mom_sign(us_wrap_mom)}{us_wrap_mom/1000000:.1f}M{invisible_dot}</div>
                    <div style="text-align: right; font-size: 14px; font-weight: 600; color: {get_mom_color(kr_leverage_mom)};">{get_mom_sign(kr_leverage_mom)}{kr_leverage_mom/1000000:.1f}M{invisible_dot}</div>
                    <div style="text-align: right; font-size: 14px; font-weight: 600; color: {get_mom_color(kr_sector_mom)};">{get_mom_sign(kr_sector_mom)}{kr_sector_mom/1000000:.1f}M{invisible_dot}</div>
                    <div style="text-align: right; font-size: 16px; font-weight: 700; color: {get_mom_color(total_mom)};">{get_mom_sign(total_mom)}{total_mom/1000000:.1f}M</div>
                </div>
                """
        
        monthly_performance_html += '</div>'
        monthly_performance_html = clean_html(monthly_performance_html)
    else:
        monthly_performance_html = ""

    # === 실현손익 계산 검증 ===
    st.markdown("---")
    st.markdown("### 🔍 실현손익 계산 검증")
    
    # Strategy 1: US Market Index 검증
    st.markdown("#### 1. US Market Index (S&P, 나스닥, TDF)")
    
    verification_data = []
    
    for acct_name in ["ISA", "Pension", "IRP", "US"]:
        df_trade = trade_dfs[acct_name]
        df_cash = cash_df[cash_df["계좌명"] == acct_name]
        
        # 전체 계좌
        df_s_all, s_all = calculate_account_summary(df_trade, df_cash, df_dividend, is_us_stock=(acct_name == "US"))
        
        # 필터링 (S&P, 나스닥, TDF만)
        sp_nasdaq_mask = df_trade["유형"].isin(["S&P", "나스닥", "TDF"])
        df_filtered = df_trade[sp_nasdaq_mask]
        
        if not df_filtered.empty:
            df_s_filtered, s_filtered = calculate_account_summary(df_filtered, df_cash, df_dividend, is_us_stock=(acct_name == "US"))
            
            # 환율 적용
            multiplier = exchange_rate if acct_name == "US" else 1
            
            verification_data.append({
                "계좌": acct_name,
                "전체_평가손익": f"{s_all['current_profit'] * multiplier:,.0f}",
                "전체_실현손익": f"{s_all['actual_profit'] * multiplier:,.0f}",
                "필터_평가손익": f"{s_filtered['current_profit'] * multiplier:,.0f}",
                "필터_실현손익": f"{s_filtered['actual_profit'] * multiplier:,.0f}",
                "필터_총손익": f"{(s_filtered['current_profit'] + s_filtered['actual_profit']) * multiplier:,.0f}"
            })
    
    df_verify = pd.DataFrame(verification_data)
    st.dataframe(df_verify)
    
    # 합계 확인
    st.markdown("**US Market Index 합계:**")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("평가손익", f"{us_market_current_profit:,.0f}")
    with col2:
        st.metric("실현손익", f"{us_market_actual_profit:,.0f}")
    with col3:
        st.metric("총손익", f"{us_market_profit:,.0f}")
    
    # Strategy 2: US AI Power 검증
    st.markdown("#### 2. US AI Power & Grid (전력)")
    
    verification_data_ai = []
    
    for acct_name in ["ISA", "Pension", "IRP", "US"]:
        df_trade = trade_dfs[acct_name]
        df_cash = cash_df[cash_df["계좌명"] == acct_name]
        
        # 필터링 (전력만)
        power_mask = df_trade["유형"] == "전력"
        df_filtered = df_trade[power_mask]
        
        if not df_filtered.empty:
            df_s_filtered, s_filtered = calculate_account_summary(df_filtered, df_cash, df_dividend, is_us_stock=(acct_name == "US"))
            
            multiplier = exchange_rate if acct_name == "US" else 1
            
            verification_data_ai.append({
                "계좌": acct_name,
                "평가손익": f"{s_filtered['current_profit'] * multiplier:,.0f}",
                "실현손익": f"{s_filtered['actual_profit'] * multiplier:,.0f}",
                "총손익": f"{(s_filtered['current_profit'] + s_filtered['actual_profit']) * multiplier:,.0f}"
            })
    
    if verification_data_ai:
        df_verify_ai = pd.DataFrame(verification_data_ai)
        st.dataframe(df_verify_ai)
        
        st.markdown("**US AI Power 합계:**")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("평가손익", f"{us_ai_current_profit:,.0f}")
        with col2:
            st.metric("실현손익", f"{us_ai_actual_profit:,.0f}")
        with col3:
            st.metric("총손익", f"{us_ai_profit:,.0f}")
    
    # 전체 종목별 상세 확인
    st.markdown("#### 3. 종목별 상세 (US Market Index)")
    
    all_holdings = []
    for acct_name in ["ISA", "Pension", "IRP", "US"]:
        df_trade = trade_dfs[acct_name]
        df_cash = cash_df[cash_df["계좌명"] == acct_name]
        
        sp_nasdaq_mask = df_trade["유형"].isin(["S&P", "나스닥", "TDF"])
        df_filtered = df_trade[sp_nasdaq_mask]
        
        if not df_filtered.empty:
            df_s, s = calculate_account_summary(df_filtered, df_cash, df_dividend, is_us_stock=(acct_name == "US"))
            
            if not df_s.empty:
                multiplier = exchange_rate if acct_name == "US" else 1
                for _, row in df_s.iterrows():
                    all_holdings.append({
                        "계좌": acct_name,
                        "종목명": row["종목명"],
                        "유형": row["유형"],
                        "평가금액": int(row["평가금액"] * multiplier),
                        "평가손익": int(row["평가손익"] * multiplier),
                        "수익률": f"{row['수익률(%)']:.2f}%"
                    })
    
    if all_holdings:
        df_holdings = pd.DataFrame(all_holdings)
        st.dataframe(df_holdings)
        
        st.markdown(f"**종목별 평가손익 합계:** {df_holdings['평가손익'].sum():,.0f}")

    # 레이아웃
    col_left, col_right = st.columns([1, 1.3])
    with col_left:
        st.markdown(total_value_html, unsafe_allow_html=True)
        st.markdown(allocation_html, unsafe_allow_html=True)
    with col_right:
        st.markdown(strategy_html, unsafe_allow_html=True)

    # 월간 성과 표시
    if monthly_performance_html:
        st.markdown(monthly_performance_html, unsafe_allow_html=True)

else:
    # 기존 레이아웃 (그대로 유지)
    with st.container():
        col_left, col_right = st.columns([1, 1.2])
        with col_left:
            st.markdown(card_html_profit, unsafe_allow_html=True)
            st.markdown(card_html_balance, unsafe_allow_html=True)
        with col_right:
            st.markdown(card_html_stock, unsafe_allow_html=True)

