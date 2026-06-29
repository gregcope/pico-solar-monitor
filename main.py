import machine
import time
import network
import json
import gc
import onewire
import ds18x20
from ubinascii import hexlify
import secrets

try:
    from simple import MQTTClient
except ImportError:
    from umqtt.simple import MQTTClient

app_version = "5.5.0" # Added internal CPU temperature monitoring

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
sensor_conversion_delay_ms = 750
mqtt_keepalive_grace_secs = 20

# --- System Internals ---
system_start_time_secs = time.time()
watchdog = machine.WDT(timeout=watchdog_timeout_ms) 
onboard_led = machine.Pin("LED", machine.Pin.OUT, value=1)

# Connect to the Pico's internal temperature sensor (ADC channel 4)
internal_temp_sensor = machine.ADC(4)

system_status = "Initializing"
last_published_status = ""
force_sensor_publish = False 

def get_reset_cause() -> str:
    cause = machine.reset_cause()
    if cause == machine.PWRON_RESET: return "Power On"
    if cause == machine.WDT_RESET: return "Watchdog Timer"
    if cause == machine.SOFT_RESET: return "Software Reset"
    return "Unknown"

last_reset_reason = get_reset_cause()
print(f"--- SYSTEM START (v{app_version}) ---")
print(f"--- Last Reset Cause: {last_reset_reason} ---")

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
        self.is_converting = False
        self.conversion_start_time_ms = 0
        self.roms_cache = []
        self.sensors = []
        for name in sensor_list:
            self.sensors.append({
                'sensor_name': name, 'sensor_id': name, 
                'last_temp_c': -999.0, 'last_avail': 'unknown', 
                'history': [], 'fail_count': 0 
            })

    def start_conversion(self) -> bool:
        try:
            self.roms_cache = self.ds_sensor.scan()
            if not self.roms_cache: return False
            self.ds_sensor.convert_temp()
            self.is_converting = True
            self.conversion_start_time_ms = time.ticks_ms()
            return True
        except onewire.OneWireError:
            self.is_converting = False
            return False

    def read_and_evaluate(self, force_publish: bool = False) -> list:
        updates = []
        self.is_converting = False 
        try:
            for idx, s in enumerate(self.sensors):
                temp_c = None
                if idx < len(self.roms_cache):
                    t = self.ds_sensor.read_temp(self.roms_cache[idx])
                    if t != 85.0 and t != -127.0: temp_c = t
                
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
        except Exception: pass
        return updates

# ==========================================
# 3. THE NETWORK LIBRARY
# ==========================================
class NetworkManager:
    def __init__(self, client_id: str, broker: str, user: str, password: str) -> None:
        self.client_id = client_id
        self.broker = broker
        self.user = user
        self.password = password
        self.client = None
        self.failed_attempts = 0
        self.reconnects = 0
        self.master_avail_topic = f"homeassistant/sensor/{self.client_id}/availability"

    def maintain_connection(self) -> bool:
        global system_status
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        
        if wlan.isconnected() and self.client is not None:
            return True

        system_status = "Connecting"
        if not wlan.isconnected():
            print("WiFi: Connecting...")
            wlan.connect(secrets.wifiSsid, secrets.wifiPassword)
            for _ in range(10):
                if wlan.isconnected(): break
                watchdog.feed()
                time.sleep(1)

        if wlan.isconnected():
            try:
                unique_id = self.client_id + "_" + hexlify(machine.unique_id()).decode()
                self.client = MQTTClient(
                    unique_id, 
                    self.broker, 
                    user=self.user, 
                    password=self.password, 
                    keepalive=heartbeat_interval_secs + mqtt_keepalive_grace_secs
                )
                
                # Register the Last Will and Testament BEFORE connecting
                lwt_topic_bytes = self.master_avail_topic.encode('utf-8')
                self.client.set_last_will(lwt_topic_bytes, b"offline", retain=True)
                
                self.client.connect()
                
                # Immediately announce the device is online
                self.publish(self.master_avail_topic, "online", retain=True)
                
                self.reconnects += 1
                self.failed_attempts = 0 
                system_status = "Healthy"
                print("--- NETWORK CONNECTED ---")
                return True
            except OSError:
                self.client = None
                
        self.failed_attempts += 1
        system_status = f"Network Error ({self.failed_attempts}/200)"
        print(f" ! Connection failure {self.failed_attempts}/200")
        
        if self.failed_attempts >= 200:
            print(" ! Resetting due to persistent network failure.")
            machine.reset() 
            
        return False

    def publish(self, topic: str, payload_data, retain: bool = True) -> bool:
        if self.client is None: return False
        try:
            t_bytes = topic.encode('utf-8')
            p_bytes = json.dumps(payload_data).encode('utf-8') if isinstance(payload_data, dict) else str(payload_data).encode('utf-8')
            self.client.publish(t_bytes, p_bytes, retain=retain)
            return True
        except OSError:
            self.client = None
            return False

    def check_messages(self) -> None:
        if self.client:
            try: self.client.check_msg()
            except OSError: self.client = None

    def send_discovery(self, sensors_list: list) -> bool:
        try:
            for s in sensors_list:
                topic = f"homeassistant/sensor/{s['sensor_id']}_T/config"
                payload = {
                    "name": f"{s['sensor_name']} Temp", "unique_id": f"{s['sensor_id']}_T", 
                    "state_topic": f"homeassistant/sensor/{s['sensor_id']}/state",
                    "availability": [
                        {"topic": self.master_avail_topic},
                        {"topic": f"homeassistant/sensor/{s['sensor_id']}/availability"}
                    ],
                    "availability_mode": "all",
                    "unit_of_measurement": "°C", "device_class": "temperature",
                    "state_class": "measurement", 
                    "value_template": "{{ value_json.temperature }}",
                    "device": {"identifiers": [self.client_id], "name": "Wall Solar Monitor"}
                }
                self.publish(topic, payload)
                time.sleep(0.1)
            
            # Included pico_temp in the diagnostics list
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
                    "name": name, "unique_id": f"{self.client_id}_{key}",
                    "state_topic": f"homeassistant/sensor/{self.client_id}_sys/state",
                    "availability_topic": self.master_avail_topic,
                    "value_template": f"{{{{ value_json.{key} }}}}", "entity_category": "diagnostic",
                    "device": {"identifiers": [self.client_id], "name": "Wall Solar Monitor"}
                }
                if d_class: payload["device_class"] = d_class
                if unit: payload["unit_of_measurement"] = unit
                if s_class: payload["state_class"] = s_class 
                self.publish(topic, payload)
            return True
        except Exception: return False

# ==========================================
# 4. THE MAIN LOOP
# ==========================================
sensors_manager = SensorManager(data_pin_num=15, sensor_list=sensor_names)
network_controller = NetworkManager(client_id, secrets.mqttBroker, secrets.mqttUser, secrets.mqttPassword)

last_read_time_secs = 0
last_heartbeat_time_secs = 0

while True:
    watchdog.feed()
    now_secs = time.time()

    # 1. Network Housekeeping
    if not network_controller.maintain_connection():
        time.sleep(2) 
        continue

    # 2. Config Handshake
    if network_controller.failed_attempts == 0 and last_read_time_secs == 0:
        if network_controller.send_discovery(sensors_manager.sensors):
            last_read_time_secs = now_secs - sensor_interval_secs
            last_heartbeat_time_secs = now_secs - heartbeat_interval_secs
        else:
            network_controller.client = None
            continue

    network_controller.check_messages()

    # 3. Dynamic Telemetry Broadcast
    force_heartbeat = (now_secs - last_heartbeat_time_secs >= heartbeat_interval_secs)
    status_changed = (system_status != last_published_status)

    if (force_heartbeat or status_changed) and network_controller.client is not None:
        # Convert internal 16-bit ADC value to voltage, then to Celsius
        internal_volts = internal_temp_sensor.read_u16() * (3.3 / 65535)
        pico_temp_c = round(27 - (internal_volts - 0.706) / 0.001721, 1)

        sys_data = {
            "pico_temp": pico_temp_c,
            "rssi": network.WLAN(network.STA_IF).status('rssi'),
            "uptime": time.time() - system_start_time_secs,
            "version": app_version,
            "reconnects": network_controller.reconnects,
            "last_reset": last_reset_reason,
            "status": system_status
        }
        network_controller.publish(f"homeassistant/sensor/{client_id}_sys/state", sys_data)
        last_published_status = system_status
        if force_heartbeat:
            print(f"--> System Heartbeat Sent. Status: {system_status}")
            force_sensor_publish = True 
            last_heartbeat_time_secs = now_secs

    # 4. Sensor Initialization Check
    if not sensors_manager.is_converting and (now_secs - last_read_time_secs >= sensor_interval_secs):
        bus_active = sensors_manager.start_conversion()
        if not bus_active:
            system_status = "1-Wire Bus Error"
            print(" ! Warning: 1-Wire bus communication failure. Check hardware connections.")
            for s in sensors_manager.sensors:
                if s['last_avail'] != 'offline':
                    network_controller.publish(f"homeassistant/sensor/{s['sensor_id']}/availability", "offline")
                    s['last_avail'] = 'offline'
            last_read_time_secs = now_secs 
        else:
            if system_status == "1-Wire Bus Error":
                system_status = "Healthy"

    # 5. Data Evaluation Block
    if sensors_manager.is_converting and time.ticks_diff(time.ticks_ms(), sensors_manager.conversion_start_time_ms) >= sensor_conversion_delay_ms:
        ready_payloads = sensors_manager.read_and_evaluate(force_publish=force_sensor_publish)
        force_sensor_publish = False 
        
        for item in ready_payloads:
            network_controller.publish(item['topic'], item['payload'])
            
        last_read_time_secs = now_secs
        gc.collect()

    time.sleep(0.1)
