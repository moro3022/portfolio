import streamlit as st
import pandas as pd
import FinanceDataReader as fdr
import yfinance as yf
from datetime import datetime, timedelta
from streamlit_option_menu import option_menu
from streamlit_gsheets import GSheetsConnection
from textwrap import dedent

# --- 기본 설정 ---
ACCOUNT_NAMES =  ["ISA", "Pension", "IRP", "ETF", "US", "사주", "LV"]

# --- 구글 시트 연결 ---
conn = st.connection("gsheets", type=GSheetsConnection)

# --- 데이터 불러오기 ---
cash_df = conn.read(worksheet="입출금")
cash_df.columns = cash_df.columns.str.strip()
cash_df["거래일"] = pd.to_datetime(cash_df["거래일"])

# ✅ 환율 시트 불러오기
exchange_df = conn.read(worksheet="환율")
exchange_df.columns = exchange_df.columns.str.strip()
exchange_df["날짜"] = pd.to_datetime(exchange_df["날짜"])
exchange_df = exchange_df.set_index("날짜")

# 각 계좌 시트 불러오기
TRADE_SHEET_NAMES = [name for name in ACCOUNT_NAMES if name not in ["LV"]]

trade_dfs = {
    acct: conn.read(worksheet=acct)
    for acct in TRADE_SHEET_NAMES
}
for acct, df in trade_dfs.items():
    df.columns = df.columns.str.strip()

    # ✅ ISA, Pension만 종목코드 특별 처리
    if acct in ["ISA", "Pension", "사주"]:
        df['종목코드'] = df['종목코드'].astype(str).str.split('.').str[0].str.zfill(6)

    df["거래일"] = pd.to_datetime(df["거래일"])
    df["제세금"] = pd.to_numeric(df["제세금"], errors="coerce").fillna(0)
    df["단가"] = pd.to_numeric(df["단가"], errors="coerce").fillna(0)
    df["수량"] = pd.to_numeric(df["수량"], errors="coerce").fillna(0)
    df["거래금액"] = pd.to_numeric(df["거래금액"], errors="coerce").fillna(0)


    # ✅ 유형 열이 있는 경우에만 처리
    if "유형" in df.columns:
        df["유형"] = df["유형"].fillna("미분류")
    else:
        df["유형"] = "미분류"  # 유형 열이 없으면 전체를 미분류로

# 배당 시트 불러오기
df_dividend = conn.read(worksheet="배당")
df_dividend.columns = df_dividend.columns.str.strip()
df_dividend["배당금"] = pd.to_numeric(df_dividend["배당금"], errors="coerce").fillna(0).astype(int)

# ✅ WRAP 시트에서 K1, M1 셀 읽기
wrap_capital_df = conn.read(worksheet="WRAP", usecols=[10], nrows=1, header=None)
wrap_capital = float(wrap_capital_df.iloc[0, 0]) if not wrap_capital_df.empty else 0

wrap_value_df = conn.read(worksheet="WRAP", usecols=[12], nrows=1, header=None)
wrap_value = float(wrap_value_df.iloc[0, 0]) if not wrap_value_df.empty else 0

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
        # NaN 체크 추가
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
        "actual_profit": round(actual_profit) if pd.notna(actual_profit) else 0,  # 수정된 부분
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
.mix-item { 
    background: #EDEDE9;
    border-radius: 12px;
    padding: 16px 20px;
    margin-top: 12px;
    margin-bottom: 16px;
    display: flex;
    justify-content: space-between;
}                  

</style>
""", unsafe_allow_html=True)

# --- Streamlit 탭 구성 ---
st.markdown("""
<div style='font-size:32px; font-weight:bold; margin-bottom:16px;'>Dashboard</div>
""", unsafe_allow_html=True)

# --- 커스텀 탭 디자인  ---
ACCOUNT_NAMES = ["전체", "ISA", "Pension", "IRP", "ETF", "US", "MIX"]
green_color = "#3A866A"
red_color = "#C54E4A"

# 옵션 메뉴 사용
selected_tab = option_menu(
    menu_title=None,
    options=ACCOUNT_NAMES,
    icons=["back", "geo-alt-fill","geo-alt-fill","geo-alt-fill","geo-alt-fill","geo-alt-fill","grid-1x2-fill"],
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
    "ISA": "#B9CCD9",      # 블루그레이
    "Pension": "#F6CD7D",  # 머스타드
    "IRP": "#C8D9A2",      # 올리브
    "ETF": "#F6C793",      # 살구
    "전체": "#EDE5D9",      # 기본 회색 (또는 투명)
    "US": "#F7B7A3",
    "ESOP": "#D6B8F9",
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

# 탭 선택에 따른 데이터 분기 설정-
if acct == "전체":
    df_summary = pd.concat(df_summary_list, ignore_index=True)
    summary = local_summary
    
    # 유형별 집계 (로컬 계좌만)
    df_local = df_summary[["유형", "매입금액", "평가금액"]].copy()

    # US 계좌 요약 계산
    df_trade_us = trade_dfs["US"]
    df_cash_us = cash_df[cash_df["계좌명"] == "US"]
    df_us_summary, _ = calculate_account_summary(df_trade_us, df_cash_us, df_dividend)

    # 환율 시트의 최신 기준환율 (제일 마지막 행)
    latest_fx = float(pd.to_numeric(exchange_df["기준환율"].iloc[-1], errors="coerce"))

    if not df_us_summary.empty:
        df_us_krw = df_us_summary[["유형", "매입금액", "평가금액"]].copy()
        df_us_krw[["매입금액","평가금액"]] = (
            df_us_krw[["매입금액","평가금액"]]
            .apply(pd.to_numeric, errors="coerce").fillna(0)
            * latest_fx
        ).round().astype(int)
        df_all = pd.concat([df_local, df_us_krw], ignore_index=True)
    else:
        df_all = df_local

    # ✅ LV 데이터 추가
    lv_cash = cash_df[cash_df["계좌명"] == "LV"]
    lv_balance = lv_cash[lv_cash["구분"] == "입금"]["금액"].sum() - lv_cash[lv_cash["구분"] == "출금"]["금액"].sum()
    
    lv_df = conn.read(worksheet="LV")
    lv_df.columns = lv_df.columns.str.strip()
    lv_profit = pd.to_numeric(lv_df["손익"], errors="coerce").sum()
    lv_value = lv_balance + lv_profit
    
    df_lv = pd.DataFrame({
        "유형": ["코스피"],
        "매입금액": [lv_balance],
        "평가금액": [lv_value]
    })

    # ✅ ESOP 데이터 추가
    df_trade_esop = trade_dfs["사주"]
    df_cash_esop = cash_df[cash_df["계좌명"] == "사주"]
    _, summary_esop = calculate_account_summary(df_trade_esop, df_cash_esop, df_dividend)
    
    df_esop = pd.DataFrame({
        "유형": ["금융"],
        "매입금액": [summary_esop["capital"]],
        "평가금액": [summary_esop["current_value"]]
    })

    # ✅ WRAP 데이터 추가
    df_wrap = pd.DataFrame({
        "유형": ["WRAP"],
        "매입금액": [wrap_capital],
        "평가금액": [wrap_value]
    })

    # ✅ 전체 데이터 합치기
    df_all = pd.concat([df_all, df_lv, df_esop, df_wrap], ignore_index=True)

    # 유형별 집계
    weighting_summary = (
        df_all.groupby("유형", as_index=False)
             .agg({"매입금액":"sum","평가금액":"sum"})
             .sort_values("평가금액", ascending=False)
    )

elif acct == "MIX":
    local_accounts = ["ISA", "ETF"]
    local_value, local_cash = 0, 0
    for a in local_accounts:
        df_trade = trade_dfs[a]
        df_cash = cash_df[cash_df["계좌명"] == a]
        _, s = calculate_account_summary(df_trade, df_cash, df_dividend)
        local_value += s["current_value"]
        local_cash += s["cash"]

    # Pension: Pension + IRP
    pension_accounts = ["Pension", "IRP"]
    pension_value, pension_cash = 0, 0
    for a in pension_accounts:
        df_trade = trade_dfs[a]
        df_cash = cash_df[cash_df["계좌명"] == a]
        _, s = calculate_account_summary(df_trade, df_cash, df_dividend)
        pension_value += s["current_value"]
        pension_cash += s["cash"]

    # Deposit: Local + Pension의 현금 합
    deposit = local_cash + pension_cash

    # US
    df_trade_us = trade_dfs["US"]
    df_cash_us = cash_df[cash_df["계좌명"] == "US"]
    _, summary_us = calculate_account_summary(df_trade_us, df_cash_us, df_dividend)
    us_value = summary_us["current_value"] + summary_us["cash"]
    us_profit = summary_us["total_profit"]

    # ESOP
    df_trade_esop = trade_dfs["사주"]
    df_cash_esop = cash_df[cash_df["계좌명"] == "사주"]
    _, summary_esop = calculate_account_summary(df_trade_esop, df_cash_esop, df_dividend)
    esop_value = summary_esop["current_value"]
    esop_profit = summary_esop["total_profit"]

    # LV
    lv_cash = cash_df[cash_df["계좌명"] == "LV"]
    lv_balance = lv_cash[lv_cash["구분"] == "입금"]["금액"].sum() - lv_cash[lv_cash["구분"] == "출금"]["금액"].sum()

    lv_df = conn.read(worksheet="LV")
    lv_df.columns = lv_df.columns.str.strip()
    lv_profit = pd.to_numeric(lv_df["손익"], errors="coerce").sum()

    lv_value = lv_balance + lv_profit

    # Savings
    parking_df = cash_df[cash_df["계좌명"] == "파킹"]
    savings = parking_df[parking_df["구분"] == "입금"]["금액"].sum() - parking_df[parking_df["구분"] == "출금"]["금액"].sum()
    housing_df = cash_df[cash_df["계좌명"] == "청약"]
    housing = housing_df[housing_df["구분"] == "입금"]["금액"].sum() - housing_df[housing_df["구분"] == "출금"]["금액"].sum()

elif acct == "OTH":
    # OTH 처리 로직 (US, 사주 데이터 계산)
    df_trade_us = trade_dfs["US"]
    df_cash_us = cash_df[cash_df["계좌명"] == "US"]
    df_us, summary_us = calculate_account_summary(df_trade_us, df_cash_us, df_dividend)

    df_trade_esop = trade_dfs["사주"]
    df_cash_esop = cash_df[cash_df["계좌명"] == "사주"]
    df_esop, summary_esop = calculate_account_summary(df_trade_esop, df_cash_esop, df_dividend) 

else:
    df_trade = trade_dfs[acct]
    df_cash = cash_df[cash_df["계좌명"] == acct]
    df_summary, summary = calculate_account_summary(df_trade, df_cash, df_dividend)


if acct not in ["MIX", "OTH"]: # MIX, OTH가 아닐 때만 summary 값 사용
    total_profit = summary["current_profit"] + summary["actual_profit"]
    total_profit_rate = summary["total_profit_rate"]
    today_profit = summary["today_profit"] 

    current_profit = summary["current_profit"]
    
    # ✅ 빈 DataFrame 체크 추가
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

if acct == "MIX":
    total_stock = local_value + pension_value + us_value + esop_value
    total_cash = savings + deposit + housing
    total_asset = total_stock + total_cash

# --- 레이아웃 시작 ---
    
# 1. Profit 카드

icon_book = "https://cdn-icons-png.flaticon.com/128/16542/16542648.png"
icon_wallet = "https://cdn-icons-png.flaticon.com/128/19011/19011999.png"

# 1. 통화 표시 변수 추가 (탭 선택 후 추가)

if selected_tab not in ["MIX", "OTH"]:
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
        "ISA": 40000000,
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

    # ✅ US 탭: 2025년 실현손익 계산
    if acct == "US":
        usdkrw_history = fdr.DataReader('USD/KRW')
        
        df_trade_us = trade_dfs["US"]
        realized_profit_2025_krw = 0
        
        # 환율 가져오기 함수
        def get_exchange_rate(trade_date, days_offset=2):
            settlement_date = trade_date + pd.Timedelta(days=days_offset)
            while settlement_date.weekday() >= 5:
                settlement_date += pd.Timedelta(days=1)
            
            try:
                # 정산일 이후 가장 가까운 날짜의 환율 사용
                available_dates = exchange_df.index[exchange_df.index >= settlement_date]
                if len(available_dates) > 0:
                    exchange_date = available_dates[0]
                    return exchange_df.loc[exchange_date, '기준환율'], settlement_date
                else:
                    # 미래 날짜면 최신 환율 사용
                    return exchange_df.iloc[-1]['기준환율'], settlement_date
            except:
                return exchange_df.iloc[-1]['기준환율'], settlement_date
        
        # 종목별로 실현손익 계산
        for code, group in df_trade_us.groupby("종목코드"):
            group = group.sort_values("거래일").copy()
            
            # FIFO 방식으로 매수 이력 관리
            buy_queue = []  # [(수량, 단가, 거래금액, 제세금, 환율)]
            
            for _, row in group.iterrows():
                qty = row["수량"]
                price = row["단가"]
                fee = row["제세금"]
                amt = row["거래금액"]
                trade_date = row["거래일"]
                
                if row["구분"] == "매수":
                    # 매수 시점 환율
                    buy_exchange_rate, _ = get_exchange_rate(trade_date)
                    buy_queue.append({
                        "qty": qty,
                        "price": price,
                        "amount": amt,
                        "fee": fee,
                        "exchange_rate": buy_exchange_rate
                    })
                
                else:  # 매도
                    # 매도 시점 환율 및 정산일
                    sell_exchange_rate, settlement_date = get_exchange_rate(trade_date)
                    
                    # 2025년 귀속 여부 확인
                    if settlement_date.year == 2025:
                        remaining_sell_qty = qty
                        sell_amount_krw = (amt - fee) * sell_exchange_rate  # 매도금액(원화)
                        
                        # FIFO로 매수 원가 계산
                        buy_cost_krw = 0
                        while remaining_sell_qty > 0 and len(buy_queue) > 0:
                            buy_item = buy_queue[0]
                            
                            if buy_item["qty"] <= remaining_sell_qty:
                                # 해당 매수 건 전체 소진
                                buy_cost_krw += (buy_item["amount"] + buy_item["fee"]) * buy_item["exchange_rate"]
                                remaining_sell_qty -= buy_item["qty"]
                                buy_queue.pop(0)
                            else:
                                # 해당 매수 건 일부만 사용
                                ratio = remaining_sell_qty / buy_item["qty"]
                                buy_cost_krw += (buy_item["amount"] + buy_item["fee"]) * ratio * buy_item["exchange_rate"]
                                buy_item["qty"] -= remaining_sell_qty
                                buy_item["amount"] *= (1 - ratio)
                                buy_item["fee"] *= (1 - ratio)
                                remaining_sell_qty = 0
                        
                        # 실현손익(원화)
                        profit_krw = sell_amount_krw - buy_cost_krw
                        realized_profit_2025_krw += profit_krw
                    else:
                        # 2025년 귀속 아니지만 큐에서는 차감
                        remaining_sell_qty = qty
                        while remaining_sell_qty > 0 and len(buy_queue) > 0:
                            buy_item = buy_queue[0]
                            if buy_item["qty"] <= remaining_sell_qty:
                                remaining_sell_qty -= buy_item["qty"]
                                buy_queue.pop(0)
                            else:
                                ratio = remaining_sell_qty / buy_item["qty"]
                                buy_item["qty"] -= remaining_sell_qty
                                buy_item["amount"] *= (1 - ratio)
                                buy_item["fee"] *= (1 - ratio)
                                remaining_sell_qty = 0
        
        # 2025년 실현손익 바 생성
        profit_color = green_color if realized_profit_2025_krw >= 0 else red_color
        limit_remaining = 2500000 - realized_profit_2025_krw
        limit_percent = (realized_profit_2025_krw / 2500000 * 100) if realized_profit_2025_krw > 0 else 0
        bar2_html = get_bar(min(limit_percent, 100), color=theme_color)
        
        limit_html = f"""
            <div class="custom-divider"></div>
            <div style="display:flex; justify-content:space-between; align-items:center; font-weight:555; font-size:18px; color:#555; margin-top:8px;">
                <div style="margin-left:5px;">2025 Actual Profit (KRW)</div>
                <div style="margin-right:5px; font-weight:700; color:{profit_color};">{realized_profit_2025_krw:,.0f}</div>
            </div>
            {bar2_html}
            <div style="display:flex; justify-content:space-between; font-size:14px; margin-top:-8px; margin-bottom:8px;">
                <div style="color:#555; font-weight:600; margin-left:5px;">{limit_percent:.0f}%</div>
                <div style="color:#555; margin-right:5px;">잔여: {limit_remaining:,.0f}</div>
            </div>
        """.strip()


    # ✅ 조건부 추가 HTML
    elif acct in ["ISA", "Pension", "IRP"] and limit > 0:
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

    # ✅ 항상 완성된 HTML 구조 유지
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

# 총 평가금액 + 평가수익률 카드 시작
        
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
    if not df_summary.empty:  # ✅ 빈 DataFrame 체크 추가
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
    else:  # ✅ 보유종목이 없는 경우 메시지 추가
        card_html_stock += """
        <div style="text-align: center; padding: 40px; color: #999; font-size: 18px;">
            보유중인 종목이 없습니다
        </div>
        """

# 전체 탭 : Weighting 카드 HTML
if selected_tab == "전체":
    card_html_weighting = f"""
    <div class="card">
        <div class="card-title"><span style="color:{theme_color};">●</span><span style="margin-left:6px;">Weighting</span></div>
    """

    _ws = weighting_summary.copy()
    _ws["매입금액"] = pd.to_numeric(_ws["매입금액"], errors="coerce").fillna(0)
    _ws["평가금액"] = pd.to_numeric(_ws["평가금액"], errors="coerce").fillna(0)
    _ws["수익금"] = _ws["평가금액"] - _ws["매입금액"]
    _ws["수익률"] = _ws.apply(lambda r: (r["수익금"]/r["매입금액"]*100) if r["매입금액"]>0 else 0.0, axis=1)

    total_purchase = float(_ws["매입금액"].sum())
    total_value = float(_ws["평가금액"].sum())
    total_profit = float(_ws["수익금"].sum())
    total_rate = (total_profit / total_purchase * 100) if total_purchase > 0 else 0.0

    _ws = _ws.sort_values("평가금액", ascending=False).reset_index(drop=True)

    for _, row in _ws.iterrows():
        asset_type = str(row["유형"])
        purchase = int(round(row["매입금액"]))
        current = int(round(row["평가금액"]))
        profit = int(round(row["수익금"]))
        rate = float(row["수익률"])
        weight_pct = (row["평가금액"] / total_value * 100) if total_value > 0 else 0.0
        pf_color = green_color if profit >= 0 else red_color
        pfr_color = '#5BA17B' if profit >= 0 else red_color

        card_html_weighting += dedent(f"""
        <div class="stock-item" style="display:flex;align-items:center;justify-content:space-between;margin:10px 0;">
            <div style="flex:1;font-weight:600;font-size:16px;color:#333;">{asset_type}</div>
            <div style="flex:1;text-align:right;font-size:16px;font-weight:500;color:#555;">{weight_pct:.1f}%</div>
            <div style="flex:2;text-align:right;line-height:20px;">
                <div style="font-size:16px;font-weight:700;color:#555;">{current:,.0f}</div>
                <div style="font-size:15px;font-weight:500; color:#777;">{purchase:,.0f}</div>
            </div>
            <div style="flex:2;text-align:right;line-height:20px;">
                <div style="font-size:16px;font-weight:700;color:{pf_color};">{profit:,.0f}</div>
                <div style="font-size:15px;font-weight:500;color:{pfr_color};">{rate:.1f}%</div>
            </div>
        </div>
        """)

    total_color = green_color if total_profit >= 0 else red_color
    ttr_color = '#5BA17B' if total_profit >= 0 else red_color
    card_html_weighting += dedent(f"""
    <div class="stock-item" style="display:flex;align-items:center;justify-content:space-between;margin:6px 0;border-bottom:none;">
        <div style="flex:1;font-weight:800;font-size:18px;color:#333;">Total</div>
        <div style="flex:1;text-align:right;font-size:15px;font-weight:700;color:#555;">100%</div>
        <div style="flex:2;text-align:right;line-height:20px;">
            <div style="font-size:16px;font-weight:700;color:#555;">{total_value:,.0f}</div>
            <div style="font-size:15px;font-weight:500; color:#666;">{total_purchase:,.0f}</div>
        </div>
        <div style="flex:2;text-align:right;line-height:20px;">
            <div style="font-size:16px;font-weight:700;color:{total_color};">{total_profit:,.0f}</div>
            <div style="font-size:15px;font-weight:500;color:{ttr_color};">{total_rate:.2f}%</div>
        </div>
    </div>
    """)

    card_html_weighting += "</div>"


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
            "보유수량": [0],  # 수량은 의미없으므로 0
            "평균단가": [0],
            "현재가": [0],
            "평가금액": [tdf_total_value],
            "매입금액": [tdf_rows["매입금액"].sum()],
            "평가손익": [tdf_rows["평가손익"].sum()],
            "수익률(%)": [0]  # 필요시 계산 가능
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

# 4. OTH 탭 --

from textwrap import dedent

def make_holdings_card(df_summary, summary, theme_color, title="Holdings", currency="", green_color="#3A866A", red_color="#C54E4A", footer_key=None, footer_label=None):
    icon_today = "https://cdn-icons-png.flaticon.com/128/876/876754.png"
    icon_total = "https://cdn-icons-png.flaticon.com/128/13110/13110858.png"

    today_profit = summary["today_profit"]
    current_profit = summary["current_profit"]
    current_value = summary["current_value"]
    today_profit_plus = f"{currency}{today_profit:,.0f}" if today_profit > 0 else "&nbsp;"

    # 상단 카드
    card_html_stock = dedent(f"""
    <div class="card">
        <div class="card-title"><span style= "color: {theme_color}";>●</span><span style="margin-left: 6px;">{title}</span></div>
        <div class="card-value" style="display: flex; justify-content: space-between; align-items: center;">
            <div>{current_value:,.0f}</div>
        </div>
        <div class="card-item" style="padding: 5px 15px; background: #EDEDE9;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 10px; margin-bottom: 10px;">
            <div style="line-height: 24px;">
                <img src="{icon_today}" width="20" height="20" style="vertical-align: -3px; margin-left: 5px;"/>
                <span style="margin-left:5px; font-size: 20px; color: #2E7850; font-weight:600;">{today_profit_plus}</span>
            </div>
            <div style="text-align: right; line-height: 24px;">
                    <div style="display: flex; align-items: flex-start;">
                        <img src="{icon_total}" width="20" height="20" style="margin-right:15px;"/>
                        <div style="display: flex; flex-direction: column; justify-content: center; line-height: 20px; gap:4px; margin-right: 3px;">
                            <span style="font-size: 20px; font-weight: bold; color:{green_color if current_profit >= 0 else red_color};">
                                {currency}{current_profit:,.0f}
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

    # ✅ 빈 DataFrame 체크 추가
    if not df_summary.empty:
        # 종목별 루프
        for _, row in df_summary.sort_values("평가금액", ascending=False).iterrows():
            name = row["종목명"]
            profit = row["평가손익"]
            profit_rate = row["수익률(%)"]
            stock_value = row["평가금액"]
            purchase_value = row["매입금액"]
            qty = row["보유수량"]
            avg_price = row["평균단가"]
            current_price = row["현재가"]
            icon_html = icon_up(size=24) if profit >= 0 else icon_down(size=24)

            card_html_stock += dedent(f"""
            <div class="stock-item" style="display: flex; justify-content: space-between; align-items: center; margin-bottom:10px;">
                <div style="flex: 2.5; display: flex; align-items: center; gap: 10px; min-width: 0;" >
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
                        @ {current_price:,.0f}
                    </div>
                    <div style="font-size: 14px; font-weight: 500; color:#666; line-height: 22px;">
                        @ {avg_price:,.0f}
                    </div>
                </div>
                <div style="flex: 1.5; text-align: right; display: flex; flex-direction: column; justify-content: center; gap:4px;">
                    <div style="font-size: 14px; font-weight: 500; color:#666; line-height: 22px;">
                        {currency}{stock_value:,.0f}
                    </div>
                    <div style="font-size: 14px; font-weight: 500; color:#666; line-height: 22px;">
                        {currency}{purchase_value:,.0f}
                    </div>
                </div>
                <div style="flex: 1.5; text-align: right; display: flex; flex-direction: column; justify-content: center; gap:4px;">
                    <div style="font-size: 18px; font-weight: bold; color:{green_color if profit >= 0 else red_color}; line-height: 22px;">
                        {currency}{profit:,.0f}
                    </div>
                    <div style="font-size: 16px; font-weight: 500; color:{'#5BA17B' if profit >= 0 else red_color}; line-height: 22px;">
                        {profit_rate:.1f}%
                    </div>
                </div>
            </div>
            """)
    else:
        # ✅ 보유종목이 없는 경우 메시지 표시
        card_html_stock += """
        <div style="text-align: center; padding: 40px; color: #999; font-size: 18px;">
            보유중인 종목이 없습니다
        </div>
        """

    # ✅ Actual Profit 푸터 추가
    if footer_key and footer_key in summary:
        card_html_stock += f"""
        <div class="card-item" style="background:white; display:flex; justify-content:space-between; align-items:center; margin-top:6px;">
            <div class="item-label">{footer_label}</div>
            <div class="item-return">{currency}{summary[footer_key]:,.0f}</div>
        </div>
        """.strip()

    card_html_stock += "</div>"
    return card_html_stock

# 5. MIX 탭 --
if selected_tab == "MIX":
    usdkrw = fdr.DataReader('USD/KRW')
    exchange_rate = usdkrw['Close'].iloc[-1]
    us_value_krw = us_value * exchange_rate

    lv_df = conn.read(worksheet="LV")
    lv_df.columns = lv_df.columns.str.strip()

    lv_profit = pd.to_numeric(lv_df["손익"], errors="coerce").sum()
    us_profit = summary_us["total_profit"]
    esop_profit = summary_esop["total_profit"]
    mix_total_profit = local_total_summary["total_profit"] + us_profit + esop_profit + lv_profit

    total_stock = local_value + pension_value + us_value_krw + esop_value + lv_value
    total_cash = savings + housing + deposit
    total_asset = total_stock + total_cash

    cash_ratio = total_cash / total_asset
    stock_ratio = 1 - cash_ratio

    icon_asset = "https://cdn-icons-png.flaticon.com/128/3914/3914398.png"
    icon_stock = "https://cdn-icons-png.flaticon.com/128/15852/15852070.png"
    icon_cash = "https://cdn-icons-png.flaticon.com/128/7928/7928197.png"

    card_html_mix_total = f"""
    <div class="card" style="display: flex; align-items: center; gap: 12px; background: #F4C2C2; padding: 20px 24px; border-radius: 12px;">
        <img src="{icon_asset}" width="50" height="50" style="margin-right:10px;" />
        <div>
            <div class="card-title">Total Asset</div>
            <div class="card-value">{total_asset:,.0f}</div>
        </div>
    </div>
    """

    card_html_mix_stock = f"""
    <div class="card" style="display: flex; align-items: center; gap: 12px; background: #B7D9C8; padding: 20px 24px; border-radius: 12px;">
        <img src="{icon_stock}" width="50" height="50" style="margin-right:10px;" />
        <div>
            <div style="display: flex; align-items: center; gap: 8px;">
                <div class="card-title">Stock</div>
                <div style="background-color: #A0C4B2; color: #3A5A4A; font-size: 16px; font-weight: bold; padding: 2px 10px; border-radius: 8px;">
                    {stock_ratio * 100:,.1f}%
                </div>
            </div>
            <div class="card-value">{total_stock:,.0f}</div>
        </div>
    </div>
    """

    card_html_mix_cash = f"""
    <div class="card" style="display: flex; align-items: center; gap: 12px; background: #FBE8C6; padding: 20px 24px; border-radius: 12px;">
        <img src="{icon_cash}" width="50" height="50" style="margin-right:10px;" />
        <div>
            <div style="display: flex; align-items: center; gap: 8px;">
                <div class="card-title">Cash</div>
                <div style="background-color: #EACD9E; color: #85642E; font-size: 16px; font-weight: bold; padding: 2px 10px; border-radius: 8px;">
                    {cash_ratio * 100:.1f}%
                </div>
            </div>
            <div class="card-value">{total_cash:,.0f}</div>
        </div>
    </div>
    """

    card_html_mix_total_detail = f"""
    <div class="card" style="padding: 24px; border-radius: 12px;">
    <div class="mix-item" style="display: flex; justify-content: space-between; align-items: baseline;">
        <div class="item-label">Profit</div>
        <div class="item-return">{mix_total_profit:,.0f}</div>
    </div>
    </div>
    """

    card_html_mix_stock_detail = f"""
    <div class="card" style="padding: 24px; border-radius: 12px;">
    <div class="mix-item" style="display: flex; justify-content: space-between; align-items: baseline;">
        <div class="item-label">Local</div>
        <div class="item-return">{local_value:,.0f}</div>
    </div>
        <div class="mix-item" style="display: flex; justify-content: space-between; align-items: baseline;">
        <div class="item-label">Pension</div>
        <div class="item-return">{pension_value:,.0f}</div>
    </div>
    <div class="mix-item" style="display: flex; justify-content: space-between; align-items: baseline;">
        <div class="item-label">LV</div>
        <div class="item-return">{lv_value:,.0f}</div>
    </div>
    <div class="mix-item" style="display: flex; justify-content: space-between; align-items: baseline;">
        <div class="item-label">ESOP</div>
        <div class="item-return">{esop_value:,.0f}</div>
    </div>
    <div class="mix-item" style="display: flex; justify-content: space-between; align-items: baseline;">
        <div class="item-label">US</div>
        <div class="item-return">$ {us_value:,.0f}</div>
    </div>
    </div>
    """

    card_html_mix_cash_detail = f"""
    <div class="card" style="padding: 24px; border-radius: 12px;">
    <div class="mix-item" style="display: flex; justify-content: space-between; align-items: baseline;">
        <div class="item-label">Bank</div>
        <div class="item-return">{savings:,.0f}</div>
    </div>
    <div class="mix-item" style="display: flex; justify-content: space-between; align-items: baseline;">
        <div class="item-label">Housing</div>
        <div class="item-return">{housing:,.0f}</div>
    </div>
    <div class="mix-item" style="display: flex; justify-content: space-between; align-items: baseline;">
        <div class="item-label">Trading</div>
        <div class="item-return">{deposit:,.0f}</div>
    </div>
    </div>
    """


# -- 레이아웃 구성 -- #

if selected_tab == "MIX":
    row1_col1, row1_col2, row1_col3 = st.columns(3)
    with row1_col1:
        st.markdown(card_html_mix_total, unsafe_allow_html=True)
    with row1_col2:
        st.markdown(card_html_mix_stock, unsafe_allow_html=True)
    with row1_col3:
        st.markdown(card_html_mix_cash, unsafe_allow_html=True)

    row2_col1, row2_col2, row2_col3 = st.columns(3)
    with row2_col1:
        st.markdown(card_html_mix_total_detail, unsafe_allow_html=True)
    with row2_col2:
        st.markdown(card_html_mix_stock_detail, unsafe_allow_html=True)
    with row2_col3:
        st.markdown(card_html_mix_cash_detail, unsafe_allow_html=True)

if selected_tab == "OTH":
    # US
    df_trade_us = trade_dfs["US"]
    df_cash_us = cash_df[cash_df["계좌명"] == "US"]
    df_us, summary_us = calculate_account_summary(df_trade_us, df_cash_us, df_dividend)

    # ESOP
    df_trade_esop = trade_dfs["사주"]
    df_cash_esop = cash_df[cash_df["계좌명"] == "사주"]
    df_esop, summary_esop = calculate_account_summary(df_trade_esop, df_cash_esop, df_dividend)

    col1, col2 = st.columns([1, 1])
    with col1:
        st.markdown(make_holdings_card(df_esop, summary_esop, "#F7B7A3", title="ESOP"), unsafe_allow_html=True)
    with col2:
        st.markdown(make_holdings_card(df_us, summary_us, "#D6B8F9", title="US", currency="$ ", footer_key="actual_profit", footer_label="Realized P&L"), unsafe_allow_html=True)

if selected_tab == "전체":
    with st.container():
        col_left, col_right = st.columns([1, 1.2])
        with col_left:
            st.markdown(card_html_profit, unsafe_allow_html=True)
            st.markdown(card_html_balance, unsafe_allow_html=True)
            st.markdown(card_html_weighting, unsafe_allow_html=True)  # ✅ 추가
        with col_right:
            st.markdown(card_html_stock, unsafe_allow_html=True)

if selected_tab not in ["MIX", "OTH", "전체"]: 
    with st.container():
        col_left, col_right = st.columns([1, 1.2])
        with col_left:
            st.markdown(card_html_profit, unsafe_allow_html=True)
            st.markdown(card_html_balance, unsafe_allow_html=True)
        with col_right:
            st.markdown(card_html_stock, unsafe_allow_html=True)