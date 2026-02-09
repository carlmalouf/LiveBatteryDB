"""
GoodWe SEMS API Client - Fetches data from GoodWe cloud service
"""

import asyncio
import logging
import json
import aiohttp
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)

# SEMS API - Global endpoint (may redirect to regional)
SEMS_API_BASE = "https://www.semsportal.com/api/"
SEMS_LOGIN_URL = SEMS_API_BASE + "v2/Common/CrossLogin"


@dataclass
class InverterData:
    """Data class holding inverter readings"""
    timestamp: datetime
    # Solar
    pv_power: float = 0.0  # Current solar generation (W)
    pv1_power: float = 0.0
    pv2_power: float = 0.0
    pv1_voltage: float = 0.0
    pv2_voltage: float = 0.0
    
    # Battery
    battery_soc: int = 0  # State of charge (%)
    battery_power: float = 0.0  # Positive = charging, Negative = discharging (W)
    battery_voltage: float = 0.0
    battery_current: float = 0.0
    battery_temperature: float = 0.0
    
    # Grid
    grid_power: float = 0.0  # Positive = importing, Negative = exporting (W)
    grid_voltage: float = 0.0
    grid_frequency: float = 0.0
    
    # Load/House
    load_power: float = 0.0  # House consumption (W)
    
    # Totals
    total_pv_energy: float = 0.0  # kWh
    today_pv_energy: float = 0.0  # kWh
    today_export: float = 0.0  # kWh
    today_import: float = 0.0  # kWh
    
    # Status
    inverter_status: str = "Unknown"
    inverter_model: str = ""
    plant_name: str = ""
    error_message: Optional[str] = None


@dataclass 
class PlantInfo:
    """Information about a power station/plant"""
    id: str
    name: str
    status: str
    capacity: float  # kWp
    today_energy: float  # kWh
    total_energy: float  # kWh


class SEMSClient:
    """Client for communicating with GoodWe SEMS cloud API"""
    
    def __init__(self, account: str, password: str):
        self.account = account
        self.password = password
        self.token: Optional[str] = None
        self.uid: Optional[str] = None
        self._auth_token: Optional[str] = None  # Full token string for API calls
        self.api_url: str = SEMS_API_BASE  # May be updated after login
        self.plants: List[PlantInfo] = []
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_data: Optional[InverterData] = None
    
    def _get_headers(self) -> Dict[str, str]:
        """Get API request headers"""
        headers = {
            "Accept": "application/json",
            "User-Agent": "PVMaster/2.1.0 (iPhone; iOS 15.0; Scale/3.00)",
            "Token": self._token_string,
        }
        return headers
    
    @property
    def _token_string(self) -> str:
        """Get the token string for API requests"""
        if self._auth_token:
            logger.debug(f"Using auth token: {self._auth_token[:100]}...")
            return self._auth_token
        # Default token for initial login (global API accepts 'version')
        default_token = json.dumps({"version": "v2.1.0", "client": "ios", "language": "en"})
        logger.debug(f"Using default token: {default_token}")
        return default_token
    
    async def _ensure_session(self):
        """Ensure aiohttp session exists"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
    
    async def close(self):
        """Close the HTTP session"""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def login(self) -> tuple[bool, str]:
        """Login to SEMS portal and get authentication token
        
        Returns:
            Tuple of (success, message)
        """
        try:
            await self._ensure_session()
            
            # SEMS API payload
            payload = {
                "account": self.account,
                "pwd": self.password,
            }
            
            logger.info(f"Logging in to SEMS as {self.account}")
            
            async with self._session.post(
                SEMS_LOGIN_URL,
                json=payload,
                headers=self._get_headers()
            ) as response:
                response_text = await response.text()
                logger.debug(f"Login response: {response_text[:500]}")
                
                data = json.loads(response_text)
                
                # Check for errors
                code = data.get("code")
                has_error = data.get("hasError")
                msg = data.get("msg", "")
                
                if has_error is True or (code is not None and code != 0):
                    error_msg = msg or "Unknown error"
                    return False, f"Login failed: {error_msg} (code: {code})"
                
                result = data.get("data", {})
                
                if not isinstance(result, dict):
                    return False, f"Unexpected response format: {type(result)}"
                
                # Check if API is redirecting us to a regional server (api is at root level)
                api_url = data.get("api")
                if api_url:
                    self.api_url = api_url
                    logger.info(f"Using regional API: {self.api_url}")
                
                # Build auth token with the login response data (like pygoodwe)
                auth_data = {
                    "uid": result.get("uid", ""),
                    "timestamp": result.get("timestamp", ""),
                    "token": result.get("token", ""),
                    "client": "ios",
                    "version": "v2.0.4",
                    "language": "en",
                }
                self._auth_token = json.dumps(auth_data)
                
                # Extract auth credentials
                self.uid = result.get("uid", "")
                self.token = result.get("token", "")
                
                # Some regions return timestamp and other fields
                if not self.token and self.uid:
                    # Try using uid as token
                    self.token = self.uid
                
                if self.uid:
                    logger.info(f"SEMS login successful (uid: {self.uid[:8]}...)")
                    return True, "Login successful"
                elif "agreement" in str(result.keys()).lower():
                    return False, "Please log in to semsportal.com or the SEMS app first and accept any terms/agreements, then try again."
                else:
                    # Show what we got for debugging
                    return False, f"Login response missing uid. Keys: {list(result.keys())}"
                    
        except aiohttp.ClientError as e:
            logger.error(f"SEMS connection error: {e}")
            return False, f"Connection error: {str(e)}"
        except Exception as e:
            logger.error(f"SEMS login error: {e}")
            return False, f"Error: {str(e)}"
    
    async def get_plants(self) -> tuple[List[PlantInfo], str]:
        """Get list of power plants/stations
        
        Returns:
            Tuple of (plants list, message)
        """
        if not self.uid:
            success, msg = await self.login()
            if not success:
                return [], msg
        
        try:
            await self._ensure_session()
            
            headers = self._get_headers()
            
            # Use the PowerStationMonitor endpoint to get plant list
            plant_list_url = self.api_url + "v2/PowerStation/GetPowerStationByUser"
            
            payload = {}
            
            async with self._session.post(
                plant_list_url,
                data=payload,
                headers=headers
            ) as response:
                response_text = await response.text()
                logger.debug(f"Plants response: {response_text[:500]}")
                data = json.loads(response_text)
                
                if data.get("hasError") is True:
                    error_msg = data.get("msg", "Unknown error")
                    return [], f"API error: {error_msg}"
                
                # The response structure can vary
                result = data.get("data", {})
                
                # Handle different response formats
                if isinstance(result, list):
                    plants_data = result
                elif isinstance(result, dict):
                    plants_data = result.get("list", []) or result.get("plants", [])
                    # If result itself looks like a single plant
                    if not plants_data and "id" in result:
                        plants_data = [result]
                else:
                    plants_data = []
                
                self.plants = []
                for plant in plants_data:
                    if isinstance(plant, dict):
                        self.plants.append(PlantInfo(
                            id=str(plant.get("id", plant.get("powerstation_id", ""))),
                            name=plant.get("name", plant.get("stationname", "Unknown")),
                            status=str(plant.get("status", "unknown")),
                            capacity=float(plant.get("capacity", plant.get("nominal_power", 0)) or 0),
                            today_energy=float(plant.get("today_energy", plant.get("eday", 0)) or 0),
                            total_energy=float(plant.get("total_energy", plant.get("etotal", 0)) or 0)
                        ))
                
                if self.plants:
                    logger.info(f"Found {len(self.plants)} power plant(s)")
                    return self.plants, f"Found {len(self.plants)} plant(s)"
                else:
                    return [], f"No power plants found. Response keys: {list(data.keys())}, data type: {type(result)}"
                    
        except Exception as e:
            logger.error(f"Error getting plants: {e}")
            return [], f"Error: {str(e)}"
    
    async def fetch_data(self, station_id: str) -> InverterData:
        """Fetch current data from SEMS for a specific station.
        
        Args:
            station_id: The power station ID (required). Get this from the SEMS portal URL.
        """
        data = InverterData(timestamp=datetime.now())
        
        if not station_id:
            data.error_message = "Station ID is required. Please set SEMS_STATION_ID in config.py"
            return data
        
        if not self.uid:
            success, msg = await self.login()
            if not success:
                data.error_message = msg
                return data
        
        try:
            await self._ensure_session()
            
            headers = self._get_headers()
            
            # Use the regional API URL for plant details
            details_url = self.api_url + "v2/PowerStation/GetMonitorDetailByPowerstationId"
            
            payload = {
                "powerStationId": station_id
            }
            
            logger.info(f"Fetching data for station: {station_id}")
            logger.debug(f"Request URL: {details_url}")
            logger.debug(f"Headers: {headers}")
            
            async with self._session.post(
                details_url,
                data=payload,
                headers=headers
            ) as response:
                response_text = await response.text()
                logger.debug(f"Plant details response ({len(response_text) if response_text else 0} chars): {response_text[:500] if response_text else 'None'}")
                
                if not response_text or not response_text.strip():
                    data.error_message = f"Empty response from API (status: {response.status})"
                    return data
                
                result = json.loads(response_text)
                
                # Guard against None or non-dict responses
                if result is None or not isinstance(result, dict):
                    data.error_message = f"Invalid response format (got {type(result).__name__})"
                    return data
                
                if result.get("hasError") is True:
                    error_msg = result.get("msg", "Failed to fetch data")
                    data.error_message = error_msg
                    return data
                
                plant_data = result.get("data", {})
                if not isinstance(plant_data, dict):
                    plant_data = {}
                
                # Parse the data from SEMS response
                kpi = plant_data.get("kpi", {})
                inverter_info = plant_data.get("inverter", [{}])[0] if plant_data.get("inverter") else {}
                soc_data = plant_data.get("soc", {})
                
                # Power values (SEMS returns in kW, convert to W)
                data.pv_power = float(kpi.get("pac", 0)) * 1000  # Current power in W
                
                # If there's detailed inverter data
                if inverter_info:
                    data.inverter_model = inverter_info.get("model_type", "")
                    data.inverter_status = inverter_info.get("status", "Unknown")
                    
                    # Try to get more detailed power info
                    invert_full = inverter_info.get("invert_full", {})
                    if invert_full:
                        data.pv1_power = float(invert_full.get("pv1_power", 0))
                        data.pv2_power = float(invert_full.get("pv2_power", 0))
                        data.pv1_voltage = float(invert_full.get("pv1_voltage", 0))
                        data.pv2_voltage = float(invert_full.get("pv2_voltage", 0))
                        data.grid_voltage = float(invert_full.get("vac1", 0))
                        data.grid_frequency = float(invert_full.get("fac1", 0))
                
                # Battery data
                if soc_data:
                    data.battery_soc = int(soc_data.get("power", 0))
                    
                # Get battery power from homeKit data if available
                home_kit = plant_data.get("homeKit", {})
                logger.debug(f"homeKit raw: {home_kit}")
                if home_kit:
                    # Battery power (positive = charging, negative = discharging)
                    data.battery_power = float(home_kit.get("pCharge", 0)) * 1000
                    if home_kit.get("pDisCharge"):
                        data.battery_power = -float(home_kit.get("pDisCharge", 0)) * 1000
                    
                    # Grid power (positive = import, negative = export)
                    data.grid_power = float(home_kit.get("pGrid", 0)) * 1000
                    if home_kit.get("pGridExport"):
                        data.grid_power = -float(home_kit.get("pGridExport", 0)) * 1000
                    
                    # Load power
                    data.load_power = float(home_kit.get("pLoad", 0)) * 1000

                # Fallback: parse powerflow section (string format like "3266(W)")
                powerflow = plant_data.get("powerflow", {})
                if powerflow and not home_kit:
                    logger.debug(f"Using powerflow data: {powerflow}")

                    def _parse_power(val_str):
                        """Extract numeric watts from strings like '3266(W)' or '0(W)'."""
                        if not val_str or not isinstance(val_str, str):
                            return 0.0
                        import re
                        m = re.match(r"([\d.]+)", val_str)
                        return float(m.group(1)) if m else 0.0

                    # PV power
                    pv_val = _parse_power(powerflow.get("pv", "0"))
                    if pv_val > 0:
                        data.pv_power = pv_val

                    # Battery: betteryStatus -1=discharging, 1=charging
                    batt_val = _parse_power(powerflow.get("bettery", "0"))
                    batt_status = powerflow.get("betteryStatus", 0)
                    if batt_status == -1:
                        data.battery_power = -batt_val  # discharging (negative)
                    elif batt_status == 1:
                        data.battery_power = batt_val    # charging (positive)

                    # Grid: gridStatus -1=exporting, 1=importing
                    grid_val = _parse_power(powerflow.get("grid", "0"))
                    grid_status = powerflow.get("gridStatus", 0)
                    if grid_status == -1:
                        data.grid_power = -grid_val  # exporting
                    elif grid_status == 1:
                        data.grid_power = grid_val   # importing

                    # Load
                    load_val = _parse_power(powerflow.get("load", "0"))
                    if load_val > 0:
                        data.load_power = load_val

                    # SOC from powerflow (may be more current than soc section)
                    pf_soc = powerflow.get("soc")
                    if pf_soc is not None:
                        data.battery_soc = int(pf_soc)
                
                # Energy totals
                data.today_pv_energy = float(kpi.get("power", 0))  # Today's energy in kWh
                data.total_pv_energy = float(kpi.get("total_power", 0))  # Total energy in kWh
                
                # Month/year stats if available
                if "month_generation" in kpi:
                    pass  # Could add monthly stats
                
                self._last_data = data
                logger.debug(f"Fetched SEMS data: PV={data.pv_power}W, Battery={data.battery_soc}%, Grid={data.grid_power}W")
                
        except Exception as e:
            logger.error(f"Error fetching SEMS data: {e}")
            data.error_message = str(e)
        
        return data
    
    async def fetch_chart_data(self, station_id: str, date: str) -> Dict[str, Any]:
        """Fetch 5-minute interval chart data for a specific date.
        
        Args:
            station_id: The power station ID
            date: Date string in YYYY-MM-DD format
            
        Returns:
            Dictionary with chart data including:
            - pv_power: List of (timestamp, watts) tuples
            - battery_power: List of (timestamp, watts) tuples
            - meter_power: List of (timestamp, watts) tuples - grid import/export
            - load_power: List of (timestamp, watts) tuples
            - soc: List of (timestamp, percent) tuples
            - today_energy: Total kWh for the day
        """
        result = {
            "date": date,
            "pv_power": [],
            "battery_power": [],
            "meter_power": [],
            "load_power": [],
            "soc": [],
            "today_energy": 0.0,
            "error": None
        }
        
        if not station_id:
            result["error"] = "Station ID is required"
            return result
        
        if not self.uid:
            success, msg = await self.login()
            if not success:
                result["error"] = msg
                return result
        
        try:
            await self._ensure_session()
            headers = self._get_headers()
            
            # Fetch chart data from GetPlantPowerChart endpoint
            url = self.api_url + "v2/Charts/GetPlantPowerChart"
            payload = {"id": station_id, "date": date}
            
            logger.info(f"Fetching chart data for {date}")
            
            async with self._session.post(url, data=payload, headers=headers) as response:
                response_text = await response.text()
                
                if not response_text or not response_text.strip():
                    result["error"] = "Empty response from API"
                    return result
                
                data = json.loads(response_text)
                
                # Guard against None or non-dict responses
                if data is None or not isinstance(data, dict):
                    result["error"] = f"Invalid response format (got {type(data).__name__})"
                    return result
                
                if data.get("hasError"):
                    result["error"] = data.get("msg", "Unknown error")
                    return result
                
                chart_data = data.get("data", {})
                if not isinstance(chart_data, dict):
                    chart_data = {}
                
                # Extract generation summary
                generate_data = chart_data.get("generateData", [])
                for item in generate_data:
                    if item.get("key") == "Generation":
                        result["today_energy"] = float(item.get("value", 0))
                
                # Extract time series data
                lines = chart_data.get("lines", [])
                
                # Map API keys to our result keys
                key_mapping = {
                    "PCurve_Power_PV": "pv_power",
                    "PCurve_Power_Battery": "battery_power",
                    "PCurve_Power_Meter": "meter_power",
                    "PCurve_Power_Load": "load_power",
                    "PCurve_Power_SOC": "soc",
                }
                
                for line in lines:
                    api_key = line.get("key", "")
                    result_key = key_mapping.get(api_key)
                    
                    if result_key:
                        xy_data = line.get("xy", [])
                        for point in xy_data:
                            time_str = point.get("x", "")  # Format: "HH:MM"
                            value = point.get("y", 0.0) or 0.0
                            # Combine date and time
                            timestamp = f"{date} {time_str}:00"
                            result[result_key].append((timestamp, value))
                
                logger.info(f"Got {len(result['pv_power'])} data points for {date}")
                return result
                
        except Exception as e:
            logger.error(f"Error fetching chart data: {e}")
            result["error"] = str(e)
            return result
    
    @property
    def last_data(self) -> Optional[InverterData]:
        """Get the last fetched data"""
        return self._last_data


# Global client instance for reuse
_client: Optional[SEMSClient] = None


def get_sems_client(account: str, password: str) -> SEMSClient:
    """Get or create SEMS client"""
    global _client
    if _client is None or _client.account != account:
        _client = SEMSClient(account, password)
    return _client


def get_inverter_data(account: str, password: str, station_id: str) -> InverterData:
    """Synchronous wrapper to fetch inverter data from SEMS.
    
    Args:
        account: SEMS account email
        password: SEMS password
        station_id: Power station ID (required)
    """
    if not station_id:
        data = InverterData(timestamp=datetime.now())
        data.error_message = "Station ID is required. Please set SEMS_STATION_ID in config.py"
        return data
    
    async def _fetch():
        client = get_sems_client(account, password)
        try:
            return await client.fetch_data(station_id)
        finally:
            await client.close()
    
    return asyncio.run(_fetch())


if __name__ == "__main__":
    # Test the client
    import config
    
    logging.basicConfig(level=logging.DEBUG)
    
    if not config.SEMS_ACCOUNT or not config.SEMS_PASSWORD:
        print("Please set SEMS_ACCOUNT and SEMS_PASSWORD in config.py")
        exit(1)
    
    station_id = getattr(config, 'SEMS_STATION_ID', None)
    if not station_id:
        print("""
Station ID is required. Please set SEMS_STATION_ID in config.py

To find your Station ID:
1. Log in to https://www.semsportal.com
2. Click on your power station
3. The URL will show: https://www.semsportal.com/powerstation/powerstatussnmin/YOUR-STATION-ID
4. Copy the ID (format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)
""")
        exit(1)

    print("\n=== Fetching Inverter Data ===")
    data = get_inverter_data(config.SEMS_ACCOUNT, config.SEMS_PASSWORD, station_id)
    
    print(f"\nPlant: {data.plant_name}")
    print(f"Time: {data.timestamp}")
    print(f"Solar Power: {data.pv_power}W")
    print(f"Battery: {data.battery_soc}% ({data.battery_power}W)")
    print(f"Grid: {data.grid_power}W")
    print(f"Load: {data.load_power}W")
    print(f"Today's Generation: {data.today_pv_energy} kWh")
    if data.error_message:
        print(f"Error: {data.error_message}")
