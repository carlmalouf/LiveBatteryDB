import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import asyncio
from datetime import datetime, date
import time
import os

# Import our existing client
from sems_client import SEMSClient

# Try to import config for local development, but don't fail if it's missing (e.g. on server)
try:
    import config
except ImportError:
    config = None

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

def get_setting(key, default=None):
    """
    Retrieve a configuration setting with the following priority:
    1. Streamlit Secrets (st.secrets) - for deployment
    2. Environment Variables - for Docker/System setup
    3. config.py - for local development
    4. Default value
    """
    # 1. Streamlit Secrets
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass # Ignore errors accessing secrets if not configured
        
    # 2. Environment Variables
    if key in os.environ:
        return os.environ[key]
        
    # 3. Local Config module
    if config and hasattr(config, key):
        return getattr(config, key)
        
    return default

# Page Config
st.set_page_config(
    page_title="Live Energy Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Hardcoded Tariffs (c/kWh)
USAGE_TARIFF = 26.18  # Cost to buy from grid
FEEDIN_TARIFF = 5.0   # Credit for selling to grid

# -----------------------------------------------------------------------------
# Data Fetching
# -----------------------------------------------------------------------------

@st.cache_resource(ttl=55)
def get_sems_data():
    """Fetch today's chart data and realtime status from SEMS."""
    async def fetch():
        # Retrieve credentials from secrets/env/config
        sems_account = get_setting("SEMS_ACCOUNT")
        sems_password = get_setting("SEMS_PASSWORD")
        station_id = get_setting("SEMS_STATION_ID")

        if not sems_account or not sems_password or not station_id:
            st.error("Missing Configuration: Please set SEMS_ACCOUNT, SEMS_PASSWORD, and SEMS_STATION_ID in .streamlit/secrets.toml or config.py")
            return None, None

        # Create client using config (now supports env vars)
        client = SEMSClient(sems_account, sems_password)
        today = date.today().strftime("%Y-%m-%d")
        
        # Fetch live instantaneous data and today's chart history
        realtime_data = await client.fetch_data(station_id)
        chart_data = await client.fetch_chart_data(station_id, today)
        return realtime_data, chart_data

    # Run async function
    return asyncio.run(fetch())

def process_chart_data(chart_json):
    """Process raw API chart data into a DataFrame with calculated metrics."""
    if not chart_json or chart_json.get("error"):
        return None
    
    # 1. Convert lists to DataFrame
    data_frames = []
    # API Return Keys: pv_power, battery_power, meter_power, load_power, soc (all lists of tuples: time, value)
    headers = ["pv_power", "battery_power", "meter_power", "load_power", "soc"]
    
    for key in headers:
        if key in chart_json and chart_json[key]:
            # Convert list of tuples to DataFrame
            df_temp = pd.DataFrame(chart_json[key], columns=["timestamp", key])
            df_temp["timestamp"] = pd.to_datetime(df_temp["timestamp"])
            df_temp = df_temp.set_index("timestamp")
            data_frames.append(df_temp)
            
    if not data_frames:
        return pd.DataFrame()
        
    # Merge all series on timestamp
    df = pd.concat(data_frames, axis=1).sort_index().fillna(0)
    
    # 2. Convert raw Watts to kW suitable for display
    for col in ["pv_power", "battery_power", "meter_power", "load_power"]:
        if col in df.columns:
            df[f"{col}_kw"] = df[col] / 1000.0
    
    # 3. Calculate Energies (kWh) per interval
    # Calculate time difference between points in hours (usually ~5 mins = 0.0833 hrs)
    df["dt_hours"] = df.index.to_series().diff().dt.total_seconds() / 3600.0
    # Fill first NaN with estimated 5 mins (standard SEMS interval)
    df["dt_hours"] = df["dt_hours"].fillna(5.0/60.0) 
    
    # Energy (kWh) = Power (W) / 1000 * time (h)
    df["pv_energy_kwh"] = df["pv_power"] / 1000.0 * df["dt_hours"]
    df["load_energy_kwh"] = df["load_power"] / 1000.0 * df["dt_hours"]
    
    # Battery Energy
    # Chart Data Convention: Positive = Discharge, Negative = Charge
    df["battery_discharge_kwh"] = df["battery_power"].clip(lower=0) / 1000.0 * df["dt_hours"]
    df["battery_charge_kwh"] = (-df["battery_power"]).clip(lower=0) / 1000.0 * df["dt_hours"]

    # Grid Energy
    # Chart Data Convention: Negative = Import, Positive = Export (Matches dashboard.py observations)
    df["grid_import_kwh"] = (-df["meter_power"]).clip(lower=0) / 1000.0 * df["dt_hours"]
    df["grid_export_kwh"] = df["meter_power"].clip(lower=0) / 1000.0 * df["dt_hours"]

    # 4. Calculate Financial Benefits
    
    # Solar Self-Consumption: Min of what we generated vs what we used
    df["solar_to_load_kwh"] = df[["pv_energy_kwh", "load_energy_kwh"]].min(axis=1)
    
    # Solar Benefit ($): Money saved by not buying grid power for self-consumed solar
    df["solar_benefit"] = df["solar_to_load_kwh"] * USAGE_TARIFF / 100.0
    
    # Battery Savings ($): Money saved by discharging battery instead of buying grid power
    df["battery_discharge_benefit"] = df["battery_discharge_kwh"] * USAGE_TARIFF / 100.0
    
    # Battery Cost ($): Money lost by charging (opportunity cost of not exporting)
    # Using Feed-in Tariff as the cost basis
    df["battery_charge_cost"] = df["battery_charge_kwh"] * FEEDIN_TARIFF / 100.0
    
    # Net Battery Benefit ($)
    df["battery_net_benefit"] = df["battery_discharge_benefit"] - df["battery_charge_cost"]

    # Export Income ($): Money earned from selling to grid
    df["export_income"] = df["grid_export_kwh"] * FEEDIN_TARIFF / 100.0
    
    # Grid Cost ($): Money spent on imports
    df["grid_cost"] = df["grid_import_kwh"] * USAGE_TARIFF / 100.0
    
    return df

# -----------------------------------------------------------------------------
# Main Application
# -----------------------------------------------------------------------------

# Title with Date
st.title(f"🌞 Live Energy Dashboard - {date.today().strftime('%A %d %B')}")

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
        stored_kwh = (current_soc / 100.0) * 44.8
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
    
    st.caption(f"Last updated: {datetime.now().strftime('%H:%M:%S')}. Data refreshes automatically every minute.")

else:
    st.info("Waiting for data... (Only today's data is shown)")

# -----------------------------------------------------------------------------
# Auto Refresh Logic
# -----------------------------------------------------------------------------
time.sleep(60)
st.rerun()
