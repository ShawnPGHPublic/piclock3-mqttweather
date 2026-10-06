# PiClock3 MqttWeather

Weather data provider plugin: uses local weather temperature and
humidity if available via listening to a MQTT topic.

## Install

```
cd ~/PiClock3
export PIP_BREAK_SYSTEM_PACKAGES=1
python3 -m pip install paho-mqtt
git clone https://github.com/ShawnPGHPublic/piclock3-mqttweather plugins/MqttWeather
```

## Test

python3 PyQtPiClock3.py plugins/MqttWeather/examples/mqttweather.yaml

------------------------------------------------------------------------

### Use it

Add the mqttweather provider to your config:

```
mqttweather:  
    plugin: plugins.MqttWeather
    weather-provider: openmeteo
    mqtt-host: mqtt.lan
    mqtt-username: '{apikeys.mqtt-user}'
    mqtt-password: '{apikeys.mqtt}'
    mqtt-topics:
      temp:       {topic: ha/sensor/outside_temperature/state, unit: F}
      humidity:   {topic: ha/sensor/outside_humidity/state}
      wind:       {topic: ha/sensor/outside_wind_speed/state, unit: mph}
      wind-dir:   {topic: ha/sensor/outside_wind_direction/state}
      gust:       {topic: ha/sensor/cotech_wind_gust/state, unit: mph}
      feels-like: {topic: ha/sensor/outside_feels_like/state, unit: F}
      
```

\***Note** it is using the openmeteo provider to fill in any missing weather data

For you weather widget use the mqttweather provider instead of
openmeteo:

```
current-conditions:
    plugin: PiClock3.CurrentConditions
    region: current
    conditions-provider: mqttweather
`
