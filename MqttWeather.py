"""Weather provider that overlays MQTT readings on another provider.

Anything not published on MQTT — icons, pressure, hourly and daily forecast —
stays with weather-provider.  A reading is replaced only after its topic has
delivered a number, so the panel is not blank at startup.

Widgets expect Celsius, percent, km/h and degrees.  Each topic names the unit
its payload is already in; this converts before handing the number on.
"""
import json
import logging

from PyQt5.QtCore import QObject, pyqtSignal

from PiClock3.Weather import Weather

logger = logging.getLogger(__name__)

# conditions() key -> quantity in PiClock3/units, and the unit widgets expect
FIELDS = {
    'temp':       ('temperature', 'C'),
    'feels-like': ('temperature', 'C'),
    'dew':        ('temperature', 'C'),
    'humidity':   ('percent',     '%'),
    'wind':       ('speed',       'kph'),
    'gust':       ('speed',       'kph'),
    'wind-dir':   ('direction',   'deg'),
    'pressure':   ('pressure',    'hPa'),
}

# names a payload may use that are not keys in quantities.yaml
UNIT_ALIAS = {
    'FAHRENHEIT': 'F', 'CELSIUS': 'C', 'KELVIN': 'K',
    'MPH': 'mph', 'KPH': 'kph', 'KMH': 'kph', 'KM/H': 'kph',
    'MPS': 'mps', 'M/S': 'mps',
    'KT': 'kt', 'KTS': 'kt', 'KNOT': 'kt', 'KNOTS': 'kt',
    'DEG': 'deg', 'DEGREES': 'deg', '°': 'deg',
    'HPA': 'hPa', 'MB': 'mb', 'INHG': 'inHg',
    '%': '%', 'PERCENT': '%', 'RH': '%',
}


class _Bridge(QObject):
    """MQTT callbacks arrive off the Qt thread; this hops them back."""
    reading = pyqtSignal(str, float)   # conditions key, value in widget units


class MqttWeather(Weather):

    attribution = ''

    def __init__(self, piclock, name, config):
        super().__init__(piclock, name, config)
        self.source = None
        self.listeners = []
        self.mqtt = {}          # field -> value in the unit widgets expect
        self.readings = {}      # field -> {topic, unit, payload-key}
        self.client = None
        self.bridge = _Bridge()
        self.bridge.reading.connect(self._gotReading)

    # ---------------------------------------------------------------- lifecycle

    def start(self):
        source_name = self.config.get('weather-provider')
        if not source_name:
            raise ValueError(
                '%s needs weather-provider: the Open-Meteo / METAR / … '
                'instance that still supplies what MQTT does not'
                % self.name)
        try:
            self.source = self.piclock.plugins[source_name]
        except KeyError:
            raise ValueError(
                '%s weather-provider %r is not in providers:'
                % (self.name, source_name))

        self.attribution = self.source.attribution or source_name
        self.source.subscribe(self._sourceUpdated)
        self.readings = self._readings()
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
        for field, value in self.mqtt.items():
            overlay[field] = value
            overlay[field + '-source'] = 'mqtt'
        return overlay

    def hourly(self, count, step):
        return [] if self.source is None else self.source.hourly(count, step)

    def daily(self, count):
        return [] if self.source is None else self.source.daily(count)

    # ---------------------------------------------------------------- config

    def _readings(self):
        """mqtt-topics:, with the old single-topic keys folded in."""
        raw = self.config.get('mqtt-topics') or {}
        readings = {}
        if isinstance(raw, dict):
            for field, spec in raw.items():
                if isinstance(spec, str):
                    spec = {'topic': spec}
                if isinstance(spec, dict) and spec.get('topic'):
                    readings[field] = spec

        legacy_unit = self.config.get('mqtt-temp-unit') or 'C'
        legacy = (
            ('temp', 'mqtt-temp-topic', self.config.get('payload-key')),
            ('humidity', 'mqtt-humidity-topic',
             self.config.get('humidity-payload-key')),
        )
        for field, key, payload_key in legacy:
            topic = (self.config.get(key) or '').strip()
            if topic and field not in readings:
                readings[field] = {
                    'topic': topic,
                    'unit': legacy_unit if field == 'temp' else '',
                    'payload-key': payload_key or '',
                }
        return readings

    # ---------------------------------------------------------------- MQTT

    def _connectMqtt(self):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.error(
                '%s needs paho-mqtt  (pip3 install paho-mqtt)', self.name)
            return

        if not self.readings:
            logger.error('%s needs mqtt-topics: (or mqtt-temp-topic:)', self.name)
            return

        host = self.config.get('mqtt-host') or 'localhost'
        port = int(self.config.get('mqtt-port') or 1883)
        client_id = self.config.get('mqtt-client-id') or 'piclock3-mqttweather'
        kwargs = {'client_id': client_id}
        version = getattr(mqtt, 'CallbackAPIVersion', None)
        if version is not None:
            kwargs['callback_api_version'] = version.VERSION1
        self.client = mqtt.Client(**kwargs)
        self.client.reconnect_delay_set(min_delay=2, max_delay=60)

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
        seen = []
        for spec in self.readings.values():
            topic = self.expand(str(spec.get('topic') or '')).strip()
            if topic and topic not in seen:
                seen.append(topic)
        return seen

    def _onConnect(self, client, userdata, flags, rc):
        if rc != 0:
            logger.warning('%s mqtt connect rc=%s', self.name, rc)
            return
        for topic in self._topics():
            client.subscribe(topic)
            logger.info('%s subscribed %s', self.name, topic)

    def _onDisconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning('%s mqtt disconnected rc=%s', self.name, rc)

    def _onMessage(self, client, userdata, msg):
        payload = msg.payload.decode('utf-8', errors='replace').strip()
        if payload.lower() in ('', 'unknown', 'unavailable', 'none', 'null'):
            return
        for field, spec in self.readings.items():
            topic = self.expand(str(spec.get('topic') or '')).strip()
            if topic != msg.topic:
                continue
            value = self._parsePayload(payload, spec.get('payload-key'))
            if value is None:
                logger.warning('%s unreadable %s %r', self.name, field, payload)
                continue
            converted = self._convert(field, value, spec.get('unit'))
            if converted is not None:
                self.bridge.reading.emit(field, converted)

    def _gotReading(self, field, value):
        previous = self.mqtt.get(field)
        if previous is not None and abs(previous - value) < 0.01:
            return
        self.mqtt[field] = value
        logger.info('%s mqtt %s %.2f', self.name, field, value)
        self._notify()

    def _sourceUpdated(self):
        self._notify()

    def _notify(self):
        for fn in self.listeners:
            fn()

    # ---------------------------------------------------------------- payload

    def _parsePayload(self, payload, key):
        """A bare number, or a JSON object using a dotted key / common names."""
        key = (key or self.config.get('payload-key') or '').strip()
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
                for guess in ('value', 'state', 'temperature', 'temp',
                              'humidity', 'relative_humidity', 'rh',
                              'wind', 'speed', 'gust', 'direction'):
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

    def _convert(self, field, value, unit):
        """payload unit -> the unit CurrentConditions already asks for."""
        quantity, target = FIELDS.get(field, (None, None))
        if quantity is None:
            logger.warning('%s ignoring unknown mqtt field %r', self.name, field)
            return None
        if field == 'humidity':
            return self._toPercent(value)

        name = UNIT_ALIAS.get(str(unit or '').strip().upper(),
                              (unit or '').strip())
        if not name or name == target:
            return float(value)
        try:
            return self.piclock.units.convert(quantity, name, target, float(value))
        except SystemExit:
            logger.warning('%s unknown unit %r for %s', self.name, unit, field)
            return None

    @staticmethod
    def _toPercent(value):
        """Accept 0–100 or a 0–1 fraction."""
        if 0.0 <= value <= 1.0:
            return value * 100.0
        if value < 0:
            return 0.0
        if value > 100:
            return 100.0
        return value