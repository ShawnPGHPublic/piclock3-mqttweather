# PiClock3 MqttWeather

Weather data provider plugin: uses local weather temperature and humidity if available via listening to a MQTT topic.

## Install

cd ~/PiClock3

git clone https://github.com/ShawnPGHPublic/piclock3-mqttweather plugins/mqttweather

## Test

python3 PyQtPiClock3.py examples/mqttweather.yaml

---

### Use it

Add the mqttweather provider to your config:

`  mqttweather:  
    plugin: plugins.MqttWeather  
    weather-provider: openmeteo  
    mqtt-host: localhost  
    mqtt-port: 1883  
    mqtt-temp-topic: home/outdoor/temperature  
    mqtt-humidity-topic: home/outdoor/humidity  
    mqtt-unit: F`  

***Note** it is using the openmeteo to fill in any missing weather data

For you weather widget use the mqttweather provider instead of openmeteo:

`  forecast:  
    plugin: PiClock3.Forecast  
    region: forecast  
    forecast-provider: mqttweather`  



