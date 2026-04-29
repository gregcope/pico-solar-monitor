# pico-solar-monitor
Rapberry Pico 2 W DS18B20 temp monitor publishing to MQTT e.g. Home Assistant

Uses a Raspberry Pico 2 W and micropython to read four DB18B20 sensors ever 10 secs and push temperatures to Home Assistant via MQTT if changed.   Every 60 secs it sends all data including diagnostic data like reconnects, version, RSSI and uptime.

It is designed to be outside, be simple and robust.

Requires `umqtt.simple` lib for MQTT.

Populate the `secrets_example.py` with your WIFI/MQTT details and renname it `secrets.py`
