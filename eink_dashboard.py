"""
E-Ink Energy Dashboard for Waveshare 7.3" ACeP 7-Color Display (800x480)
=========================================================================
Renders the same live energy data from simple_dashboard.py as a static image
optimised for the Waveshare 7.3inch e-Paper HAT (F) — WS-23434.

Display specs:
  - 800 x 480 pixels, 7 colours (Black, White, Green, Blue, Red, Yellow, Orange)
  - SPI interface, ~35 s full-refresh, min 180 s between refreshes
  - Raspberry Pi 40-PIN GPIO HAT

Usage:
  Raspberry Pi (with display):  python3 eink_dashboard.py
  Development PC (simulation):  python3 eink_dashboard.py --simulate
  Single shot (no loop):        python3 eink_dashboard.py --once

The --simulate flag skips the Waveshare driver and saves a PNG preview instead.
"""

import asyncio
import argparse
import logging
import os
import time
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from energy_common import (
    TIMEZONE, USAGE_TARIFF, FEEDIN_TARIFF, BATTERY_CAPACITY_KWH,
    fetch_sems_data,
    process_chart_data,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Display Constants
# ---------------------------------------------------------------------------
EPD_WIDTH = 800
EPD_HEIGHT = 480

# 7-colour palette (RGB values matching the Waveshare epd7in3f driver)
BLACK   = (0, 0, 0)
WHITE   = (255, 255, 255)
GREEN   = (0, 255, 0)
BLUE    = (0, 0, 255)
RED     = (255, 0, 0)
YELLOW  = (255, 255, 0)
ORANGE  = (255, 128, 0)

# Refresh interval in seconds (5 minutes — well above the 180 s minimum)
REFRESH_INTERVAL = 300

# ---------------------------------------------------------------------------
# Font helpers — tries system fonts, falls back to PIL default
# ---------------------------------------------------------------------------

def _load_font(size: int, bold: bool = False):
    """Try to load a TTF font; fall back to PIL default bitmap font."""
    # Common paths for DejaVu Sans on Raspberry Pi OS
    candidates = []
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except (IOError, OSError):
                continue
    # Absolute fallback
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except (IOError, OSError):
        return ImageFont.load_default()


# Pre-load a set of sizes
FONT_XL_BOLD = _load_font(36, bold=True)
FONT_LG_BOLD = _load_font(24, bold=True)
FONT_LG      = _load_font(24)
FONT_MD_BOLD = _load_font(18, bold=True)
FONT_MD      = _load_font(18)
FONT_SM_BOLD = _load_font(14, bold=True)
FONT_SM      = _load_font(14)
FONT_XS      = _load_font(12)

# ---------------------------------------------------------------------------
# Image Rendering
# ---------------------------------------------------------------------------

def render_dashboard(realtime, df, now) -> Image.Image:
    """Build an 800x480 PIL Image for the e-ink display."""
    img = Image.new("RGB", (EPD_WIDTH, EPD_HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)

    # ----- Layout geometry (must total EPD_HEIGHT = 480) -----
    HEADER_H     = 44
    LIVE_TOP     = HEADER_H
    LIVE_H       = 120
    DIVIDER1_Y   = LIVE_TOP + LIVE_H       # 164
    SECTION_H    = 24
    TOTALS_TOP   = DIVIDER1_Y + SECTION_H  # 188
    TOTALS_H     = 210
    FOOTER_H     = 44
    FOOTER_TOP   = EPD_HEIGHT - FOOTER_H   # 436
    # 44 + 120 + 24 + 210 + 38 (power bar) + 44 = 480
    COL_W        = EPD_WIDTH // 4           # 200 px per column

    # ================================================================
    #  HEADER BAR
    # ================================================================
    draw.rectangle([0, 0, EPD_WIDTH, HEADER_H], fill=BLACK)
    draw.text((12, 5), "SOLAR DASHBOARD", font=FONT_LG_BOLD, fill=ORANGE)

    date_str = now.strftime("%A %d %B")
    time_str = f"Updated {now.strftime('%H:%M')}"
    # Right-align time
    time_bbox = draw.textbbox((0, 0), time_str, font=FONT_MD)
    draw.text((EPD_WIDTH - (time_bbox[2] - time_bbox[0]) - 12, 11), time_str, font=FONT_MD, fill=WHITE)
    # Centre date
    date_bbox = draw.textbbox((0, 0), date_str, font=FONT_MD)
    date_w = date_bbox[2] - date_bbox[0]
    draw.text(((EPD_WIDTH - date_w) // 2, 11), date_str, font=FONT_MD, fill=YELLOW)

    # ================================================================
    #  LIVE SNAPSHOT  (4 columns)
    # ================================================================
    def draw_live_card(col_idx, title, value_str, sub_str, accent_color):
        x0 = col_idx * COL_W
        x1 = x0 + COL_W
        y0 = LIVE_TOP
        y1 = DIVIDER1_Y

        # Card border
        draw.rectangle([x0, y0, x1, y1], outline=BLACK, width=1)
        # Accent bar at top of card
        draw.rectangle([x0 + 1, y0 + 1, x1 - 1, y0 + 5], fill=accent_color)

        # Title
        draw.text((x0 + 10, y0 + 12), title, font=FONT_SM_BOLD, fill=BLACK)

        # Large value
        draw.text((x0 + 10, y0 + 35), value_str, font=FONT_XL_BOLD, fill=accent_color)

        # Status / subtitle
        draw.text((x0 + 10, y0 + 80), sub_str, font=FONT_MD, fill=BLACK)

    if realtime and not realtime.error_message:
        # --- Solar ---
        pv_kw = realtime.pv_power / 1000.0
        draw_live_card(0, "SOLAR", f"{pv_kw:.2f} kW", "Producing" if pv_kw > 0.01 else "Idle", ORANGE)

        # --- Battery ---
        batt_w = realtime.battery_power
        batt_kw = abs(batt_w) / 1000.0
        soc = realtime.battery_soc
        if batt_w > 10:
            batt_status, batt_color = "Charging", GREEN
        elif batt_w < -10:
            batt_status, batt_color = "Discharging", RED
        else:
            batt_status, batt_color = "Idle", BLACK
        draw_live_card(1, "BATTERY", f"{batt_kw:.2f} kW", f"{batt_status}  SOC {soc}%", batt_color)

        # --- Grid ---
        grid_w = realtime.grid_power
        grid_kw = abs(grid_w) / 1000.0
        if grid_w < -50:
            grid_status, grid_color = "Exporting", GREEN
        elif grid_w > 50:
            grid_status, grid_color = "Importing", RED
        else:
            grid_status, grid_color = "Idle", BLACK
        draw_live_card(2, "GRID", f"{grid_kw:.2f} kW", grid_status, grid_color)

        # --- Load ---
        load_kw = realtime.load_power / 1000.0
        draw_live_card(3, "LOAD", f"{load_kw:.2f} kW", "Consuming", BLUE)
    else:
        # No live data — draw placeholder cards
        for i, label in enumerate(["SOLAR", "BATTERY", "GRID", "LOAD"]):
            draw_live_card(i, label, "-- kW", "No data", BLACK)

    # ================================================================
    #  SECTION DIVIDER — "TODAY'S TOTALS"
    # ================================================================
    draw.rectangle([0, DIVIDER1_Y, EPD_WIDTH, DIVIDER1_Y + SECTION_H], fill=BLACK)
    draw.text((12, DIVIDER1_Y + 3), "TODAY'S TOTALS", font=FONT_MD_BOLD, fill=YELLOW)

    # ================================================================
    #  DAILY TOTALS  (4 columns)
    # ================================================================
    if df is not None and not df.empty:
        pv_total       = df["pv_energy_kwh"].sum()
        solar_benefit  = df["solar_benefit"].sum()
        export_earnings = df["export_income"].sum()
        total_solar_val = solar_benefit + export_earnings
        solar_home_kwh = df["solar_to_load_kwh"].sum()
        solar_grid_kwh = df["grid_export_kwh"].sum()

        current_soc    = realtime.battery_soc if (realtime and not realtime.error_message) else df["soc"].iloc[-1]
        stored_kwh     = (current_soc / 100.0) * BATTERY_CAPACITY_KWH
        batt_discharge = df["battery_discharge_kwh"].sum()
        batt_charge    = df["battery_charge_kwh"].sum()
        batt_benefit   = df["battery_net_benefit"].sum()
        batt_discharge_ben = df["battery_discharge_benefit"].sum()
        batt_charge_cost   = df["battery_charge_cost"].sum()

        import_total   = df["grid_import_kwh"].sum()
        export_total   = df["grid_export_kwh"].sum()
        grid_cost_gross = df["grid_cost"].sum()
        net_grid_cost  = grid_cost_gross - export_earnings

        load_total     = df["load_energy_kwh"].sum()

        def draw_totals_col(col_idx, lines):
            """Draw a list of (text, font, color) tuples in a column."""
            x0 = col_idx * COL_W
            y = TOTALS_TOP + 8
            # Column border
            draw.rectangle([x0, TOTALS_TOP, x0 + COL_W, TOTALS_TOP + TOTALS_H], outline=BLACK, width=1)
            for text, fnt, color in lines:
                draw.text((x0 + 10, y), text, font=fnt, fill=color)
                bbox = draw.textbbox((0, 0), text, font=fnt)
                y += (bbox[3] - bbox[1]) + 6

        # Col 1 — Solar
        draw_totals_col(0, [
            ("SOLAR",                           FONT_SM_BOLD, ORANGE),
            (f"Generation: {pv_total:.1f} kWh", FONT_MD,      BLACK),
            (f"Benefit: ${total_solar_val:.2f}", FONT_MD_BOLD, GREEN),
            ("",                                 FONT_XS,      BLACK),
            (f"Home: {solar_home_kwh:.1f} kWh",  FONT_SM,      BLACK),
            (f"  (${solar_benefit:.2f} saved)",  FONT_SM,      GREEN),
            (f"Export: {solar_grid_kwh:.1f} kWh", FONT_SM,     BLACK),
            (f"  (${export_earnings:.2f} earned)", FONT_SM,    GREEN),
        ])

        # Col 2 — Battery
        draw_totals_col(1, [
            ("BATTERY",                                  FONT_SM_BOLD, GREEN if batt_benefit >= 0 else RED),
            (f"SOC: {current_soc}% ({stored_kwh:.1f} kWh)", FONT_MD, BLACK),
            (f"Net Benefit: ${batt_benefit:.2f}",        FONT_MD_BOLD, GREEN if batt_benefit >= 0 else RED),
            ("",                                          FONT_XS,     BLACK),
            (f"Discharge: {batt_discharge:.1f} kWh",     FONT_SM,     BLACK),
            (f"  (${batt_discharge_ben:.2f} saved)",     FONT_SM,     GREEN),
            (f"Charge: {batt_charge:.1f} kWh",           FONT_SM,     BLACK),
            (f"  (-${batt_charge_cost:.2f} cost)",       FONT_SM,     RED),
        ])

        # Col 3 — Grid
        net_color = GREEN if net_grid_cost <= 0 else RED
        draw_totals_col(2, [
            ("GRID",                                     FONT_SM_BOLD, net_color),
            (f"Net Cost: ${net_grid_cost:.2f}",          FONT_MD_BOLD, net_color),
            ("",                                          FONT_XS,     BLACK),
            (f"Import: {import_total:.1f} kWh",          FONT_MD,     RED),
            (f"  Cost: ${grid_cost_gross:.2f}",          FONT_SM,     RED),
            (f"Export: {export_total:.1f} kWh",          FONT_MD,     GREEN),
            (f"  Earned: ${export_earnings:.2f}",        FONT_SM,     GREEN),
        ])

        # Col 4 — Load / Consumption
        draw_totals_col(3, [
            ("CONSUMPTION",                        FONT_SM_BOLD, BLUE),
            (f"{load_total:.1f} kWh",              FONT_XL_BOLD, BLUE),
            ("",                                    FONT_XS,     BLACK),
            (f"Solar self-use: {solar_home_kwh:.1f} kWh", FONT_SM, GREEN),
            (f"Battery: {batt_discharge:.1f} kWh", FONT_SM,      GREEN),
            (f"Grid: {import_total:.1f} kWh",      FONT_SM,      RED),
        ])
    else:
        draw.text((12, TOTALS_TOP + 40), "No chart data available", font=FONT_LG, fill=RED)

    # ================================================================
    #  FOOTER
    # ================================================================
    draw.rectangle([0, FOOTER_TOP, EPD_WIDTH, EPD_HEIGHT], fill=BLACK)
    footer_text = (
        f"Last updated: {now.strftime('%H:%M:%S')}    |    "
        f"Refreshes every {REFRESH_INTERVAL // 60} mins    |    "
        f"Tariffs: {USAGE_TARIFF}c / {FEEDIN_TARIFF}c feed-in"
    )
    draw.text((12, FOOTER_TOP + 14), footer_text, font=FONT_SM, fill=WHITE)

    # ================================================================
    #  MINI POWER-FLOW BAR  (between totals and footer)
    # ================================================================
    if df is not None and not df.empty:
        _draw_power_flow_bar(draw, df, TOTALS_TOP + TOTALS_H, FOOTER_TOP)

    return img


def _draw_power_flow_bar(draw, df, y_top, y_bottom):
    """Draw a compact stacked bar showing where today's energy came from / went."""
    bar_h = y_bottom - y_top - 4
    if bar_h < 10:
        return

    y = y_top + 2

    pv_total   = df["pv_energy_kwh"].sum()
    load_total = df["load_energy_kwh"].sum()
    if load_total <= 0:
        return

    solar_home = df["solar_to_load_kwh"].sum()
    batt_dis   = df["battery_discharge_kwh"].sum()
    grid_imp   = df["grid_import_kwh"].sum()
    total_supply = solar_home + batt_dis + grid_imp
    if total_supply <= 0:
        return

    bar_start = 12
    bar_end   = EPD_WIDTH - 12
    bar_width = bar_end - bar_start

    # Label
    draw.text((bar_start, y), "Supply mix:", font=FONT_XS, fill=BLACK)

    bar_y = y + 2
    segments = [
        (solar_home / total_supply, ORANGE, f"Solar {solar_home:.1f}"),
        (batt_dis   / total_supply, GREEN,  f"Battery {batt_dis:.1f}"),
        (grid_imp   / total_supply, RED,    f"Grid {grid_imp:.1f}"),
    ]
    x = bar_start + 100
    seg_w = bar_width - 100
    for frac, color, label in segments:
        w = int(frac * seg_w)
        if w > 0:
            draw.rectangle([x, bar_y, x + w, bar_y + bar_h], fill=color)
            if w > 50:
                draw.text((x + 4, bar_y + 1), label, font=FONT_XS, fill=WHITE)
            x += w


# ---------------------------------------------------------------------------
# Display Driver Helpers
# ---------------------------------------------------------------------------

def display_on_epd(img: Image.Image):
    """Send an 800x480 PIL Image to the Waveshare 7.3" e-Paper (F)."""
    try:
        from waveshare_epd import epd7in3f  # type: ignore
    except ImportError:
        logger.error(
            "waveshare_epd library not found. "
            "Install via: git clone https://github.com/waveshare/e-Paper.git && "
            "cd e-Paper/RaspberryPi_JetsonNano/python && pip install ."
        )
        raise

    logger.info("Initialising e-Paper display…")
    epd = epd7in3f.EPD()
    epd.init()

    logger.info("Sending image to display (refresh takes ~35 s)…")
    buf = epd.getbuffer(img)
    epd.display(buf)

    logger.info("Putting display to sleep.")
    epd.sleep()


def save_simulation(img: Image.Image, path: str = "eink_preview.png"):
    """Save the rendered image as a PNG for development preview."""
    img.save(path)
    logger.info("Simulation preview saved to %s", path)

# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="E-Ink Energy Dashboard")
    parser.add_argument(
        "--simulate", action="store_true",
        help="Save PNG preview instead of driving the e-Paper display",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run once and exit (no refresh loop)",
    )
    parser.add_argument(
        "--output", type=str, default="eink_preview.png",
        help="Output filename for simulation mode (default: eink_preview.png)",
    )
    parser.add_argument(
        "--interval", type=int, default=REFRESH_INTERVAL,
        help=f"Refresh interval in seconds (default: {REFRESH_INTERVAL})",
    )
    args = parser.parse_args()

    while True:
        try:
            now = datetime.now(ZoneInfo(TIMEZONE))
            logger.info("Fetching SEMS data at %s …", now.strftime("%H:%M:%S"))

            realtime, chart_json = asyncio.run(fetch_sems_data())

            df = None
            if chart_json and not chart_json.get("error"):
                df = process_chart_data(chart_json)

            if realtime and realtime.error_message:
                logger.warning("Realtime error: %s", realtime.error_message)
            if chart_json and chart_json.get("error"):
                logger.warning("Chart error: %s", chart_json.get("error"))

            logger.info("Rendering dashboard image…")
            img = render_dashboard(realtime, df, now)

            if args.simulate:
                save_simulation(img, args.output)
            else:
                display_on_epd(img)

            logger.info("Refresh complete.")

        except KeyboardInterrupt:
            logger.info("Interrupted – exiting.")
            break
        except Exception:
            logger.exception("Error during refresh cycle")

        if args.once:
            break

        logger.info("Sleeping %d s until next refresh…", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
