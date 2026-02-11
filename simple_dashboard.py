import streamlit as st
import plotly.graph_objects as go
import asyncio
from datetime import datetime
import time

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from energy_common import (
    TIMEZONE, BATTERY_CAPACITY_KWH, BATTERY_MIN_SOC,
    get_setting as _base_get_setting,
    fetch_sems_data,
    process_chart_data,
)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

def get_setting(key, default=None):
    """Extend common get_setting with Streamlit Secrets support."""
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return _base_get_setting(key, default)

# Page Config
st.set_page_config(
    page_title="Live Energy Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# -----------------------------------------------------------------------------
# Data Fetching
# -----------------------------------------------------------------------------

@st.cache_resource(ttl=55)
def get_sems_data():
    """Fetch today's chart data and realtime status from SEMS."""
    if not get_setting("SEMS_ACCOUNT") or not get_setting("SEMS_PASSWORD") or not get_setting("SEMS_STATION_ID"):
        st.error("Missing Configuration: Please set SEMS_ACCOUNT, SEMS_PASSWORD, and SEMS_STATION_ID in .streamlit/secrets.toml or config.py")
        return None, None
    return asyncio.run(fetch_sems_data(get_setting_fn=get_setting))

# -----------------------------------------------------------------------------
# Main Application
# -----------------------------------------------------------------------------

# Title with Date
st.title(f"🌞 Live Energy Dashboard - {datetime.now(ZoneInfo(TIMEZONE)).strftime('%A %d %B')}")

# Fetch Data
realtime, chart_json = get_sems_data()

# Process Data
df = None
if chart_json and not chart_json.get("error"):
    df = process_chart_data(chart_json)

# Error Handling
if realtime and realtime.error_message:
    st.error(f"⚠️ Live Status Error: {realtime.error_message}")
if chart_json and chart_json.get("error"):
    st.error(f"⚠️ Chart Data Error: {chart_json.get('error')}")

# Display Dashboard
if df is not None and not df.empty:
    
    # --- CALCULATE AGGREGATES ---
    pv_total = df["pv_energy_kwh"].sum()
    solar_benefit = df["solar_benefit"].sum()
    
    batt_discharge = df["battery_discharge_kwh"].sum()
    batt_charge = df["battery_charge_kwh"].sum()
    batt_benefit = df["battery_net_benefit"].sum()
    # Use live SOC if available, otherwise last chart value
    current_soc = realtime.battery_soc if realtime else df["soc"].iloc[-1]
    
    import_total = df["grid_import_kwh"].sum()
    export_total = df["grid_export_kwh"].sum()
    grid_cost_gross = df["grid_cost"].sum()
    export_earnings = df["export_income"].sum()
    net_grid_cost = grid_cost_gross - export_earnings
    
    load_total = df["load_energy_kwh"].sum()

    # --- LATEST SNAPSHOT ---
    st.subheader("⚡ Live Snapshot")
    
    # helper for pastel cards
    def card(title, value, sub_value, bg_color, text_color="black"):
        st.markdown(f"""
        <div style="background-color: {bg_color}; padding: 10px; border-radius: 10px; color: {text_color};">
            <h4 style="margin:0; opacity: 0.7; font-size: 0.9rem;">{title}</h4>
            <h2 style="margin:0; font-size: 2.5rem;">{value}</h2>
            <p style="margin:0; opacity: 0.8; font-size: 0.8rem;">{sub_value}</p>
        </div>
        """, unsafe_allow_html=True)

    l1, l2, l3, l4 = st.columns(4)

    # 1. Solar
    # Solar is always generated (positive or zero) -> pastel yellow/orange
    if realtime:
        with l1:
            pv_kw = realtime.pv_power / 1000
            card("☀️ Solar", f"{pv_kw:.2f} kW", "Producing...", "#FFF9C4") # Pastel Yellow

        # 2. Battery
        # Live Data: Pos = Charge, Neg = Discharge
        # User wants "Good" (Charging) = Green, "Bad" (Discharging) = Red
        with l2:
            batt_w = realtime.battery_power
            batt_kw = abs(batt_w) / 1000
            
            if batt_w > 10: # Charging
                bg = "#C8E6C9" # Pastel Green
                status = "Charging..."
            elif batt_w < -10: # Discharging
                bg = "#FFCCBC" # Pastel Red/Orange
                status = "Discharging..."
            else:
                bg = "#F5F5F5" # Grey
                status = "Idle"
            
            card("🔋 Battery", f"{batt_kw:.2f} kW ({current_soc}%)", status, bg)

        # 3. Grid
        # Live Data: Pos = Import, Neg = Export
        # User wants "Good" (Exporting) = Green, "Bad" (Importing) = Red
        # Small values (<50W) are considered residual/neutral
        with l3:
            grid_w = realtime.grid_power
            grid_kw = abs(grid_w) / 1000
            
            if grid_w < -50: # Exporting (Good)
                bg = "#C8E6C9" # Pastel Green
                status = "Exporting..."
            elif grid_w > 50: # Importing (Bad)
                bg = "#FFCCBC" # Pastel Red/Orange
                status = "Importing..."
            else:
                bg = "#F5F5F5" # Grey
                if grid_w > 0:
                    status = "Residual import"
                elif grid_w < 0:
                    status = "Residual export"
                else:
                    status = "Idle"
                
            card("🔌 Grid", f"{grid_kw:.2f} kW", status, bg)

        # 4. Load
        # Consumption -> Pastel Blue
        with l4:
            load_kw = realtime.load_power / 1000
            card("🏠 Load", f"{load_kw:.2f} kW", "Consuming...", "#BBDEFB") # Pastel Blue

    st.divider()

    # --- DAILY TOTALS ---
    st.subheader("📅 Today's Totals")
    c1, c2, c3, c4 = st.columns(4)
    
    with c1:
        st.markdown("### ☀️ Solar")
        st.metric("Generation", f"{pv_total:.1f} kWh")
        
        # Total solar economic benefit (Savings + Earnings)
        total_solar_val = solar_benefit + export_earnings
        st.metric("Solar Benefit", f"${total_solar_val:.2f}", 
                 help=f"Savings from Home Usage: ${solar_benefit:.2f}\nEarnings from Grid Export: ${export_earnings:.2f}")

        # Breakdown of where solar went
        solar_home_kwh = df["solar_to_load_kwh"].sum()
        solar_grid_kwh = df["grid_export_kwh"].sum()
        
        # More prominent breakdown
        st.markdown(f"""
        <div style="font-size: 0.9em; margin-top: 5px;">
        <div>🏠 <b>Home Usage:</b> {solar_home_kwh:.1f} kWh <span style='color:green'>(${solar_benefit:.2f})</span></div>
        <div>🌐 <b>Grid Export:</b> {solar_grid_kwh:.1f} kWh <span style='color:green'>(${export_earnings:.2f})</span></div>
        </div>
        """, unsafe_allow_html=True)
    
    with c2:
        st.markdown("### 🔋 Battery")
        stored_kwh = max(0.0, (current_soc - BATTERY_MIN_SOC) / (100.0 - BATTERY_MIN_SOC)) * BATTERY_CAPACITY_KWH
        st.metric("SOC", f"{current_soc}% ({stored_kwh:.1f} kWh)")
        st.metric("Battery Benefit", f"${batt_benefit:.2f}", 
                help=f"Savings (Avoided Grid Cost): ${df['battery_discharge_benefit'].sum():.2f}\nCharge Cost (Solar Export Foregone): -${df['battery_charge_cost'].sum():.2f}")
        
        # More prominent breakdown
        st.markdown(f"""
        <div style="font-size: 0.9em; margin-top: 5px;">
        <div>📉 <b>Discharge:</b> {batt_discharge:.1f} kWh (${df['battery_discharge_benefit'].sum():.2f})</div>
        <div>📈 <b>Charge:</b> {batt_charge:.1f} kWh (-${df['battery_charge_cost'].sum():.2f})</div>
        </div>
        """, unsafe_allow_html=True)

    with c3:
        st.markdown("### 🔌 Grid")
        st.metric("Grid Net Cost", f"${net_grid_cost:.2f}", delta_color="inverse")
        st.metric("Import / Export", f"{import_total:.1f} / {export_total:.1f} kWh")
        
        # More prominent breakdown
        st.markdown(f"""
        <div style="font-size: 0.9em; margin-top: 5px;">
        <div>💸 <b>Cost:</b> <span style='color:#D32F2F'>${grid_cost_gross:.2f}</span></div>
        <div>💰 <b>Earned:</b> <span style='color:green'>${export_earnings:.2f}</span></div>
        </div>
        """, unsafe_allow_html=True)
    
    with c4:
        st.markdown("### 🏠 Load")
        st.metric("Consumption", f"{load_total:.1f} kWh")
        
    st.divider()
    
    # --- POWER FLOW CHART ---
    
    # Create single plot for Power Flow (Removed SOC)
    fig = go.Figure()
    
    # 1. Solar (Area, Orange)
    fig.add_trace(go.Scatter(
        x=df.index, y=df["pv_power_kw"], 
        name="Solar", 
        fill="tozeroy", 
        line=dict(color="#FFA500")
    ))
    
    # 2. Load (Line, Blue)
    fig.add_trace(go.Scatter(
        x=df.index, y=df["load_power_kw"], 
        name="Load", 
        line=dict(color="#4169E1", width=2)
    ))
    
    # 3. Battery (Line, Green)
    fig.add_trace(go.Scatter(
        x=df.index, y=df["battery_power_kw"], 
        name="Battery", 
        line=dict(color="#32CD32", width=1.5)
    ))
    
    # 4. Grid (Bar, Red/Transparent)
    fig.add_trace(go.Bar(
        x=df.index, y=df["meter_power_kw"], 
        name="Grid", 
        marker_color="rgba(220, 20, 60, 0.5)"
    ))
    
    # Layout Config
    fig.update_layout(
        height=350, 
        margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        yaxis_title="Power (kW)"
    )
    
    st.plotly_chart(fig, use_container_width=True)
    
    st.caption(f"Last updated: {datetime.now(ZoneInfo(TIMEZONE)).strftime('%H:%M:%S')}. Data refreshes automatically every minute.")

else:
    st.info("Waiting for data... (Only today's data is shown)")

# -----------------------------------------------------------------------------
# Auto Refresh Logic
# -----------------------------------------------------------------------------
time.sleep(60)
st.rerun()
