#
#Domoticz Plugin for Vevor YT60311 Weather Station
#This plugin connects to a Vevor YT60311 weather station via Tuya protocol
#and provides outdoor temperature, humidity, wind speed/direction, rain data,
#and other weather metrics.
#
"""
<plugin key="vevor-ws" name="vevor weather station" author="obedouet" version="0.1" wikilink="https://github.com/obedouet/vevor-plugin">
    <params>
        <param field="mAddress" label="Vevor Device IP address" width="200px" required="true" default="127.0.0.1"/>
        <param field="mDevice" label="Vevor Device ID" width="75px" required="true"/>
        <param field="mApiKey" label="Local API key" width="75px" required="true"/>
    </params>
</plugin>
"""

import Domoticz
import tinytuya
import base64
import struct
import threading
import time
from datetime import datetime

# ── Configuration ────────────────────────────────────────────────────────────
VERSION    = 3.5

# ── Wind direction lookup ─────────────────────────────────────────────────────
WIND_DIRS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
             "S","SSW","SW","WSW","W","WNW","NW","NNW"]

def deg_to_cardinal(deg):
    if deg is None:
        return "?"
    return WIND_DIRS[round(deg / 22.5) % 16]

# Humidity
def get_humidity_status(humidity_percentage: float) -> str:
    """
    Détermine le code de statut de l'humidité en fonction du pourcentage.
    
    Normal (40% - 44% ou 56% - 60%)
    Comfortable (45% - 55%)
    Dry (< 40%)
    Wet (> 60%)
    """
    if humidity_percentage < 40:
        return "Dry"
    elif 45 <= humidity_percentage <= 55:
        return "Comfortable"
    elif 40 <= humidity_percentage <= 60:
        return "Normal"
    else:
        return "Wet"

# ── DP113 binary payload decoder ──────────────────────────────────────────────
#
# The YT60311 encodes all outdoor sensor data in a single base64 binary blob
# sent as Tuya DP 113. The format was reverse engineered by comparing live
# readings from the Smart Life app against raw byte values across multiple
# captures.
#
# Offset map (all big-endian):
#   off  2-3  : outdoor temperature      s16 / 10     → °C
#   off  7    : outdoor humidity         u8            → %
#   off 10-11 : feels like temperature   s16 / 10     → °C
#   off 15-16 : heat index               s16 / 10     → °C
#   off 20-21 : dew point                s16 / 10     → °C
#   off 32    : wind speed               u8  / 10     → km/h
#   off 38    : wind gust                u8  / 10     → km/h
#   off 42-43 : wind direction           u16           → degrees (0=N, 90=E …)
#   off 47-48 : rain rate                u16 / 100    → mm/h
#   off 53-54 : rain hourly              u16 / 100    → mm
#   off 59-60 : rain daily               u16 / 100    → mm
#   off 65-66 : illuminance              u16 / 100    → lux
#   off 73-74 : indoor temperature       s16 / 10     → °C
#   off 78    : indoor humidity          u8            → %

def decode_dp113(b64_str):
    data = base64.b64decode(b64_str)

    def s16(off):
        return struct.unpack('>h', data[off:off+2])[0]

    def u16(off):
        return struct.unpack('>H', data[off:off+2])[0]

    return {
        "temp_ext_c":       s16(2)  / 10,
        "humidity_ext_pct": data[7],
        "feels_like_c":     s16(10) / 10,
        "heat_index_c":     s16(15) / 10,
        "dewpoint_c":       s16(20) / 10,
        "wind_speed_kmh":   data[32] * 36 / 100,
        "wind_speed_mps":   data[32] / 10,
        "wind_gust_kmh":    data[38] * 36 / 100,
        "wind_gust_mps":    data[38] / 10,
        "wind_dir_deg":     u16(42),
        "rain_rate_mmh":    u16(47) / 100,
        "rain_hourly_mm":   u16(53) / 100,
        "rain_daily_mm":    u16(59) / 100,
        "lux":              u16(65) / 100,
        "temp_int_c":       s16(73) / 10,
        "humidity_int_pct": data[78],
    }

# ── Static DPs state (pushed periodically by the station) ────────────────────
state = {
    "uv_index":      None,
    "pressure_hpa":  None,
    "rain_total_mm": None,
    "gust_max_kmh":  None,
    "battery":       None,
}

DP_HANDLERS = {
    "116": lambda v: state.update({"uv_index":      v}),
    "121": lambda v: state.update({"pressure_hpa":  round(v / 100, 1)}),
    "112": lambda v: state.update({"rain_total_mm": v}),
    "120": lambda v: state.update({"gust_max_kmh":  round(v / 10, 1)}),
    "123": lambda v: state.update({"battery":       v}),
}

def apply_dps(dps):
    for k, v in dps.items():
        h = DP_HANDLERS.get(str(k))
        if h:
            h(v)

# ── Plugin Class ──────────────────────────────────────────────────────────────
class BasePlugin:
    def __init__(self):
        self.device = None
        self.connected = False
        self.thread = None
        self.stop_event = threading.Event()
        self.data_lock = threading.Lock()
        return

    def onStart(self):
        Domoticz.Log("Starting Vevor YT60311 Weather Station plugin")
        
        # Create devices if they don't exist
        if 1 not in Devices:
            Domoticz.Device(Name="Outdoor Temperature", Unit=1, Type=80, Subtype=5).Create()
        if 2 not in Devices:
            Domoticz.Device(Name="Outdoor Humidity", Unit=2, Type=81, Subtype=1).Create()
        if 3 not in Devices:
            Domoticz.Device(Name="Wind", Unit=3, Type=86, Subtype=1).Create()
        #if 4 not in Devices:
        #    Domoticz.Device(Name="Wind Direction", Unit=4, Type=243, Subtype=31, Options={"Custom": "0;°"}).Create()
        #if 5 not in Devices:
        #    Domoticz.Device(Name="Rain Rate", Unit=5, Type=243, Subtype=31, Options={"Custom": "0;mm/h"}).Create()
        if 6 not in Devices:
            #BUG: raintotal error
            Domoticz.Device(Name="Rain", Unit=6, Type=85, Subtype=1).Create()
        if 7 not in Devices:
            Domoticz.Device(Name="UV Index", Unit=7, Type=87, Subtype=1).Create()
        if 8 not in Devices:
            Domoticz.Device(Name="Pressure", Unit=8, Type=243, Subtype=9).Create()
        if 9 not in Devices:
            Domoticz.Device(Name="Battery Level", Unit=9, Type=243, Subtype=22).Create()
        if 10 not in Devices:
            Domoticz.Device(Name="Indoor Temperature", Unit=10, Type=80, Subtype=5).Create()
        if 11 not in Devices:
            Domoticz.Device(Name="Indoor Humidity", Unit=11, Type=81, Subtype=1).Create()
        
        # Initialize device connection
        try:
            self.device = tinytuya.Device(
                dev_id=Parameters["mDevice"].strip(),
                address=Parameters["mAddress"].strip(),
                local_key=Parameters["mApiKey"].strip(),
                version=VERSION
            )
            self.device.set_socketPersistent(True)
            self.device.set_socketNODELAY(True)
            
            Domoticz.Log("Device initialized successfully")

            self.connected = True
            Domoticz.Log("Connected to Vevor weather station")
            
            # Start the data receiving thread
            if self.thread is None or not self.thread.is_alive():
                self.stop_event.clear()
                self.thread = threading.Thread(target=self.receive_data_loop)
                self.thread.daemon = True
                self.thread.start()
                Domoticz.Log("Data receiving thread started")

        except Exception as e:
            Domoticz.Error(f"Failed to initialize device: {str(e)}")

    def onConnect(self, Status, Description):
        Domoticz.Log(f"onConnect called with status: {Status}")
        
    def receive_data_loop(self):
        """Thread function to continuously receive data from the weather station"""
        Domoticz.Log("Starting data receiving loop")
        
        try:
            while not self.stop_event.is_set():
                # Get status updates
                if self.device is not None:
                    status = self.device.status()
                    
                    # Process the received data
                    if "dps" in status:
                        dps = status["dps"]
                        apply_dps(dps)
                        
                        dp113 = dps.get("113")
                        if dp113:
                            try:
                                wx = decode_dp113(dp113)
                                
                                # Update devices with new data (thread-safe)
                                with self.data_lock:
                                    if 1 in Devices:
                                        Devices[1].Update(nValue=0, sValue=str(wx['temp_ext_c']))
                                    if 2 in Devices:
                                        # Domoticz expect humidity in nValue and humidity status in sValue
                                        hum_status=get_humidity_status(wx['humidity_ext_pct'])
                                        Devices[2].Update(nValue=wx['humidity_ext_pct'], sValue=hum_status)
                                    if 3 in Devices:
                                        #BUG:  Warning: Expecting svalue with at least 5 elements separated by semicolon, 4 elements received ("100;E;0.0;0.0")
                                        # Expected values:
                                        # Wind bearing (0-359)
                                        # Wind direction (S, SW, NNW, etc.)
                                        # 10 * Wind speed [m/s]
                                        # 10 * Gust [m/s]
                                        # Temperature
                                        # Temperature Windchill

                                        wind_dir_card = deg_to_cardinal(wx['wind_dir_deg'])
                                        Devices[3].Update(nValue=0, sValue=str(wx['wind_dir_deg']) + ";" \
                                            + wind_dir_card + ";"\
                                            + str(wx['wind_speed_mps']) + ";"\
                                            + str(wx['wind_gust_mps']) + ";"\
                                            + str(wx['temp_ext_c']) + ";"\
                                            + str(wx['feels_like_c']))
                                    #if 4 in Devices:
                                    #    wind_dir_card = deg_to_cardinal(wx['wind_dir_deg'])
                                    #    Devices[4].Update(nValue=0, sValue=str(wx['wind_dir_deg']) + " (" + wind_dir_card + ")")
                                    #if 5 in Devices:
                                    #    Devices[5].Update(nValue=0, sValue=str(wx['rain_rate_mmh']))
                                    if 6 in Devices:
                                        Devices[6].Update(nValue=0, sValue=str(wx['rain_rate_mmh'])+";"+str(state['rain_total_mm']))
                                    if 7 in Devices:
                                        #BUG:  Warning: Expecting svalue with at least 2 elements separated by semicolon, 1 elements received ("0")
                                        # Domoticz expect 2 values:
                                        # UV = Float (example: 2.1) with current UV reading.
                                        # temperature (only for UV subtype TFA (3), else specify 0)
                                        Devices[7].Update(nValue=0, sValue=str(state['uv_index'])+";0")
                                    if 8 in Devices:
                                        Devices[8].Update(nValue=0, sValue=str(state['pressure_hpa']))
                                    if 9 in Devices:
                                        Devices[9].Update(nValue=0, sValue=str(state['battery']))
                                    if 10 in Devices:
                                        Devices[10].Update(nValue=0, sValue=str(wx['temp_int_c']))
                                    if 11 in Devices:
                                        # Domoticz expect humidity in nValue and humidity status in sValue
                                        hum_status=get_humidity_status(wx['humidity_int_pct'])
                                        Devices[11].Update(nValue=wx['humidity_int_pct'], sValue=hum_status)
                                        
                            except Exception as e:
                                Domoticz.Error(f"Error processing weather data: {str(e)}")
                
                # Wait before next update
                if self.stop_event.wait(timeout=30):
                    break  # Update every 30 seconds
                
        except Exception as e:
            Domoticz.Error(f"Error in data receiving loop: {str(e)}")
            
        Domoticz.Log("Data receiving loop stopped")

    def onStop(self):
        self.connected = False
        Domoticz.Log("Disconnected from Vevor weather station")
        
        # Stop the data receiving thread
        if self.thread is not None:
            self.stop_event.set()
            self.thread.join(timeout=2)
            Domoticz.Log("Data receiving thread stopped")

    def onHeartbeat(self):
        # This is called periodically, we can add periodic updates here if needed
        pass

# ── Plugin Instance ───────────────────────────────────────────────────────────
plugin = BasePlugin()

# ── Callbacks ────────────────────────────────────────────────────────────────
def onStart():
    plugin.onStart()

def onConnect(Status, Description):
    plugin.onConnect(Status, Description)

def onMessage(Data):
    # This method is not used in this implementation as we're using threading
    pass

def onDisconnect():
    plugin.onStop()

def onStop():
    plugin.onStop()

def onHeartbeat():
    plugin.onHeartbeat()