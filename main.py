import machine
import time
import network
import json
import gc
import onewire
import ds18x20
from ubinascii import hexlify

# Robust import for MQTT
try:
    from simple import MQTTClient
except ImportError:
    from umqtt.simple import MQTTClient

# Start global timer using absolute seconds for infinite uptime tracking
systemStartTime = time.time()
reconnectCount = 0
failedConnAttempts = 0 
appVersion = "3.1.7" # Renamed sensor array variable to tempSensors

# ==========================================
# 1. CONFIGURATION (camelCase)
# ==========================================
import secrets

wifiSsid = secrets.wifiSsid
wifiPassword = secrets.wifiPassword
mqttBroker = secrets.mqttBroker
mqttUser = secrets.mqttUser
mqttPassword = secrets.mqttPassword

# Cleaned up client ID to prevent "solar_array" appearing in HA entity IDs
clientId = "solar_monitor"

sensorInterval = 5 # Polling 1-Wire bus every 5 seconds
heartbeatInterval = 60 # Force publish interval
tempChangeThreshold = 0.3 # Minimum temp change required to trigger a publish
maxFailedAttempts = 5 
keepAliveInterval = 60 # Broker timeout threshold

# ==========================================
# 2. HARDWARE SETUP
# ==========================================
onboardLed = machine.Pin("LED", machine.Pin.OUT, value=1)
dataPin = machine.Pin(15)
dsBus = onewire.OneWire(dataPin)
dsSensor = ds18x20.DS18X20(dsBus)
watchdog = machine.WDT(timeout=8000)

print(f"--- SYSTEM START (v{appVersion}) ---")

# ==========================================
# 3. SAFETY STARTUP DELAY
# ==========================================
# Delay to allow hardware to settle before connecting
for i in range(5, 0, -1):
    print(f"Status: Safety delay... {i}s")
    onboardLed.on()
    time.sleep(0.1)
    onboardLed.off()
    time.sleep(0.9)

# ==========================================
# 4. SENSOR SETUP
# ==========================================
tempSensors = []
sensorConfig = [("PanA", 1), ("PanB", 2), ("PanC", 3), ("PanD", 4)]

# Build sensor dictionaries to retain state
for name, num in sensorConfig:
    tempSensors.append({
        'sensorName': name,
        'sensorId': name, # Removed "solar" prefix for cleaner entity IDs
        'lastTemp': 0.0 
    })

# Define system diagnostic sensors to retain state for HA
systemDiagnostics = [
    ("rssi", "Signal Strength", "signal_strength", "dBm"),
    ("uptime", "Uptime", "duration", "s"),
    ("version", "Firmware Version", None, None),
    ("reconnects", "Reconnect Count", None, None)
]

# ==========================================
# 5. NETWORK & MQTT
# ==========================================
def connectToNetwork():
    global reconnectCount, failedConnAttempts
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    if not wlan.isconnected():
        print(f"WiFi: Connecting to {wifiSsid}...")
        wlan.connect(wifiSsid, wifiPassword)
        for _ in range(15):
            if wlan.isconnected(): break
            watchdog.feed()
            time.sleep(1)
            
    if wlan.isconnected():
        print(f"WiFi: Connected! IP: {wlan.ifconfig()[0]}")
        try:
            uniqueMqttId = clientId + "_" + hexlify(machine.unique_id()).decode()
            client = MQTTClient(uniqueMqttId, mqttBroker, user=mqttUser, password=mqttPassword, keepalive=keepAliveInterval)
            
            # Connect to broker. Throws exception if failed.
            client.connect()
            
            # If we reach this line, connection was successful
            reconnectCount += 1
            failedConnAttempts = 0 
            print(f"--- SYSTEM READY (v{appVersion}) ---")
            return client
            
        except Exception as e: 
            print(f"MQTT Connection Error: {e}")
            pass
    
    # Increment failure count if WiFi or MQTT fails
    failedConnAttempts += 1
    if failedConnAttempts >= maxFailedAttempts: 
        print("Status: Max failures reached. Hard resetting device...")
        machine.reset()
        
    return None

def sendDiscovery(client):
    print("Status: Sending Discovery Burst...")
    try:
        # 1. Send Discovery for Temperature Sensors
        for s in tempSensors:
            payload = {
                "name": f"{s['sensorName']} Temperature", 
                "unique_id": f"{s['sensorId']}_T", 
                "state_topic": f"homeassistant/sensor/{s['sensorId']}/state",
                "unit_of_measurement": "°C", 
                "device_class": "temperature",
                "value_template": "{{ value_json.temperature }}",
                "device": {"identifiers": [clientId], "name": "Wall Solar Monitor"}
            }
            
            # Encode strings to bytes to prevent socket crash on degree symbol
            topicBytes = f"homeassistant/sensor/{s['sensorId']}_T/config".encode('utf-8')
            payloadBytes = json.dumps(payload).encode('utf-8')
            
            # Publish config using the encoded bytes
            client.publish(topicBytes, payloadBytes, retain=True)
            print(f"   > TX Discovery: {s['sensorId']}")
            time.sleep(0.5)
            
        # 2. Send Discovery for System Diagnostic Sensors
        for key, name, dClass, unit in systemDiagnostics:
            sysPayload = {
                "name": f"{name}",
                "unique_id": f"{clientId}_{key}",
                "state_topic": f"homeassistant/sensor/{clientId}_sys/state",
                "value_template": f"{{{{ value_json.{key} }}}}",
                "entity_category": "diagnostic",
                "device": {"identifiers": [clientId], "name": "Wall Solar Monitor"}
            }
            
            # Only append class and unit if they exist
            if dClass: sysPayload["device_class"] = dClass
            if unit: sysPayload["unit_of_measurement"] = unit
            
            sysTopicBytes = f"homeassistant/sensor/{clientId}_{key}/config".encode('utf-8')
            sysPayloadBytes = json.dumps(sysPayload).encode('utf-8')
            
            client.publish(sysTopicBytes, sysPayloadBytes, retain=True)
            print(f"   > TX Discovery: sys_{key}")
            time.sleep(0.5)
            
        return True
    except Exception as e:
        print(f"Discovery Failed: {e}")
        return False

# ==========================================
# 6. MAIN LOOP
# ==========================================
mqttClient = None
lastReadTime = 0
lastHeartbeatTime = 0
lastHeartbeatFlash = 0

while True:
    # Keep the watchdog happy
    watchdog.feed()
    now = time.time()

    # Reconnect logic if connection drops
    if not network.WLAN(network.STA_IF).isconnected() or mqttClient is None:
        onboardLed.on()
        mqttClient = connectToNetwork()
        if mqttClient: 
            if not sendDiscovery(mqttClient):
                # If discovery fails, kill the client so it reconnects cleanly
                mqttClient = None
                time.sleep(2)
                continue
            # Reset timers on fresh connection so we publish immediately
            lastReadTime = now - sensorInterval 
            lastHeartbeatTime = now - heartbeatInterval
        else: 
            time.sleep(5)
            continue

    # Check for incoming messages
    try: 
        mqttClient.check_msg()
    except: 
        mqttClient = None
        continue

    # Heartbeat LED flash to show programme is running
    if time.ticks_diff(time.ticks_ms(), lastHeartbeatFlash) > 1000:
        onboardLed.toggle()
        lastHeartbeatFlash = time.ticks_ms()

    # Sensor read cycle
    if now - lastReadTime >= sensorInterval:
        cycleStart = time.ticks_ms()
        forcePublish = (now - lastHeartbeatTime) >= heartbeatInterval
        
        try:
            # Read 1-Wire bus
            roms = dsSensor.scan()
            dsSensor.convert_temp()
            time.sleep_ms(750)
            
            for idx, rom in enumerate(roms[:4]):
                s = tempSensors[idx]
                val = round(dsSensor.read_temp(rom), 1)
                
                # Check for bad read values
                if val != 85.0 and val != -127.0:
                    # Logic: Only publish if value changed by threshold OR heartbeat timer tripped
                    if abs(val - s['lastTemp']) >= tempChangeThreshold or forcePublish:
                        try:
                            # Encode topic and payload to bytes for stability
                            topicBytes = f"homeassistant/sensor/{s['sensorId']}/state".encode('utf-8')
                            payloadBytes = json.dumps({"temperature": val}).encode('utf-8')
                            
                            # Attempt to publish to MQTT broker
                            mqttClient.publish(topicBytes, payloadBytes, retain=True)
                            s['lastTemp'] = val
                            
                            # Explicitly log the payload being sent
                            print(f" > MQTT TX [{s['sensorId']}]: {payloadBytes.decode('utf-8')}")
                        except:
                            # Only drop client if publish fails
                            print(f" ! MQTT publish failed for {s['sensorName']}")
                            mqttClient = None
        except Exception as e:
            # Catch 1-Wire errors without dropping MQTT connection
            print(f" ! 1-Wire bus read error: {e}")
            
        # System Diagnostics - Only publish on the heartbeat to reduce network noise
        if forcePublish and mqttClient is not None:
            try:
                wlanObj = network.WLAN(network.STA_IF)
                rssiVal = wlanObj.status('rssi')
                # Use absolute seconds for massive uptime stability
                uptimeSecs = time.time() - systemStartTime 
                
                diagPayload = {
                    "rssi": rssiVal,
                    "uptime": uptimeSecs,
                    "version": appVersion,
                    "reconnects": reconnectCount
                }
                
                diagTopicBytes = f"homeassistant/sensor/{clientId}_sys/state".encode('utf-8')
                diagPayloadBytes = json.dumps(diagPayload).encode('utf-8')
                
                mqttClient.publish(diagTopicBytes, diagPayloadBytes, retain=True)
                print(f" > MQTT TX [sys_diag]: {diagPayloadBytes.decode('utf-8')}")
                
                # Reset heartbeat timer after successful burst
                lastHeartbeatTime = now
                
            except Exception as e:
                print(f" ! System diagnostics publish failed: {e}")
            
        print(f"Status: Cycle completed in {time.ticks_diff(time.ticks_ms(), cycleStart)}ms\n")
        lastReadTime = now
        
        # Free up memory
        gc.collect()

    # Small delay to prevent CPU pegging
    time.sleep(0.1)