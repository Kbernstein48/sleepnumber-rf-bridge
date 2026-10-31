"""Exclusive EZSP manufacturing-radio transport; never joins a Zigbee network."""

import asyncio
from collections import deque
import contextlib
import time

from .protocol import decode, encode, matches


class RadioError(RuntimeError):
    """A radio operation failed or the base rejected a command."""


class ReadTimeout(RadioError):
    """A reply was not received. A write may still have taken effect."""


class Transport:
    def __init__(self, config, factory=None):
        self.config, self.factory = config, factory
        self.target = int(config["target"], 16)
        self.radio = None
        self.lock_file = None
        self.original_channel = None
        self.callback_id = None
        self.started = self.ready = False
        self.queue = asyncio.Queue(maxsize=128)
        self.request_lock = asyncio.Lock()
        self.rssi = None
        self.recent = deque(maxlen=20)

    def received(self, name, values):
        if name != "mfglibRxHandler":
            return
        _, rssi, raw = values
        frame = decode(bytes(raw))
        if not frame or frame.source != self.target or frame.origin != self.target or frame.destination != 0:
            return
        self.rssi = int(rssi)
        self.recent.append({"command": frame.command, "qualifier": frame.qualifier, "rssi": self.rssi})
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait((time.monotonic(), frame))

    async def open(self):
        import fcntl
        if self.ready:
            return
        if self.factory is None:
            from bellows.ezsp import EZSP
            self.factory = EZSP
        try:
            self.lock_file = open(self.config["lock_file"], "a")
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.radio = self.factory({"path": self.config["device"], "baudrate": 57600, "flow_control": "hardware"})
            await asyncio.wait_for(self.radio.connect(), 15)
            await asyncio.wait_for(self.radio.version(), 5)
            (network,) = await self.radio.networkState()
            if int(network):
                raise RadioError("Radio belongs to a Zigbee network; refusing to take it over")
            self.callback_id = self.radio.add_callback(self.received)
            (status,) = await self.radio.mfglibStart(rxCallback=True)
            self.check(status, "Start radio")
            self.started = True
            (self.original_channel,) = await self.radio.mfglibGetChannel()
            (status,) = await self.radio.mfglibSetChannel(channel=self.config["channel"])
            self.check(status, "Set channel")
            (channel,) = await self.radio.mfglibGetChannel()
            if int(channel) != self.config["channel"]:
                raise RadioError("Channel verification failed")
            self.ready = True
        except BaseException:
            await self.close()
            raise

    @staticmethod
    def check(status, operation):
        if int(status):
            raise RadioError(f"{operation}: {status}")

    async def set_channel(self, channel):
        """Retune without changing the persisted configuration."""
        if type(channel) is not int or not 11 <= channel <= 26:
            raise ValueError("Invalid RF channel")
        if not self.ready:
            raise RadioError("Radio disconnected")
        async with self.request_lock:
            (status,) = await asyncio.wait_for(self.radio.mfglibSetChannel(channel=channel), 3)
            self.check(status, "Set channel")

    async def close(self):
        self.ready = False
        try:
            if self.radio is not None:
                if self.started:
                    try:
                        if self.original_channel is not None:
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(self.radio.mfglibSetChannel(channel=self.original_channel), 2)
                    finally:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(self.radio.mfglibEnd(), 2)
                if self.callback_id is not None:
                    self.radio.remove_callback(self.callback_id)
                with contextlib.suppress(Exception):
                    await self.radio.disconnect()
        finally:
            if self.lock_file is not None:
                self.lock_file.close()
            self.lock_file = self.radio = self.callback_id = self.original_channel = None
            self.started = False

    async def request(self, command, qualifier=0, payload=b"", timeout=1.0):
        if not self.ready:
            raise RadioError("Radio disconnected")
        async with self.request_lock:
            while not self.queue.empty():
                self.queue.get_nowait()
            sent = time.monotonic()
            try:
                (status,) = await asyncio.wait_for(self.radio.mfglibSendPacket(
                    packetContents=encode(self.target, command, qualifier, payload)), timeout)
                self.check(status, "Transmit")
                deadline = time.monotonic() + timeout
                while True:
                    stamp, frame = await asyncio.wait_for(self.queue.get(), max(0.001, deadline - time.monotonic()))
                    if stamp < sent or not matches(frame, self.target, command, qualifier, bool(payload)):
                        continue
                    if frame.qualifier in (14, 15):
                        raise RadioError(f"Base rejected function 0x{command:02x}")
                    return frame.payload
            except TimeoutError as exc:
                # There is no transaction identifier: never repeat an uncertain write.
                raise ReadTimeout(f"No reply to 0x{command:02x}; command was not repeated") from exc
