"""
Shared configuration, data fetching, and chart processing for the energy dashboards.
Used by both simple_dashboard.py (Streamlit) and eink_dashboard.py (e-Paper).
"""

import logging
import os
from datetime import datetime

import pandas as pd

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from sems_client import SEMSClient

try:
    import config
except ImportError:
    config = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIMEZONE = "Australia/Brisbane"

# Tariffs (c/kWh)
USAGE_TARIFF = 26.18   # Cost to buy from grid
FEEDIN_TARIFF = 5.0    # Credit for selling to grid

# Battery
BATTERY_CAPACITY_KWH = 44.8

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    """Retrieve a configuration value from environment variables or config.py."""
    if key in os.environ:
        return os.environ[key]
    if config and hasattr(config, key):
        return getattr(config, key)
    return default

# ---------------------------------------------------------------------------
# Data Fetching
# ---------------------------------------------------------------------------

async def fetch_sems_data(get_setting_fn=None):
    """Fetch realtime and chart data from the SEMS portal.

    Args:
        get_setting_fn: Optional callable(key, default=None) for resolving
                        configuration values.  Falls back to the module-level
                        :func:`get_setting` when *None*.

    Returns:
        Tuple of (realtime_data, chart_json).  Either element may be *None*
        on failure.
    """
    _get = get_setting_fn or get_setting

    account    = _get("SEMS_ACCOUNT")
    password   = _get("SEMS_PASSWORD")
    station_id = _get("SEMS_STATION_ID")

    if not account or not password or not station_id:
        logger.error(
            "Missing SEMS credentials (SEMS_ACCOUNT, SEMS_PASSWORD, SEMS_STATION_ID)."
        )
        return None, None

    client = SEMSClient(account, password)
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")

    try:
        realtime = await client.fetch_data(station_id)
        chart    = await client.fetch_chart_data(station_id, today)
        return realtime, chart
    except Exception:
        logger.exception("SEMS data fetch failed")
        return None, None
    finally:
        await client.close()

# ---------------------------------------------------------------------------
# Chart Data Processing
# ---------------------------------------------------------------------------

def process_chart_data(chart_json):
    """Process raw SEMS chart JSON into a DataFrame with energy & financial metrics.

    Returns a :class:`~pandas.DataFrame` or *None* when the input data is
    missing or contains an error flag.
    """
    if not chart_json or chart_json.get("error"):
        return None

    data_frames = []
    for key in ["pv_power", "battery_power", "meter_power", "load_power", "soc"]:
        if key in chart_json and chart_json[key]:
            df_temp = pd.DataFrame(chart_json[key], columns=["timestamp", key])
            df_temp["timestamp"] = pd.to_datetime(df_temp["timestamp"])
            df_temp = df_temp.set_index("timestamp")
            data_frames.append(df_temp)

    if not data_frames:
        return None

    df = pd.concat(data_frames, axis=1).sort_index().fillna(0)

    # kW columns (used by Streamlit chart)
    for col in ["pv_power", "battery_power", "meter_power", "load_power"]:
        if col in df.columns:
            df[f"{col}_kw"] = df[col] / 1000.0

    # Time deltas between data points
    df["dt_hours"] = df.index.to_series().diff().dt.total_seconds() / 3600.0
    df["dt_hours"] = df["dt_hours"].fillna(5.0 / 60.0)  # assume 5-min SEMS interval

    # Energy (kWh) = Power (W) / 1000 * time (h)
    df["pv_energy_kwh"]   = df["pv_power"]   / 1000.0 * df["dt_hours"]
    df["load_energy_kwh"] = df["load_power"] / 1000.0 * df["dt_hours"]

    # Battery — Chart convention: Positive = Discharge, Negative = Charge
    df["battery_discharge_kwh"] = df["battery_power"].clip(lower=0) / 1000.0 * df["dt_hours"]
    df["battery_charge_kwh"]    = (-df["battery_power"]).clip(lower=0) / 1000.0 * df["dt_hours"]

    # Grid — Chart convention: Negative = Import, Positive = Export
    df["grid_import_kwh"] = (-df["meter_power"]).clip(lower=0) / 1000.0 * df["dt_hours"]
    df["grid_export_kwh"] = df["meter_power"].clip(lower=0) / 1000.0 * df["dt_hours"]

    # Financial metrics
    df["solar_to_load_kwh"]         = df[["pv_energy_kwh", "load_energy_kwh"]].min(axis=1)
    df["solar_benefit"]             = df["solar_to_load_kwh"]       * USAGE_TARIFF  / 100.0
    df["battery_discharge_benefit"] = df["battery_discharge_kwh"]   * USAGE_TARIFF  / 100.0
    df["battery_charge_cost"]       = df["battery_charge_kwh"]      * FEEDIN_TARIFF / 100.0
    df["battery_net_benefit"]       = df["battery_discharge_benefit"] - df["battery_charge_cost"]
    df["export_income"]             = df["grid_export_kwh"]         * FEEDIN_TARIFF / 100.0
    df["grid_cost"]                 = df["grid_import_kwh"]         * USAGE_TARIFF  / 100.0

    return df
