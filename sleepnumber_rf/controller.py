"""Head-only control, readback and keepalive withdrawal for Stop."""

import asyncio
import contextlib
import logging
import time

from .protocol import foundation_status, head_payload, system_status, outlet_status, pinch_clear
from .transport import Transport, RadioError, ReadTimeout

LOG = logging.getLogger(__name__)
POLL_TIMEOUT = 120


class MotionCancelled(RadioError):
    """The user stopped a position operation."""


class Controller:
    def __init__(self, config, transport_factory=Transport, *, clock=None):
        self.clock = clock or time.monotonic
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
        self.discovering = False
        self.poll_failures = 0
        self.last_discovery = None
        self.config_changed = None
        self.recovery_failures = 0
        self.radio_restarts = 0
        self.recoveries = 0
        self.last_poll = self.clock()
        self.next_poll = 0.0
        self.next_features = 0.0
        self.next_light = 0.0
        self.lights_valid = False
        self.reset_for_outage = False

    def retry_delay(self):
        return min(600, self.config["idle_poll_seconds"] * 2 ** min(max(0, self.poll_failures - 1), 4))

    async def read_state(self, keep=False):
        try:
            data = await self.transport.request(0x12, 2 if keep else 0)
        except ReadTimeout:
            self.poll_failures += 1
            self.next_poll = self.clock() + self.retry_delay()
            raise
        self.state = foundation_status(data)
        self.updated = self.clock()
        self.poll_failures = 0
        self.recovery_failures = 0
        self.reset_for_outage = False
        self.next_poll = self.updated + (2 if self.state["moving"] else self.config["idle_poll_seconds"])
        return self.state

    async def refresh(self, full=True):
        await self.read_state()
        now = self.clock()
        if full or not self.features and now >= self.next_features:
            self.next_features = now + 600
            self.features = system_status(await self.transport.request(0x25))
        if self.features.get("has_light") and (full or now >= self.next_light):
            # Schedule before the read: an outlet timeout must not cause a retry
            # on every otherwise successful position poll.
            self.next_light = now + self.config["light_poll_seconds"]
            self.lights_valid = False
            try:
                for outlet in self.config["light_outlets"]:
                    self.lights[outlet] = outlet_status(await self.transport.request(0x14, outlet))
            except BaseException:
                self.lights_valid = False
                raise
            self.lights_valid = True
        self.error = ""

    def snapshot(self):
        age = int((self.clock() - self.updated) * 1000) if self.updated else None
        stale_after = max(15000, self.config["idle_poll_seconds"] * 2500)
        if self.state and self.state["moving"]:
            stale_after = 15000
        connected = bool(self.config["enabled"] and self.transport.ready and age is not None
                         and age < stale_after and not self.poll_failures)
        state = self.state or {}
        return {"connected": connected, "transport": "rf", "foundation": bool(self.state),
                "model": "360 FlexFit", "head_only": True, "busy": self.busy or self.discovering,
                "discovering": self.discovering,
                "moving": state.get("moving", False), "homing": state.get("homing", False),
                "fault": state.get("fault", False), "age_ms": age, "error": self.error,
                "positions": {"right_head": state["head"]} if self.state else {},
                "presets": list(self.config["presets"]),
                "light": {"available": self.features.get("has_light", False) and self.lights_valid,
                          "on": any(item["on"] for item in self.lights.values()) if self.lights_valid else None},
                "radio": {"channel": self.config["channel"], "rssi": self.transport.rssi,
                          "restarts": self.radio_restarts, "recoveries": self.recoveries,
                          "failed_searches": self.recovery_failures,
                          "traffic": getattr(self.transport, "statistics", lambda: {})()},
                "polling": {"idle_seconds": self.config["idle_poll_seconds"],
                            "light_seconds": self.config["light_poll_seconds"],
                            "next_check_seconds": round(max(0, self.next_poll - self.clock()), 1)},
                "stop_method": "withdraw_keepalive"}

    async def discover_channel(self):
        """Locate the configured address using plain, read-only status requests.

        The operation lock is held by the caller. Never scan during motion or
        replay a failed movement/light command after a channel is rediscovered.
        """
        original = self.config["channel"]
        found = False
        self.discovering = True
        self.error = "Searching RF channels for the base"
        self.last_discovery = self.clock()
        try:
            channels = [original] + [channel for channel in range(11, 27) if channel != original]
            for channel in channels:
                if self.closing or not self.config["enabled"]:
                    return False
                await self.transport.set_channel(channel)
                try:
                    # Two addressed replies reduce the risk of adopting a
                    # channel based on a single delayed/corrupted transaction.
                    foundation_status(await self.transport.request(0x12, timeout=0.9))
                    state = foundation_status(await self.transport.request(0x12, timeout=0.9))
                except (ReadTimeout, ValueError):
                    continue
                self.state = state
                self.updated = self.clock()
                self.config["channel"] = channel
                found = True
                self.poll_failures = 0
                self.next_poll = self.clock() + self.config["idle_poll_seconds"]
                self.reset_for_outage = False
                if channel != original and self.config_changed:
                    self.config_changed()
                self.error = ""
                LOG.info("Base status verified on RF channel %s", channel)
                return True
            self.error = "Base did not reply on channels 11–26; waiting before another search"
            return False
        finally:
            self.discovering = False
            self.last_discovery = self.clock()
            if not found:
                await self.transport.set_channel(original)

    async def recover_connection(self):
        """One bounded search, with at most one radio reset per outage.

        Called with operation_lock held. Never replay an actuator/light write.
        A base that has lost power must not cause an unbounded reset loop.
        """
        if await self.discover_channel():
            self.recovery_failures = 0
            self.recoveries += 1
            return True
        self.recovery_failures += 1
        if not self.closing and self.config["enabled"] and not self.reset_for_outage:
            self.error = "Reinitializing radio once for this outage; no duplicate channel search"
            LOG.warning(self.error)
            self.reset_for_outage = True
            self.radio_restarts += 1
            await self.transport.close()
            await self.transport.open()
        self.next_poll = self.clock() + self.retry_delay()
        self.error = "Base not responding; backing off radio requests"
        LOG.warning(self.error)
        return False

    async def poll_once(self):
        # Connection changes, ordinary reads, recovery scans and physical jobs
        # all share this lock. A scan therefore cannot interrupt MicroAdjust.
        async with self.operation_lock:
            if self.config["enabled"] and self.clock() < self.next_poll:
                return
            try:
                if self.config["enabled"]:
                    if not self.transport.ready:
                        await self.transport.open()
                    try:
                        await self.refresh(full=False)
                    except ReadTimeout as exc:
                        self.error = str(exc)
                        now = self.clock()
                        # Only primary-status failures count. Missing optional
                        # light/configuration telemetry cannot start a hunt.
                        delay = min(1800, 600 * 2 ** min(max(0, self.recovery_failures - 1), 2))
                        can_scan = self.last_discovery is None or now - self.last_discovery >= delay
                        if self.poll_failures >= 3 and can_scan:
                            await self.recover_connection()
                        else:
                            LOG.warning("Radio poll: %s", exc)
                elif self.transport.ready:
                    await self.transport.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = str(exc)
                self.poll_failures += 1
                self.next_poll = self.clock() + self.retry_delay()
                LOG.warning("Radio poll: %s", exc)
                await self.transport.close()

    async def maintain(self):
        while not self.closing:
            try:
                # A responsive HTTP server must not conceal a wedged radio worker.
                await asyncio.wait_for(self.poll_once(), POLL_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = "Radio worker recovered: " + (str(exc) or type(exc).__name__)
                LOG.exception(self.error)
                with contextlib.suppress(Exception):
                    await self.transport.close()
            finally:
                self.last_poll = self.clock()
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
        self.lights_valid = False
        for outlet in self.config["light_outlets"]:
            await self.transport.request(0x13, outlet, bytes([enabled, 120 if enabled else 0, 0]))
            result = outlet_status(await self.transport.request(0x14, outlet))
            if result["on"] != enabled:
                raise RadioError("Light state was not confirmed")
            self.lights[outlet] = result
        self.lights_valid = True
        self.next_light = self.clock() + self.config["light_poll_seconds"]

    async def execute(self, action, data):
        async with self.operation_lock:
            self.busy = True
            try:
                if action in ("connect", "reconnect"):
                    self.config["enabled"] = True
                    if action == "reconnect":
                        self.radio_restarts += 1
                        await self.transport.close()
                    if not self.transport.ready:
                        await self.transport.open()
                    try:
                        await self.refresh()
                    except ReadTimeout:
                        if not await self.recover_connection():
                            raise RadioError(self.error)
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
