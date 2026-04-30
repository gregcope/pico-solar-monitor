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

appVersion = "4.0.0" # Modular Object-Oriented Refactor

# ==========================================
# 1. CONFIGURATION
# ==========================================
clientId = "solar_monitor"
sensorInterval = 5 
heartbeatInterval = 60 
tempChangeThreshold = 0.3 

systemStartTime = time.time()
watchdog = machine.WDT(timeout=8000)
onboardLed = machine.Pin("LED", machine.Pin.OUT, value=1)

print(f"--- SYSTEM START (v{appVersion}) ---")
for i in range(5, 0, -1):
    onboardLed.toggle()
    time.sleep(1)

# ==========================================
# 2. THE SENSOR LIBRARY (Isolated Logic)
# ==========================================
class SensorManager:
    def __init__(self, data_pin_num):
        self.dataPin = machine.Pin(data_pin_num)
        self.dsBus = onewire.OneWire(self.dataPin)
        self.dsSensor = ds18x20.DS18X20(self.dsBus)
        
        self.sensors = [
            {'sensorName': "PanA", 'sensorId': "PanA", 'lastTemp': 0.0},
            {'sensorName': "PanB", 'sensorId': "PanB", 'lastTemp': 0.0},
            {'sensorName': "PanC", 'sensorId': "PanC", 'lastTemp': 0.0},
            {'sensorName': "PanD", 'sensorId': "PanD", 'lastTemp': 0.0}
        ]

    def read_and_evaluate(self, force_publish=False):
        """Reads bus and returns a list of MQTT payloads ONLY if they changed."""
        updates = []
        try:
            roms = self.dsSensor.scan()
            self.dsSensor.convert_temp()
            time.sleep_ms(750)
            
            for idx, rom in enumerate(roms[:4]):
                s = self.sensors[idx]
                val = round(self.dsSensor.read_temp(rom), 1)
                
                if val != 85.0 and val != -127.0:
                    if abs(val - s['lastTemp']) >= tempChangeThreshold or force_publish:
                        updates.append({
                            'id': s['sensorId'],
                            'topic': f"homeassistant/sensor/{s['sensorId']}/state",
                            'payload': {"temperature": val}
                        })
                        s['lastTemp'] = val # Update state
        except Exception as e:
            print(f" ! Sensor read error: {e}")
            
        return updates

# ==========================================
# 3. THE NETWORK LIBRARY (Isolated Logic)
# ==========================================
class NetworkManager:
    def __init__(self, client_id, broker, user, password):
        self.clientId = client_id
        self.broker = broker
        self.user = user
        self.password = password
        self.client = None
        self.failedAttempts = 0
        self.reconnects = 0

    def maintain_connection(self):
        """Ensures WiFi and MQTT are active. Returns True if connected."""
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        
        if wlan.isconnected() and self.client is not None:
            return True

        onboardLed.on()
        print("WiFi: Connecting...")
        if not wlan.isconnected():
            wlan.connect(secrets.wifiSsid, secrets.wifiPassword)
            for _ in range(15):
                if wlan.isconnected(): break
                watchdog.feed(); time.sleep(1)

        if wlan.isconnected():
            try:
                uniqueId = self.clientId + "_" + hexlify(machine.unique_id()).decode()
                self.client = MQTTClient(uniqueId, self.broker, user=self.user, password=self.password, keepalive=60)
                self.client.connect()
                self.reconnects += 1
                self.failedAttempts = 0
                print("--- NETWORK READY ---")
                return True
            except:
                pass
                
        self.failedAttempts += 1
        if self.failedAttempts >= 5: machine.reset()
        return False

    def publish(self, topic, payload_dict, retain=True):
        """Safely encodes and publishes JSON data."""
        if self.client is None: return False
        try:
            t_bytes = topic.encode('utf-8')
            p_bytes = json.dumps(payload_dict).encode('utf-8')
            self.client.publish(t_bytes, p_bytes, retain=retain)
            print(f" > MQTT TX [{topic.split('/')[-2]}]: {p_bytes.decode('utf-8')}")
            return True
        except:
            print(" ! Publish failed. Dropping connection.")
            self.client = None
            return False

    def check_messages(self):
        if self.client:
            try: self.client.check_msg()
            except: self.client = None

    def send_discovery(self, sensors_list):
        """Fires the discovery burst. Needs formatting specific to the HA setup."""
        print("Status: Sending Discovery Burst...")
        try:
            for s in sensors_list:
                topic = f"homeassistant/sensor/{s['sensorId']}_T/config"
                payload = {
                    "name": f"{s['sensorName']} Temperature", 
                    "unique_id": f"{s['sensorId']}_T", 
                    "state_topic": f"homeassistant/sensor/{s['sensorId']}/state",
                    "unit_of_measurement": "°C", "device_class": "temperature",
                    "value_template": "{{ value_json.temperature }}",
                    "device": {"identifiers": [self.clientId], "name": "Wall Solar Monitor"}
                }
                self.publish(topic, payload)
                time.sleep(0.5)
            
            sys_sensors = [("rssi", "Signal Strength", "signal_strength", "dBm"), ("uptime", "Uptime", "duration", "s"),
                           ("version", "Firmware Version", None, None), ("reconnects", "Reconnect Count", None, None)]
            for key, name, dClass, unit in sys_sensors:
                topic = f"homeassistant/sensor/{self.clientId}_{key}/config"
                payload = {
                    "name": name, "unique_id": f"{self.clientId}_{key}",
                    "state_topic": f"homeassistant/sensor/{self.clientId}_sys/state",
                    "value_template": f"{{{{ value_json.{key} }}}}", "entity_category": "diagnostic",
                    "device": {"identifiers": [self.clientId], "name": "Wall Solar Monitor"}
                }
                if dClass: payload["device_class"] = dClass
                if unit: payload["unit_of_measurement"] = unit
                self.publish(topic, payload)
                time.sleep(0.5)
            return True
        except: return False


# ==========================================
# 4. THE MAIN LOOP (The Conductor)
# ==========================================
# Initialize our modular objects
sensors = SensorManager(data_pin_num=15)
networkController = NetworkManager(clientId, secrets.mqttBroker, secrets.mqttUser, secrets.mqttPassword)

lastReadTime = 0
lastHeartbeatTime = 0
lastHeartbeatFlash = 0

while True:
    watchdog.feed()
    now = time.time()

    # 1. Ensure Network is Alive
    if not networkController.maintain_connection():
        time.sleep(2)
        continue

    # 2. Initial Setup (If freshly connected)
    if networkController.failedAttempts == 0 and lastReadTime == 0:
        if networkController.send_discovery(sensors.sensors):
            lastReadTime = now - sensorInterval
            lastHeartbeatTime = now - heartbeatInterval
        else:
            networkController.client = None
            continue

    networkController.check_messages()

    # Heartbeat LED
    if time.ticks_diff(time.ticks_ms(), lastHeartbeatFlash) > 1000:
        onboardLed.toggle()
        lastHeartbeatFlash = time.ticks_ms()

    # 3. Read Cycle & Publish Logic
    if now - lastReadTime >= sensorInterval:
        cycleStart = time.ticks_ms()
        forceHeartbeat = (now - lastHeartbeatTime) >= heartbeatInterval
        
        # Ask the sensor library to do the heavy lifting
        ready_payloads = sensors.read_and_evaluate(force_publish=forceHeartbeat)
        
        # Ask the network library to send whatever the sensors found
        for item in ready_payloads:
            networkController.publish(item['topic'], item['payload'])
            
        # Send System Diagnostics if it's heartbeat time
        if forceHeartbeat and networkController.client is not None:
            sys_data = {
                "rssi": network.WLAN(network.STA_IF).status('rssi'),
                "uptime": time.time() - systemStartTime,
                "version": appVersion,
                "reconnects": networkController.reconnects
            }
            networkController.publish(f"homeassistant/sensor/{clientId}_sys/state", sys_data)
            lastHeartbeatTime = now
            
        print(f"Status: Cycle completed in {time.ticks_diff(time.ticks_ms(), cycleStart)}ms\n")
        lastReadTime = now
        gc.collect()

    time.sleep(0.1)
