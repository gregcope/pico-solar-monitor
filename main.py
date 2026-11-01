import asyncio
import json
import os
import time
import machine
import network
import secrets
import ugit
import onewire
import ds18x20
from ubinascii import hexlify

try:
    from simple import MQTTClient
except ImportError:
    from umqtt.simple import MQTTException, MQTTClient

app_version = "5.6.8"

# ==========================================
# 1. CONFIGURATION
# ==========================================
client_id = "solar_monitor"
sensor_names = ["Bottom", "Middle", "TopL", "TopR"]

# Time and Threshold Variables
sensor_interval_secs = 5 
heartbeat_interval_secs = 300 
sensor_change_threshold_c = 0.3 

# Hardware and Network Timing
watchdog_timeout_ms = 8000 
mqtt_keepalive_grace_secs = 20
initial_backoff_secs = 1.0
max_backoff_secs = 60.0
max_failed_attempts = 20

# --- System Internals ---
system_start_time_secs = time.time()
watchdog = None
onboard_led = machine.Pin("LED", machine.Pin.OUT, value=1)
internal_temp_sensor = machine.ADC(4)
device_serial = hexlify(machine.unique_id()).decode()

system_status = "Initializing"
last_published_status = ""
force_sensor_publish = False 


class SensorReadError(Exception):
    """Raised when an individual 1-Wire sensor read fails."""
    pass


def get_reset_cause() -> str:
    """Determine the reset cause, prioritizing OTA firmware updates."""
    try:
        with open("/ugit_log.txt", "r") as log_file:
            log_data = log_file.read()
        if "/main.py updated" in log_data:
            os.remove("/ugit_log.txt")
            return "Firmware Upgrade"
    except OSError:
        pass

    cause = machine.reset_cause()
    if cause == machine.PWRON_RESET: return "Power On"
    if cause == machine.WDT_RESET: return "Watchdog Timer"
    if cause == machine.SOFT_RESET: return "Software Reset"
    return "Unknown"


last_reset_reason = get_reset_cause()

for i in range(10, 0, -1):
    onboard_led.toggle()
    time.sleep(0.2)


# ==========================================
# 2. THE SENSOR LIBRARY (Median Filtering)
# ==========================================
class SensorManager:
    def __init__(self, data_pin_num: int, sensor_list: list) -> None:
        self.data_pin = machine.Pin(data_pin_num)
        self.ds_bus = onewire.OneWire(self.data_pin)
        self.ds_sensor = ds18x20.DS18X20(self.ds_bus)
        self.roms_cache = []
        self.sensors = []
        for name in sensor_list:
            self.sensors.append({
                'sensor_name': name, 'sensor_id': name, 
                'last_temp_c': -999.0, 'last_avail': 'unknown', 
                'history': [], 'fail_count': 0 
            })

    def start_conversion(self) -> bool:
        """Starts a temperature conversion for all devices on the bus."""
        global system_status
        try:
            self.roms_cache = self.ds_sensor.scan()
            if not self.roms_cache: return False
            self.ds_sensor.convert_temp()
            return True
        except (onewire.OneWireError, OSError):
            system_status = "1-Wire Bus Error"
            return False

    def read_and_evaluate(self, force_publish: bool = False) -> list:
        global system_status
        updates = []
        bus_fault_detected = False
        try:
            for idx, s in enumerate(self.sensors):
                temp_c = None
                if idx < len(self.roms_cache):
                    try:
                        t = self.ds_sensor.read_temp(self.roms_cache[idx])
                        if t != 85.0 and t != -127.0: temp_c = t
                    except (onewire.OneWireError, OSError):
                        bus_fault_detected = True
                
                if temp_c is not None:
                    s['fail_count'] = 0
                    s['history'].append(temp_c)
                    if len(s['history']) > 3: s['history'].pop(0)
                    sorted_history = sorted(s['history'])
                    filtered_temp_c = sorted_history[1] if len(sorted_history) == 3 else sorted_history[-1]
                    filtered_temp_c = round(filtered_temp_c, 1)
                    current_avail = "online"
                    final_temp_to_evaluate_c = filtered_temp_c
                else:
                    s['fail_count'] += 1
                    final_temp_to_evaluate_c = None
                    current_avail = "offline" if s['fail_count'] >= 3 else s['last_avail'] 
                
                if force_publish or current_avail != s['last_avail']:
                    updates.append({'topic': f"homeassistant/sensor/{s['sensor_id']}/availability", 'payload': current_avail})
                    s['last_avail'] = current_avail
                    
                if current_avail == "online" and final_temp_to_evaluate_c is not None:
                    if force_publish or abs(final_temp_to_evaluate_c - s['last_temp_c']) >= sensor_change_threshold_c:
                        updates.append({'topic': f"homeassistant/sensor/{s['sensor_id']}/state", 'payload': {"temperature": final_temp_to_evaluate_c}})
                        s['last_temp_c'] = final_temp_to_evaluate_c
                        
            if system_status in ("Sensor Bus Exception", "1-Wire Bus Error") and not bus_fault_detected:
                system_status = "Healthy"
                
        except (onewire.OneWireError, OSError): 
            system_status = "Sensor Bus Exception"
        except Exception: 
            system_status = "Sensor Evaluation Error"
        return updates


# ==========================================
# 3. THE NETWORK MANAGER
# ==========================================
class NetworkManager:
    """Manages WiFi connectivity, MQTT communication, exponential backoff, and discovery payloads."""

    def __init__(
        self, client_id: str, broker: str, user: str, password: str
    ) -> None:
        self.client_id = client_id
        self.broker = broker
        self.user = user
        self.password = password
        self.client = None
        self.failed_attempts = 0
        self.reconnects = 0
        self.current_backoff = initial_backoff_secs

        self.master_avail_topic = (
            f"homeassistant/sensor/{self.client_id}/availability"
        )
        self.ota_cmd_topic = (
            f"homeassistant/button/{self.client_id}_ota/set"
        )

    async def maintain_connection(self) -> bool:
        """Maintain robust network and MQTT connectivity using exponential backoff."""
        global system_status
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)

        if wlan.isconnected() and self.client is not None:
            return True

        system_status = "Connecting"
        if not wlan.isconnected():
            wlan.connect(secrets.wifiSsid, secrets.wifiPassword)
            for _ in range(10):
                if wlan.isconnected():
                    break
                if watchdog is not None:
                    watchdog.feed()
                await asyncio.sleep(1)

        if wlan.isconnected():
            try:
                unique_id = self.client_id + "_" + device_serial
                self.client = MQTTClient(
                    unique_id,
                    self.broker,
                    user=self.user,
                    password=self.password,
                    keepalive=heartbeat_interval_secs
                    + mqtt_keepalive_grace_secs,
                )

                lwt_topic_bytes = self.master_avail_topic.encode("utf-8")
                self.client.set_last_will(
                    lwt_topic_bytes, b"offline", retain=True
                )

                self.client.connect()
                self.publish(self.master_avail_topic, "online", retain=True)

                # --- OTA Validation Handshake ---
                try:
                    os.remove("ota_pending.flag")
                except OSError:
                    pass

                self.reconnects += 1
                self.failed_attempts = 0
                self.current_backoff = initial_backoff_secs
                system_status = "Healthy"
                return True
            except (OSError, MQTTException):
                self.client = None

        self.failed_attempts += 1
        system_status = (
            f"Network Error ({self.failed_attempts}/{max_failed_attempts})"
        )

        sleep_steps = int(self.current_backoff)
        for _ in range(max(1, sleep_steps)):
            if watchdog is not None:
                watchdog.feed()
            await asyncio.sleep(1)

        self.current_backoff = min(
            self.current_backoff * 2.0, max_backoff_secs
        )

        if self.failed_attempts >= max_failed_attempts:
            machine.reset()

        return False

    def publish(
        self, topic: str, payload_data, retain: bool = True
    ) -> bool:
        """Publish data payload to specified MQTT topic."""
        if self.client is None:
            return False
        try:
            t_bytes = topic.encode("utf-8")
            p_bytes = (
                json.dumps(payload_data).encode("utf-8")
                if isinstance(payload_data, dict)
                else str(payload_data).encode("utf-8")
            )
            self.client.publish(t_bytes, p_bytes, retain=retain)
            return True
        except (OSError, MQTTException):
            self.client = None
            return False

    def check_messages(self) -> None:
        """Check for incoming MQTT messages."""
        if self.client:
            try:
                self.client.check_msg()
            except (OSError, MQTTException):
                self.client = None

    def send_discovery(self, sensors_list: list) -> bool:
        """Send Home Assistant MQTT discovery configuration."""
        try:
            device_info = {
                "identifiers": [self.client_id, device_serial],
                "name": "Wall Solar Monitor",
                "model": "Raspberry Pi Pico 2 W",
                "sw_version": app_version,
                "serial_number": device_serial,
            }

            # Register OTA Update Button
            ota_config_topic = (
                f"homeassistant/button/{self.client_id}_ota/config"
            )
            ota_payload = {
                "name": "Update Firmware",
                "unique_id": f"{self.client_id}_ota",
                "command_topic": self.ota_cmd_topic,
                "device_class": "update",
                "availability_topic": self.master_avail_topic,
                "device": device_info,
            }
            self.publish(ota_config_topic, ota_payload)
            time.sleep(0.1)

            for s in sensors_list:
                topic = f"homeassistant/sensor/{s['sensor_id']}_T/config"
                payload = {
                    "name": f"{s['sensor_name']} Temp", 
                    "unique_id": f"{s['sensor_id']}_T", 
                    "state_topic": f"homeassistant/sensor/{s['sensor_id']}/state",
                    "availability": [
                        {"topic": self.master_avail_topic},
                        {"topic": f"homeassistant/sensor/{s['sensor_id']}/availability"}
                    ],
                    "availability_mode": "all",
                    "unit_of_measurement": "°C", "device_class": "temperature",
                    "state_class": "measurement", 
                    "value_template": "{{ value_json.temperature }}",
                    "device": device_info
                }
                self.publish(topic, payload)
                time.sleep(0.1)
            
            sys_sensors = [("pico_temp", "Internal CPU Temp", "temperature", "°C", "measurement"),
                           ("rssi", "Signal Strength", "signal_strength", "dBm", "measurement"), 
                           ("uptime", "Uptime", "duration", "s", None),
                           ("version", "Firmware Version", None, None, None), 
                           ("reconnects", "Reconnect Count", None, None, None),
                           ("last_reset", "Last Reset Reason", None, None, None), 
                           ("status", "System Status", None, None, None)]
                           
            for key, name, d_class, unit, s_class in sys_sensors:
                topic = f"homeassistant/sensor/{self.client_id}_{key}/config"
                payload = {
                    "name": name, 
                    "unique_id": f"{self.client_id}_{key}",
                    "state_topic": f"homeassistant/sensor/{self.client_id}_sys/state",
                    "availability_topic": self.master_avail_topic,
                    "value_template": f"{{{{ value_json.{key} }}}}", 
                    "entity_category": "diagnostic",
                    "device": device_info
                }
                if d_class: payload["device_class"] = d_class
                if unit: payload["unit_of_measurement"] = unit
                if s_class: payload["state_class"] = s_class 
                self.publish(topic, payload)
                time.sleep(0.1)
            return True
        except (OSError, ValueError):
            return False


# ==========================================
# 3. THE MAIN LOOP & ASYNC TASKS
# ==========================================
async def main() -> None:
    global system_status, last_published_status, force_sensor_publish, watchdog

    # Start the watchdog strictly when the application loop begins
    watchdog = machine.WDT(timeout=watchdog_timeout_ms)

    sensors_manager = SensorManager(data_pin_num=15, sensor_list=sensor_names)
    network_controller = NetworkManager(
        client_id, secrets.mqttBroker, secrets.mqttUser, secrets.mqttPassword
    )

    # MQTT message callback handler
    def sub_cb(topic: bytes, msg: bytes) -> None:
        topic_str = topic.decode("utf-8")
        msg_str = msg.decode("utf-8")

        if topic_str == network_controller.ota_cmd_topic:
            if msg_str.upper() == "PRESS":
                try:
                    # Base system telemetry payload
                    internal_volts = internal_temp_sensor.read_u16() * (3.3 / 65535)
                    pico_temp_c = round(
                        27 - (internal_volts - 0.706) / 0.001721, 1
                    )
                    sys_data = {
                        "pico_temp": pico_temp_c,
                        "rssi": network.WLAN(network.STA_IF).status("rssi"),
                        "uptime": time.time() - system_start_time_secs,
                        "version": app_version,
                        "reconnects": network_controller.reconnects,
                        "last_reset": last_reset_reason,
                    }

                    # Stage 1: Announce OTA initiation
                    sys_data["status"] = "OTA: Initiating Sequence"
                    network_controller.publish(
                        f"homeassistant/sensor/{client_id}_sys/state", sys_data
                    )
                    
                    # Suspend RP2040/RP2350 software execution timer constraints natively
                    # (Fallback safety mechanism is handled by boot.py missing main trigger)
                    with open("ota_pending.flag", "w") as f:
                        f.write("pending")

                    try:
                        os.remove("main_backup.py")
                    except OSError:
                        pass

                    os.rename("main.py", "main_backup.py")

                    # Stage 2: Announce download start
                    sys_data["status"] = "OTA