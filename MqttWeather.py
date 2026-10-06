"""Weather provider that overlays MQTT temperature and humidity.

Wind, pressure, feels-like, icons, hourly and daily forecasts stay with the
named weather-provider (Open-Meteo, METAR, …).  conditions()['temp'] and
conditions()['humidity'] are replaced when those MQTT payloads have arrived.
Until then the wrapped source's own readings are shown so the panel is not
blank at startup.
"""
import json
import logging

from PyQt5.QtCore import QObject, pyqtSignal

from PiClock3.Weather import Weather

logger = logging.getLogger(__name__)


class _Bridge(QObject):
    """MQTT callbacks arrive off the Qt thread; this hops them back."""
    reading = pyqtSignal(str, float)   # kind ('temp' | 'humidity'), value


class MqttWeather(Weather):

    attribution = ''

    def __init__(self, piclock, name, config):
        super().__init__(piclock, name, config)
        self.source = None
        self.listeners = []
        self.mqtt_temp = None       # Celsius, or None before the first message
        self.mqtt_humidity = None   # percent, or None before the first message
        self.client = None
        self.temp_topic = ''
        self.humidity_topic = ''
        self.bridge = _Bridge()
        self.bridge.reading.connect(self._gotReading)

    # ---------------------------------------------------------------- lifecycle

    def start(self):
        source_name = self.config.get('weather-provider')
        if not source_name:
            raise ValueError(
                '%s needs weather-provider: the Open-Meteo / METAR / … '
                'instance that still supplies wind and forecast'
                % self.name)
        try:
            self.source = self.piclock.plugins[source_name]
        except KeyError:
            raise ValueError(
                '%s weather-provider %r is not in providers:'
                % (self.name, source_name))

        self.attribution = self.source.attribution or source_name
        self.source.subscribe(self._sourceUpdated)
        self._connectMqtt()

    def pageChange(self):
        return

    # ---------------------------------------------------------------- Weather

    def subscribe(self, fn):
        self.listeners.append(fn)
        if self.conditions():
            fn()

    def conditions(self):
        if self.source is None:
            return None
        now = self.source.conditions()
        if not now:
            return now
        overlay = dict(now)
        if self.mqtt_temp is not None:
            overlay['temp'] = self.mqtt_temp
            overlay['temp-source'] = 'mqtt'
        if self.mqtt_humidity is not None:
            overlay['humidity'] = self.mqtt_humidity
            overlay['humidity-source'] = 'mqtt'
        return overlay

    def hourly(self, count, step):
        return [] if self.source is None else self.source.hourly(count, step)

    def daily(self, count):
        return [] if self.source is None else self.source.daily(count)

    # ---------------------------------------------------------------- MQTT

    def _connectMqtt(self):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.error(
                '%s needs paho-mqtt  (pip3 install paho-mqtt)', self.name)
            return

        host = self.config.get('mqtt-host') or 'localhost'
        port = int(self.config.get('mqtt-port') or 1883)
        self.temp_topic = (self.config.get('mqtt-temp-topic') or '').strip()
        self.humidity_topic = (
            self.config.get('mqtt-humidity-topic') or '').strip()
        if not self.temp_topic and not self.humidity_topic:
            logger.error(
                '%s needs mqtt-temp-topic: and/or mqtt-humidity-topic:', self.name)
            return

        client_id = self.config.get('mqtt-client-id') or 'piclock3-mqttweather'
        kwargs = {'client_id': client_id}
        version = getattr(mqtt, 'CallbackAPIVersion', None)
        if version is not None:
            kwargs['callback_api_version'] = version.VERSION1
        self.client = mqtt.Client(**kwargs)

        user = self.expand(self.config.get('mqtt-username') or '')
        password = self.expand(self.config.get('mqtt-password') or '')
        if user:
            self.client.username_pw_set(user, password)
        if self.config.get('tls'):
            self.client.tls_set()

        self.client.on_connect = self._onConnect
        self.client.on_message = self._onMessage
        self.client.on_disconnect = self._onDisconnect

        logger.info('%s connecting mqtt://%s:%s', self.name, host, port)
        try:
            self.client.connect(host, port, keepalive=60)
        except Exception as exc:
            logger.warning('%s mqtt connect failed: %s', self.name, exc)
            return
        self.client.loop_start()

    def _topics(self):
        topics = []
        if self.temp_topic:
            topics.append(self.temp_topic)
        if self.humidity_topic and self.humidity_topic not in topics:
            topics.append(self.humidity_topic)
        return topics

    def _onConnect(self, client, userdata, flags, rc):
        if rc != 0:
            logger.warning('%s mqtt connect rc=%s', self.name, rc)
            return
        for topic in self._topics():
            client.subscribe(topic)
            logger.info('%s subscribed %s', self.name, topic)

    def _onDisconnect(self, client, userdata, rc):
        logger.warning('%s mqtt disconnected rc=%s', self.name, rc)

    def _onMessage(self, client, userdata, msg):
        payload = msg.payload.decode('utf-8', errors='replace').strip()
        topic = msg.topic

        same_topic = (
            self.temp_topic
            and self.humidity_topic
            and self.temp_topic == self.humidity_topic
        ) or (self.temp_topic and not self.humidity_topic)

        if topic == self.temp_topic:
            value = self._parsePayload(payload, self.config.get('payload-key'),
                                       ('temperature', 'temp', 'value', 'state'))
            if value is not None:
                self.bridge.reading.emit('temp', self._toCelsius(value))
            elif not same_topic:
                logger.warning('%s unreadable temperature %r', self.name, payload)

        if topic == self.humidity_topic or (same_topic and topic == self.temp_topic):
            value = self._parsePayload(
                payload,
                self.config.get('humidity-payload-key'),
                ('humidity', 'relative_humidity', 'rh', 'value', 'state'))
            if value is not None:
                self.bridge.reading.emit('humidity', self._toPercent(value))
            elif topic == self.humidity_topic and not same_topic:
                logger.warning('%s unreadable humidity %r', self.name, payload)

    def _gotReading(self, kind, value):
        if kind == 'temp':
            if self.mqtt_temp is not None and abs(self.mqtt_temp - value) < 0.01:
                return
            self.mqtt_temp = value
            logger.info('%s mqtt temperature %.2f C', self.name, value)
        elif kind == 'humidity':
            if (self.mqtt_humidity is not None
                    and abs(self.mqtt_humidity - value) < 0.05):
                return
            self.mqtt_humidity = value
            logger.info('%s mqtt humidity %.1f %%', self.name, value)
        else:
            return
        self._notify()

    def _sourceUpdated(self):
        self._notify()

    def _notify(self):
        for fn in self.listeners:
            fn()

    # ---------------------------------------------------------------- payload

    def _parsePayload(self, payload, key, guesses):
        """A bare number, or a JSON object using a dotted key / common names."""
        key = (key or '').strip()
        if not key:
            try:
                return float(payload)
            except ValueError:
                pass
            try:
                obj = json.loads(payload)
            except ValueError:
                return None
            if isinstance(obj, (int, float)):
                return float(obj)
            if isinstance(obj, dict):
                for guess in guesses:
                    if guess in obj:
                        try:
                            return float(obj[guess])
                        except (TypeError, ValueError):
                            return None
            return None

        try:
            obj = json.loads(payload)
        except ValueError:
            return None
        for part in key.split('.'):
            if not isinstance(obj, dict) or part not in obj:
                return None
            obj = obj[part]
        try:
            return float(obj)
        except (TypeError, ValueError):
            return None

    def _toCelsius(self, value):
        unit = (self.config.get('mqtt-unit') or 'C').strip().upper()
        if unit in ('F', 'FAHRENHEIT'):
            return (value - 32.0) * 5.0 / 9.0
        if unit in ('K', 'KELVIN'):
            return value - 273.15
        return value

    def _toPercent(self, value):
        """Accept 0–100 or a 0–1 fraction."""
        if 0.0 <= value <= 1.0:
            return value * 100.0
        if value < 0:
            return 0.0
        if value > 100:
            return 100.0
        return value
