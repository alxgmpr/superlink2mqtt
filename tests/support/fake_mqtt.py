# tests/support/fake_mqtt.py
"""In-memory MQTT client double: records publishes/subscriptions, no broker."""


class FakeMqttClient:
    def __init__(self):
        self.published = []       # list of (topic, payload, retain)
        self.subscriptions = []
        self.lwt = None
        self.connected = False
        self.loop_running = False
        self.on_message = None
        self.on_connect = None
        self.on_disconnect = None
        self.auto_connect = True
        self.async_connect_called = False
        self.tls_enabled = False
        self.publish_waited = False

    def will_set(self, topic, payload=None, retain=False, qos=0):
        self.lwt = (topic, payload, retain)

    def username_pw_set(self, username, password=None):
        self.auth = (username, password)

    def tls_set(self):
        self.tls_enabled = True
        self.tls = True

    def reconnect_delay_set(self, min_delay, max_delay):
        self.reconnect_delays = (min_delay, max_delay)

    def connect_async(self, host, port=1883, keepalive=60):
        self.async_connect_called = True

    def is_connected(self):
        return self.connected

    def fire_on_connect(self):
        """Simulate a (re)connect: invoke on_connect with the paho v2 signature."""
        self.connected = True
        if self.on_connect is not None:
            self.on_connect(self, None, {}, 0, None)

    def fire_on_disconnect(self):
        self.connected = False
        if self.on_disconnect is not None:
            self.on_disconnect(self, None, {}, 0, None)

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))
        return FakePublishInfo(self)

    def loop_start(self):
        self.loop_running = True
        if self.auto_connect:
            self.fire_on_connect()

    def loop_stop(self):
        self.loop_running = False

    def disconnect(self):
        self.connected = False

    def find(self, topic):
        """Latest payload published to `topic`, or None."""
        for t, p, _ in reversed(self.published):
            if t == topic:
                return p
        return None


class FakePublishInfo:
    rc = 0

    def __init__(self, client):
        self.client = client

    def wait_for_publish(self, timeout):
        self.client.publish_waited = True

    def is_published(self):
        return True
