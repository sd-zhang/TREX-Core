# mqtt/base_paho.py
"""
Paho‑MQTT backbone that is API‑identical to the gmqtt clients you already use.
No new hooks, no new names.

Subclass requirements
---------------------
• set self.SUBS in __init__        – list[ (topic, qos) ]
• set self.dispatch in __init__    – {segment: handler_fn}
• handlers accept the *raw* message dict exactly like before.

Everything else (connect, loop, queue, worker) is handled here.
"""

from __future__ import annotations
import queue
import threading
from typing import Dict, Any, Tuple, List, Callable

import paho.mqtt.client as mqtt
from cuid2 import Cuid
from abc import ABC, abstractmethod


class BaseMQTTClient(ABC):
    # subclasses fill these two in __init__
    SUBS: List[Tuple[str, int]] = []
    dispatch: Dict[str, Callable[[Dict[str, Any]], None]] = {}

    # ---------- ctor ----------
    def __init__(self, host: str, port: int = 1883, consumers: int = 1):
        self.host, self.port = host, port
        self.consumers = consumers
        self.client = mqtt.Client(
            client_id=Cuid(length=10).generate(),
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )

        # Queue name kept identical to your prior code
        self.msg_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()

        self.client.on_connect    = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message    = self._enqueue

        self._STOP: Dict[str, Any] = {"_stop": True}
        self._workers: list[threading.Thread] = []

    @abstractmethod
    def on_connect(self, client, userdata, flags, reason_code, properties):
        raise NotImplementedError

    @abstractmethod
    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        raise NotImplementedError

    def subscribe_common(self) -> None:
        """Call inside your own on_connect to subscribe everything in SUBS."""
        for topic, qos in self.SUBS:
            self.client.subscribe(topic, qos=qos)

    def _enqueue(self, client, userdata, message):
        self.msg_queue.put(
            {
                "topic": message.topic,
                "payload": message.payload.decode(),
                "properties": message.properties,
            }
        )

    def _dispatch(self, message: dict):
        """
        Same logic you used before:
        iterate reversed(topic.split('/')) and call first matching handler.
        """
        for segment in reversed(message["topic"].split("/")):
            handler = self.dispatch.get(segment)
            if handler:
                handler(message)
                return
            print("unrecognised topic:", message["topic"])

    def _message_processor(self) -> None:
        while True:
            msg = self.msg_queue.get()
            try:
                if msg is self._STOP:  # sentinel ⇒ shut down worker
                    return  # but still mark task_done below
                self._dispatch(msg)
            finally:
                self.msg_queue.task_done()  # always balance the get()

    # ---------- public lifecycle ----------
    def start_client(self):
        self.client.connect(self.host, self.port, keepalive=60)
        self.client.loop_start()

        # spin up N consumer threads
        for _ in range(self.consumers):
            t = threading.Thread(target=self._message_processor,
                                 name="message_processor")
                                 # daemon=True)
            t.start()
            self._workers.append(t)

    def stop_client(self) -> None:
        # unblock each worker
        for _ in self._workers:
            self.msg_queue.put(self._STOP)
        for t in self._workers:
            t.join()
        self.client.loop_stop()
        self.client.disconnect()
