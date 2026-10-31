"""Head-only control, readback and keepalive withdrawal for Stop."""

import asyncio
import contextlib
import logging
import time

from .protocol import foundation_status, head_payload, system_status, outlet_status, pinch_clear
from .transport import Transport, RadioError, ReadTimeout

LOG = logging.getLogger(__name__)


class MotionCancelled(RadioError):
    """The user stopped a position operation."""


class Controller:
    def __init__(self, config, transport_factory=Transport):
        self.config = config
        self.transport = transport_factory(config)
        self.state = None
        self.features = {}
        self.lights = {}
        self.updated = 0.0
        self.error = "Waiting for radio"
        self.busy = False
        self.stop_event = asyncio.Event()
        self.operation_lock = asyncio.Lock()
        self.worker = None
        self.closing = False

    async def read_state(self, keep=False):
        data = await self.transport.request(0x12, 2 if keep else 0)
        self.state = foundation_status(data)
        self.updated = time.monotonic()
        return self.state

    async def refresh(self, full=True):
        await self.read_state()
        if full:
            self.features = system_status(await self.transport.request(0x25))
            if self.features["has_light"]:
                for outlet in self.config["light_outlets"]:
                    self.lights[outlet] = outlet_status(await self.transport.request(0x14, outlet))
        self.error = ""

    def snapshot(self):
        age = int((time.monotonic() - self.updated) * 1000) if self.updated else None
        connected = bool(self.config["enabled"] and self.transport.ready and age is not None and age < 15000)
        state = self.state or {}
        return {"connected": connected, "transport": "rf", "foundation": bool(self.state),
                "model": "360 FlexFit", "head_only": True, "busy": self.busy,
                "moving": state.get("moving", False), "homing": state.get("homing", False),
                "fault": state.get("fault", False), "age_ms": age, "error": self.error,
                "positions": {"right_head": state["head"]} if self.state else {},
                "presets": list(self.config["presets"]),
                "light": {"available": self.features.get("has_light", False),
                          "on": any(item["on"] for item in self.lights.values()) if self.lights else None},
                "radio": {"channel": self.config["channel"], "rssi": self.transport.rssi},
                "stop_method": "withdraw_keepalive"}

    async def maintain(self):
        while not self.closing:
            try:
                async with self.operation_lock:
                    if self.config["enabled"]:
                        if not self.transport.ready:
                            await self.transport.open()
                        await self.refresh()
                    elif self.transport.ready:
                        await self.transport.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = str(exc)
                LOG.warning("Radio poll: %s", exc)
                # One lost packet should not reset a working radio connection.
                if not isinstance(exc, ReadTimeout):
                    await self.transport.close()
            await asyncio.sleep(2)

    def start(self):
        self.worker = asyncio.create_task(self.maintain())

    async def close(self):
        self.closing = True
        self.stop_event.set()
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        async with self.operation_lock:
            await self.transport.close()

    def check_motion(self):
        if not self.state or not self.state["configured"] or self.state["homing"]:
            raise RadioError("Foundation is not ready for position control")
        if self.state["fault"]:
            raise RadioError("Head actuator fault reported")

    async def stop(self):
        """Do not renew MicroAdjust. Plain status reads do not keep it moving.

        ForceIdle 0x02 was rejected during real motion, so it is not used here.
        The base's own MicroAdjust watchdog stops the motors after keepalive loss.
        """
        self.stop_event.set()
        deadline = time.monotonic() + 8
        stable = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            try:
                await self.read_state()
            except ReadTimeout:
                continue
            idle = not self.state["moving"] and not any(x & 1 for x in self.state["actuator_status"][:2])
            stable = stable + 1 if idle else 0
            if stable >= 2:
                return
        raise RadioError("Keepalive stopped; unable to confirm the base is idle")

    async def move(self, position):
        payload = head_payload(position)
        await self.read_state()
        self.check_motion()
        if self.state["moving"]:
            raise RadioError("Base is already moving")
        if self.stop_event.is_set():
            raise MotionCancelled("Stopped before movement began")
        if not pinch_clear(await self.transport.request(0x28)):
            raise RadioError("Head pinch/disconnection reported")
        # Check again after awaiting the safety read, before the physical write.
        if self.stop_event.is_set():
            raise MotionCancelled("Stopped before movement began")
        if self.state["head"] == position:
            return
        try:
            await self.transport.request(0x11, 0, payload)
            deadline = time.monotonic() + 90
            stable = failures = 0
            while time.monotonic() < deadline:
                if self.stop_event.is_set():
                    raise MotionCancelled("Stopped")
                # Event wait wakes immediately on Stop rather than renewing again.
                try:
                    await asyncio.wait_for(self.stop_event.wait(), 0.333)
                    raise MotionCancelled("Stopped")
                except TimeoutError:
                    pass
                try:
                    await self.read_state(keep=True)
                    failures = 0
                except ReadTimeout:
                    failures += 1
                    if failures >= 3:
                        raise RadioError("Three position replies missed; keepalive stopped")
                    continue
                self.check_motion()
                idle = not self.state["moving"] and not any(x & 1 for x in self.state["actuator_status"][:2])
                # Require exact flat; allow the base's observed rounding elsewhere.
                reached = self.state["head"] == 0 if position == 0 else abs(self.state["head"] - position) <= 3
                stable = stable + 1 if idle and reached else 0
                if stable >= 2:
                    return
                if idle and not reached:
                    raise RadioError(f"Base stopped at {self.state['head']}%; target was {position}%. Command was not repeated")
            raise RadioError("Position operation timed out")
        except BaseException:
            with contextlib.suppress(Exception):
                await self.stop()
            raise

    async def light(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        if not self.features.get("has_light"):
            await self.refresh()
        if not self.features.get("has_light"):
            raise RadioError("No underbed light reported")
        for outlet in self.config["light_outlets"]:
            await self.transport.request(0x13, outlet, bytes([enabled, 120 if enabled else 0, 0]))
            result = outlet_status(await self.transport.request(0x14, outlet))
            if result["on"] != enabled:
                raise RadioError("Light state was not confirmed")
            self.lights[outlet] = result

    async def execute(self, action, data):
        async with self.operation_lock:
            self.busy = True
            try:
                if action in ("connect", "reconnect"):
                    self.config["enabled"] = True
                    if not self.transport.ready:
                        await self.transport.open()
                    await self.refresh()
                elif action == "disconnect":
                    await self.stop()
                    self.config["enabled"] = False
                    await self.transport.close()
                elif action == "stop":
                    await self.stop()
                elif action == "refresh":
                    await self.refresh()
                elif action in ("position", "preset"):
                    if self.stop_event.is_set():
                        raise MotionCancelled("Cancelled by Stop")
                    position = data["value"] if action == "position" else self.config["presets"][data["preset"]]
                    await self.move(position)
                elif action == "light":
                    await self.light(data["enabled"])
                else:
                    raise ValueError("Unknown bed action")
                self.error = ""
            except Exception as exc:
                self.error = str(exc)
                raise
            finally:
                self.busy = False
