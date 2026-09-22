# Wall Solar Monitor

MicroPython firmware designed for a Raspberry Pi Pico 2 W to track the performance and thermal characteristics of a home solar panel array via MQTT and Home Assistant auto-discovery.

## Overview

This project provides robust, headless environmental telemetry for a domestic solar installation. It polls four DS18B20 temperature probes on a single 1-wire bus, applies median filtering and delta thresholds, and publishes data continuously to Home Assistant. 

## Architectural Decisions and Core Design Logic

- **No Local Logging**: The system intentionally omits local file logging to avoid unnecessary write wear on the microcontroller flash storage, routing all diagnostics and telemetry live via MQTT to Home Assistant.
- **Resilient Network Management**: Employs an exponential backoff retry loop coupled with an 8-second hardware watchdog timer. If Wi-Fi or MQTT communication fails repeatedly, the system safely backs off before triggering a clean hardware reset.
- **Sensor Noise Reduction**: Implements a 3-sample median filter combined with a 0.3°C delta threshold to eliminate raw sensor jitter and prevent excessive MQTT database chatter.
- **Over-The-Air (OTA) Capability**: Includes an asynchronous MQTT listener and ugit integration to download and flash new firmware seamlessly without physical access.

## Hardware Architecture and Wiring

- **Microcontroller**: Raspberry Pi Pico 2 W
- **Sensors**: Four DS18B20 temperature probes (designated Bottom, Middle, TopL, and TopR) sharing a single 1-wire bus on GPIO 15.

### Pinout Connections and Hardware Notes

| Component Pin | Pico 2 W Pin | Description |
| :--- | :--- | :--- |
| VCC (Red) | 3V3 (Pin 36) | Power supply (3.3V) |
| GND (Black/Blue) | GND (Pin 38) | System Ground |
| Data (Yellow/White) | GP15 (Pin 20) | 1-Wire Data Line |

- **One-Wire Pull-Up**: Incorporates two 4.7kΩ resistors in parallel on the 1-wire data line to ensure signal integrity and proper rising-edge performance across extended cable runs to the solar array.
- **DC Input Filtering**: Features a ferrite bead (ferrule) on the incoming 5V DC power rail to suppress high-frequency electrical noise and switching transients from nearby inverter equipment.

## Installation and Configuration

### Prerequisites

1. Ensure your Raspberry Pi Pico 2 W is flashed with the latest stable MicroPython firmware.
2. Use an IDE or tool such as Thonny or mpremote to manage file transfers.

### File Deployment

Upload the following files to the root directory of the device:
- boot.py: Handles low-level startup validation and watchdog binding.
- main.py: Contains the core asynchronous sensor polling, median filter, and MQTT loop.
- secrets.py: Contains your localized Wi-Fi credentials and MQTT broker settings (excluded from version control).
- ugit.py: Manages lightweight file synchronisation and OTA code updates.
- umqtt/simple.py: The standard lightweight MQTT client library for MicroPython.

### Credentials Configuration (secrets.py)

Create a secrets.py file on your local machine containing:

wifiSsid = "YOUR_WIFI_SSID"
wifiPassword = "YOUR_WIFI_PASSWORD"
mqttBroker = "YOUR_MQTT_BROKER_IP"
mqttUser = "YOUR_MQTT_USERNAME"
mqttPassword = "YOUR_MQTT_PASSWORD"

### UgIt Configuration via REPL

1. Open your Thonny REPL (shell) and run the configuration helper to establish local sync settings:
   import ugit
   ugit.create_config()
2. Enter your Wi-Fi credentials, GitHub repository details, and specify the standard ignore list (secrets.py, config.json, and README.md). This generates a local config.json file that excludes sensitive parameters and documentation from being pushed back to GitHub.
3. Ensure your editor uses Unix LF line endings to avoid unnecessary file overwrites during synchronisation. Do not commit config.json to version control.

## Home Assistant Configuration Examples

### Lovelace Dashboard Sensor Card Example

type: entities
title: Wall Solar Array Temperatures
entities:
  - entity: sensor.wall_solar_monitor_bottom_temperature
    name: Bottom Array
  - entity: sensor.wall_solar_monitor_middle_temperature
    name: Middle Array
  - entity: sensor.wall_solar_monitor_topl_temperature
    name: Top Left Array
  - entity: sensor.wall_solar_monitor_topr_temperature
    name: Top Right Array
  - entity: sensor.wall_solar_monitor_status
    name: System Status

## Troubleshooting and Diagnostic Procedures

### 1. One-Wire Bus Errors
- **Symptom**: System status reports a bus fault or all sensors drop simultaneously.
- **Action**: Verify the parallel 4.7kΩ pull-up configuration between the 3.3V rail and the data line on GPIO 15. Check for loose terminal block screws or moisture ingress along the sensor cable run.

### 2. Single Sensor Offline or Reading 85.0°C
- **Symptom**: A specific probe displays 85.0°C or switches availability to offline.
- **Action**: An 85.0°C reading is the default power-on reset state of the DS18B20 chip, indicating a parasitic power drop or intermittent data line contact during conversion. Inspect the individual probe solder joints or crimp connectors.

### 3. MQTT Disconnection Loops
- **Symptom**: The Pico repeatedly resets or spends extended periods in the connecting state.
- **Action**: Check network signal strength via the rssi diagnostic entity. Confirm that the broker credentials stored in secrets.py are correct and that the MQTT keepalive grace period is respected.

## Features

- **Home Assistant Device Registry Integration**: Automatically registers the Pico as a first-class device, populating the serial number metadata and firmware version reporting directly into a unified device card.
- **System Diagnostics**: Publishes internal CPU temperature, Wi-Fi signal strength, uptime, reconnect counts, and last reset reasons.
- **Fault Isolation**: If a single DS18B20 probe drops from the 1-wire bus, the logic flags only that specific sensor as offline while keeping the broader system status healthy, preventing transient bus jitter from cascading into a global system fault.

## Major Version History

- **v1.7.0**: Upgraded underlying architecture to match the MVHR asynchronous logic[cite: 4]. Introduced OTA firmware update capability via MQTT and ugit, formal Device Registry integration, and enhanced fault-isolated system diagnostics.
- **v5.6.5**: Stabilized headless resilience with global system status strings, 8-second hardware watchdog timer, and network backoff logic[cite: 4].
