import asyncio
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

from sleepnumber_rf.config import validate, save, load
from sleepnumber_rf import __version__
from sleepnumber_rf.controller import Controller, MotionCancelled
from sleepnumber_rf.protocol import crc154, decode, encode, Frame, matches, head_payload, foundation_status
from sleepnumber_rf.server import make_app, SERVICE, Service
from sleepnumber_rf.transport import ReadTimeout, Transport, RadioError


def config(**kwargs):
    return validate({"target": "abcd", **kwargs})


def status(head, moving=False, motor=0):
    data = bytearray(15)
    data[0] = 64 | int(moving)
    data[1] = data[2] = head
    data[9] = data[10] = motor
    return bytes(data)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


async def due_poll(controller):
    controller.next_poll = 0
    await controller.poll_once()


class ProtocolTests(unittest.TestCase):
    def test_crc_reference_vector(self):
        self.assertEqual(crc154(b"123456789"), 0x2189)

    def test_request_layout_and_corruption(self):
        packet = encode(0xabcd, 0x11, payload=head_payload(40))
        self.assertEqual(packet[:12].hex(), "7eff420000abcd420000110c")
        self.assertEqual(packet[12:-2], b"\x28\x00" + b"\xff" * 10)
        self.assertIsNotNone(decode(packet))
        for index in range(len(packet)):
            damaged = bytearray(packet)
            damaged[index] ^= 1
            self.assertIsNone(decode(bytes(damaged)))

    def test_empty_ack_qualifier_and_routing(self):
        frame = Frame(0x41, 0xabcd, 0, 0x41, 0xabcd, 0x93, 0, b"")
        self.assertTrue(matches(frame, 0xabcd, 0x13, 3, True))
        self.assertFalse(matches(frame, 0xabcd, 0x13, 3, False))
        for bad in (replace(frame, source=1), replace(frame, destination=1),
                    replace(frame, origin=1), replace(frame, inner_node=0x11), replace(frame, command=0x92)):
            self.assertFalse(matches(bad, 0xabcd, 0x13, 3, True))

    def test_head_only_validation_and_motor_flag(self):
        self.assertEqual(head_payload(0)[2:], b"\xff" * 10)
        for bad in (-1, 101, True, 4.5, "20", None):
            with self.assertRaises(ValueError):
                head_payload(bad)
        self.assertFalse(foundation_status(status(40, True, 1))["fault"])
        self.assertTrue(foundation_status(status(40, False, 2))["fault"])

    def test_config_persistence_and_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            saved = save(path, config(presets={"favorite": 41}))
            self.assertEqual(load(path), saved)
            self.assertEqual(saved["presets"]["flat"], 0)
        for bad in ({"target": "ffff"}, {"channel": 10}, {"enabled": "true"},
                    {"light_outlets": [3, 3]}, {"presets": {"bad name": 40}}, {"password": "unused"},
                    {"idle_poll_seconds": 2}, {"idle_poll_seconds": True}, {"light_poll_seconds": 0}):
            with self.assertRaises(ValueError):
                config(**bad)


class FakeTransport:
    def __init__(self, configuration):
        self.config = configuration
        self.ready = True
        self.rssi = -45
        self.calls = []
        self.channel = configuration["channel"]
        self.channels = []
        self.keep = []
        self.plain = []
        self.current = status(40)

    async def open(self):
        self.ready = True

    async def close(self):
        self.ready = False

    async def set_channel(self, channel):
        self.channel = channel
        self.channels.append(channel)

    async def request(self, command, qualifier=0, payload=b"", **kwargs):
        self.calls.append((command, qualifier, payload))
        if command == 0x12:
            values = self.keep if qualifier == 2 else self.plain
            if values:
                value = values.pop(0)
                if isinstance(value, Exception):
                    raise value
                self.current = value
            return self.current
        if command == 0x25:
            return bytes.fromhex("03016411150000")
        if command == 0x28:
            return bytes(5)
        if command == 0x14:
            return bytes(3)
        return b""


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_read_does_not_repeat_motion_write(self):
        controller = Controller(config(), FakeTransport)
        controller.transport.keep = [status(39, True, 1), ReadTimeout("lost"), status(0, True, 1), status(0), status(0)]
        await controller.execute("position", {"value": 0})
        self.assertEqual(controller.state["head"], 0)
        self.assertEqual(sum(c[0] == 0x11 for c in controller.transport.calls), 1)

    async def test_stop_withdraws_keepalive_and_confirms_idle(self):
        controller = Controller(config(), FakeTransport)
        controller.transport.keep = [status(39, True, 1)]
        task = asyncio.create_task(controller.execute("position", {"value": 0}))
        while not any(c[:2] == (0x12, 2) for c in controller.transport.calls):
            await asyncio.sleep(0.01)
        count = len(controller.transport.calls)
        controller.transport.plain = [status(38, True, 1), status(38), status(38)]
        controller.stop_event.set()
        with self.assertRaises(MotionCancelled):
            await task
        after = controller.transport.calls[count:]
        self.assertFalse(any(c[:2] == (0x12, 2) for c in after))
        self.assertFalse(any(c[0] in (0x02, 0x11, 0x15) for c in after))
        self.assertFalse(controller.state["moving"])

    async def test_poll_start_never_moves_or_restores_light(self):
        controller = Controller(config(), FakeTransport)
        controller.start()
        await asyncio.sleep(0.03)
        await controller.close()
        self.assertTrue(controller.state)
        self.assertFalse(any(c[0] in (0x11, 0x13, 0x15, 0x24) for c in controller.transport.calls))

    async def test_stale_telemetry_unavailable(self):
        controller = Controller(config(), FakeTransport)
        await controller.refresh()
        self.assertTrue(controller.snapshot()["connected"])
        controller.updated = time.monotonic() - 151
        self.assertFalse(controller.snapshot()["connected"])

    async def test_stop_cancels_queued_motion_before_running(self):
        controller = Controller(config(), FakeTransport)
        service = Service(controller.config, controller=controller)
        motion = service.submit("position", {"value": 20})
        stop = service.submit("stop", {})
        self.assertEqual(motion["state"], "cancelled")
        self.assertEqual(service.pending[0]["job_id"], stop["job_id"])
        self.assertTrue(controller.stop_event.is_set())
        self.assertFalse(controller.transport.calls)


class ChannelRecoveryTests(unittest.IsolatedAsyncioTestCase):
    class ChangedChannel(FakeTransport):
        async def request(self, command, qualifier=0, payload=b"", **kwargs):
            if self.channel != 20:
                self.calls.append((command, qualifier, payload))
                raise ReadTimeout("No reply on old channel")
            return await super().request(command, qualifier, payload, **kwargs)

    class Offline(FakeTransport):
        async def request(self, command, qualifier=0, payload=b"", **kwargs):
            self.calls.append((command, qualifier, payload))
            raise ReadTimeout("No base")

    async def test_three_failed_polls_find_and_persist_new_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            controller = Controller(config(), self.ChangedChannel)
            service = Service(controller.config, path, controller)
            await due_poll(controller)
            await due_poll(controller)
            self.assertFalse(controller.transport.channels)
            await due_poll(controller)
            self.assertEqual(controller.config["channel"], 20)
            self.assertEqual(load(path)["channel"], 20)
            self.assertTrue(service.status()["bed"]["connected"])
            self.assertEqual(controller.poll_failures, 0)
            # Recovery must not replay a motor, native preset or light command.
            self.assertTrue(all(call[0] == 0x12 and call[1] == 0 for call in controller.transport.calls))
            self.assertFalse(service.jobs)

    async def test_offline_scan_restores_channel_and_backs_off(self):
        controller = Controller(config(), self.Offline)
        for _ in range(3):
            await due_poll(controller)
        self.assertEqual(controller.config["channel"], 15)
        self.assertEqual(controller.transport.channel, 15)
        self.assertEqual(set(controller.transport.channels), set(range(11, 27)))
        count = len(controller.transport.channels)
        for _ in range(4):
            await due_poll(controller)
        self.assertEqual(len(controller.transport.channels), count)
        self.assertFalse(controller.snapshot()["connected"])

    async def test_recovery_waits_for_active_operation_lock(self):
        controller = Controller(config(), self.ChangedChannel)
        controller.poll_failures = 3
        async with controller.operation_lock:
            task = asyncio.create_task(due_poll(controller))
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            self.assertFalse(controller.transport.calls)
        await task
        self.assertEqual(controller.config["channel"], 20)

    async def test_light_read_failures_do_not_trigger_channel_hunt(self):
        class LightTimeout(FakeTransport):
            async def request(self, command, qualifier=0, payload=b"", **kwargs):
                if command == 0x14:
                    raise ReadTimeout("Light read missing")
                return await super().request(command, qualifier, payload, **kwargs)
        controller = Controller(config(), LightTimeout)
        for _ in range(4):
            await due_poll(controller)
        self.assertFalse(controller.transport.channels)
        self.assertTrue(controller.snapshot()["connected"])

    async def test_deaf_radio_and_changed_channel_recover_without_writes(self):
        class DeafUntilReopened(self.ChangedChannel):
            reopened = False

            async def open(self):
                self.reopened = True
                await super().open()

            async def request(self, command, qualifier=0, payload=b"", **kwargs):
                if not self.reopened:
                    self.calls.append((command, qualifier, payload))
                    raise ReadTimeout("Radio session deaf")
                return await super().request(command, qualifier, payload, **kwargs)

        clock = Clock()
        controller = Controller(config(), DeafUntilReopened, clock=clock)
        for _ in range(3):
            await due_poll(controller)
        self.assertFalse(controller.snapshot()["connected"])
        self.assertEqual(len(controller.transport.channels), 17)
        clock.advance(601)
        await due_poll(controller)
        self.assertTrue(controller.snapshot()["connected"])
        self.assertEqual(controller.config["channel"], 20)
        self.assertEqual(controller.radio_restarts, 1)
        self.assertEqual(controller.recoveries, 1)
        self.assertTrue(all(c[:2] == (0x12, 0) for c in controller.transport.calls))

    async def test_powered_off_base_backs_off_resets_and_recovers_when_back(self):
        class PowerLoss(self.Offline):
            online = False

            async def request(self, *args, **kwargs):
                if self.online:
                    return await FakeTransport.request(self, *args, **kwargs)
                return await super().request(*args, **kwargs)

        controller = Controller(config(), PowerLoss)
        for _ in range(3):
            await due_poll(controller)
        self.assertEqual(controller.radio_restarts, 1)
        self.assertEqual(controller.recovery_failures, 1)
        first = len(controller.transport.channels)
        for _ in range(4):
            await due_poll(controller)
        self.assertEqual(len(controller.transport.channels), first)
        self.assertEqual(controller.radio_restarts, 1)
        self.assertFalse(controller.snapshot()["connected"])
        controller.transport.online = True
        await due_poll(controller)
        self.assertTrue(controller.snapshot()["connected"])
        self.assertEqual(controller.recovery_failures, 0)

    async def test_manual_reconnect_reopens_healthy_looking_session(self):
        class Counting(FakeTransport):
            closed = opened = 0

            async def close(self):
                self.closed += 1
                await super().close()

            async def open(self):
                self.opened += 1
                await super().open()

        controller = Controller(config(), Counting)
        await controller.execute("reconnect", {})
        self.assertEqual((controller.transport.closed, controller.transport.opened), (1, 1))
        self.assertTrue(controller.snapshot()["connected"])
        self.assertFalse(any(c[0] in (0x11, 0x13) for c in controller.transport.calls))

    async def test_hung_poll_is_bounded_and_worker_survives(self):
        controller = Controller(config(), FakeTransport)
        completed = asyncio.Event()
        calls = 0

        async def poll():
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.Event().wait()
            else:
                completed.set()

        controller.poll_once = poll
        with patch("sleepnumber_rf.controller.POLL_TIMEOUT", 0.01):
            controller.start()
            try:
                await asyncio.wait_for(completed.wait(), 3)
                self.assertFalse(controller.worker.done())
                self.assertFalse(controller.transport.ready)
            finally:
                await controller.close()


class PollingBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_position_and_preset_get_15_plain_followup_reads_then_idle(self):
        for action, data in (("position", {"value": 40}), ("preset", {"preset": "favorite"})):
            with self.subTest(action=action):
                clock = Clock()
                controller = Controller(config(presets={"favorite": 40}), FakeTransport, clock=clock)
                await controller.refresh()
                await controller.execute(action, data)
                controller.transport.calls.clear()
                for _ in range(30):
                    clock.advance(1)
                    await controller.poll_once()
                self.assertEqual(controller.transport.calls, [(0x12, 0, b"")] * 15)
                self.assertEqual(controller.snapshot()["polling"]["position_followup_remaining_seconds"], 0)
                clock.advance(59)
                await controller.poll_once()
                self.assertEqual(len(controller.transport.calls), 15)
                clock.advance(1)
                await controller.poll_once()
                self.assertEqual(len(controller.transport.calls), 16)

    async def test_another_adjustment_restarts_30_second_window(self):
        clock = Clock()
        controller = Controller(config(), FakeTransport, clock=clock)
        await controller.refresh()
        await controller.execute("position", {"value": 40})
        clock.advance(20)
        await controller.execute("position", {"value": 40})
        clock.advance(11)
        await controller.poll_once()
        self.assertEqual(controller.snapshot()["polling"]["position_followup_remaining_seconds"], 19)
        self.assertEqual(controller.next_poll - clock(), 2)

    async def test_followup_timeout_cancels_burst_and_preserves_backoff(self):
        clock = Clock()
        controller = Controller(config(), FakeTransport, clock=clock)
        await controller.refresh()
        await controller.execute("position", {"value": 40})
        controller.transport.calls.clear()
        controller.transport.plain = [ReadTimeout("No bed reply")]
        clock.advance(2)
        await controller.poll_once()
        self.assertEqual(controller.snapshot()["polling"]["position_followup_remaining_seconds"], 0)
        for _ in range(59):
            clock.advance(1)
            await controller.poll_once()
        self.assertEqual(len(controller.transport.calls), 1)
        clock.advance(1)
        await controller.poll_once()
        self.assertEqual(len(controller.transport.calls), 2)
        self.assertEqual(controller.next_poll - clock(), 60)

    async def test_failed_position_never_starts_followup(self):
        clock = Clock()
        controller = Controller(config(), FakeTransport, clock=clock)
        controller.transport.plain = [ReadTimeout("No bed")]
        with self.assertRaises(ReadTimeout):
            await controller.execute("position", {"value": 40})
        self.assertEqual(controller.snapshot()["polling"]["position_followup_remaining_seconds"], 0)
        clock.advance(2)
        await controller.poll_once()
        self.assertEqual(controller.transport.calls, [(0x12, 0, b"")])

    async def test_startup_refresh_and_light_do_not_start_fast_followup(self):
        controller = Controller(config(), FakeTransport)
        await controller.poll_once()
        await controller.execute("refresh", {})
        await controller.execute("light", {"enabled": False})
        self.assertEqual(controller.snapshot()["polling"]["position_followup_remaining_seconds"], 0)
        self.assertGreater(controller.next_poll - controller.clock(), 59)

    async def test_idle_hour_has_only_73_read_requests(self):
        clock = Clock()
        controller = Controller(config(), FakeTransport, clock=clock)
        for _ in range(3600):
            await controller.poll_once()
            clock.advance(1)
        calls = controller.transport.calls
        self.assertEqual(sum(c[0] == 0x12 for c in calls), 60)
        self.assertEqual(sum(c[0] == 0x25 for c in calls), 1)
        self.assertEqual(sum(c[0] == 0x14 for c in calls), 12)
        self.assertEqual(len(calls), 73)
        self.assertTrue(all(c[1] == 0 for c in calls if c[0] == 0x12))

    async def test_cached_position_is_valid_between_slow_polls(self):
        clock = Clock()
        controller = Controller(config(), FakeTransport, clock=clock)
        await controller.poll_once()
        clock.advance(59)
        self.assertTrue(controller.snapshot()["connected"])
        before = list(controller.transport.calls)
        await controller.poll_once()
        self.assertEqual(controller.transport.calls, before)
        clock.advance(92)
        self.assertFalse(controller.snapshot()["connected"])

    async def test_primary_timeout_backs_off_all_requests_and_marks_unavailable(self):
        clock = Clock()
        controller = Controller(config(), FakeTransport, clock=clock)
        await controller.poll_once()
        controller.transport.plain = [ReadTimeout("lost")]
        clock.advance(60)
        await controller.poll_once()
        count = len(controller.transport.calls)
        self.assertFalse(controller.snapshot()["connected"])
        for _ in range(59):
            clock.advance(1)
            await controller.poll_once()
        self.assertEqual(len(controller.transport.calls), count)
        clock.advance(1)
        await controller.poll_once()
        self.assertTrue(controller.snapshot()["connected"])

    async def test_offline_hour_is_bounded_without_duplicate_scans_or_resets(self):
        clock = Clock()
        controller = Controller(config(), ChannelRecoveryTests.Offline, clock=clock)
        for _ in range(3600):
            await controller.poll_once()
            clock.advance(1)
        self.assertLess(len(controller.transport.calls), 90)
        self.assertEqual(controller.radio_restarts, 1)
        self.assertEqual(controller.recovery_failures, 3)
        self.assertTrue(all(c[:2] == (0x12, 0) for c in controller.transport.calls))

    async def test_optional_light_failure_does_not_retry_each_position_poll(self):
        class MissingLight(FakeTransport):
            async def request(self, command, qualifier=0, payload=b"", **kwargs):
                if command == 0x14:
                    self.calls.append((command, qualifier, payload))
                    raise ReadTimeout("No outlet reply")
                return await super().request(command, qualifier, payload, **kwargs)

        clock = Clock()
        controller = Controller(config(), MissingLight, clock=clock)
        for _ in range(300):
            await controller.poll_once()
            clock.advance(1)
        self.assertEqual(sum(c[0] == 0x14 for c in controller.transport.calls), 1)
        self.assertEqual(sum(c[0] == 0x12 for c in controller.transport.calls), 5)
        self.assertTrue(controller.snapshot()["connected"])
        self.assertFalse(controller.snapshot()["light"]["available"])
        self.assertFalse(controller.transport.channels)

    async def test_explicit_refresh_bypasses_idle_deadline(self):
        controller = Controller(config(), FakeTransport)
        await controller.poll_once()
        count = len(controller.transport.calls)
        await controller.execute("refresh", {})
        self.assertEqual(len(controller.transport.calls) - count, 3)

    async def test_light_commands_get_immediate_readback_without_configuration_query(self):
        controller = Controller(config(), FakeTransport)
        await controller.poll_once()
        controller.transport.calls.clear()
        await controller.execute("light", {"enabled": False})
        self.assertEqual([c[0] for c in controller.transport.calls], [0x13, 0x14])
        self.assertTrue(controller.snapshot()["light"]["available"])


class RadioTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_change_is_verified(self):
        class Radio:
            async def mfglibSetChannel(self, **kwargs):
                return (0,)

            async def mfglibGetChannel(self):
                return (15,)

        transport = Transport(config())
        transport.radio = Radio()
        transport.ready = True
        with self.assertRaisesRegex(RadioError, "verification"):
            await transport.set_channel(20)

    async def test_uncertain_write_not_repeated(self):
        class Radio:
            count = 0
            async def mfglibSendPacket(self, **kwargs):
                self.count += 1
                return (0,)
        transport = Transport(config())
        transport.radio = Radio()
        transport.ready = True
        with self.assertRaises(ReadTimeout):
            await transport.request(0x11, payload=head_payload(40), timeout=0.02)
        self.assertEqual(transport.radio.count, 1)
        traffic = transport.statistics()
        self.assertEqual(traffic["request_attempts"], 1)
        self.assertEqual(traffic["by_function"], {"0x11/0": 1})
        self.assertEqual(traffic["timeouts"], {"0x11/0": 1})
        self.assertEqual(transport.statistics()["request_attempts"], 1)


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controller = Controller(config(), FakeTransport)
        self.client = TestClient(TestServer(make_app(self.controller.config, controller=self.controller)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_status_and_validation(self):
        result = await (await self.client.get("/api/v1/status")).json()
        self.assertEqual(result["device_id"], "sleepnumber_rf_abcd")
        for payload in ({"value": True}, {"value": 101}, {"value": 5, "axis": "foot"}, []):
            response = await self.client.post("/api/v1/bed/position", json=payload)
            self.assertEqual(response.status, 400)
        response = await self.client.post("/api/v1/bed/position", data="bad")
        self.assertEqual(response.status, 400)
        response = await self.client.get("/api/v1/jobs/unknown")
        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())["state"], "unknown")

    async def test_ha_client_end_to_end(self):
        path = Path(__file__).resolve().parents[1] / "custom_components/sleepnumber_rf/client.py"
        spec = importlib.util.spec_from_file_location("bridge_client", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        async with ClientSession() as session:
            client = module.BridgeClient(session, str(self.client.make_url("/")).rstrip("/"))
            self.assertEqual((await client.status())["version"], __version__)
            job = await client.command("refresh")
            self.assertEqual(job["state"], "succeeded")
            self.assertFalse(any(c[0] == 0x11 for c in self.controller.transport.calls))

    async def test_saved_preset_no_motion(self):
        response = await self.client.put("/api/v1/config", json={"presets": {"favorite": 37}})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["presets"], {"favorite": 37, "flat": 0})
        self.assertFalse(any(c[0] == 0x11 for c in self.controller.transport.calls))

    async def test_200_cached_http_reads_generate_no_radio_requests(self):
        await asyncio.sleep(0.01)
        before = list(self.controller.transport.calls)
        for _ in range(100):
            self.assertEqual((await self.client.get("/api/v1/status")).status, 200)
            self.assertEqual((await self.client.get("/api/v1/health")).status, 200)
        self.assertEqual(self.controller.transport.calls, before)

    async def test_health_distinguishes_offline_base_from_stuck_worker(self):
        await asyncio.sleep(0.01)
        self.controller.updated = time.monotonic() - 151
        response = await self.client.get("/api/v1/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"healthy": True, "bed_connected": False})
        self.controller.last_poll = time.monotonic() - 181
        self.assertEqual((await self.client.get("/api/v1/health")).status, 503)

    async def test_watchdog_requests_restart_without_replaying_commands(self):
        service = self.client.server.app[SERVICE]
        with patch.object(service, "healthy", return_value=False), \
                patch("sleepnumber_rf.server.asyncio.sleep", return_value=None), \
                patch("sleepnumber_rf.server.os.kill") as kill:
            await service.watchdog()
        kill.assert_called_once()
        self.assertTrue(self.controller.stop_event.is_set())
        self.assertFalse(any(c[0] in (0x11, 0x13) for c in self.controller.transport.calls))


if __name__ == "__main__":
    unittest.main()
