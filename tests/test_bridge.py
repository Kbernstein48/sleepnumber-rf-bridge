import asyncio
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

from sleepnumber_rf.config import validate, save, load
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
                    {"light_outlets": [3, 3]}, {"presets": {"bad name": 40}}, {"password": "unused"}):
            with self.assertRaises(ValueError):
                config(**bad)


class FakeTransport:
    def __init__(self, configuration):
        self.config = configuration
        self.ready = True
        self.rssi = -45
        self.calls = []
        self.keep = []
        self.plain = []
        self.current = status(40)

    async def open(self):
        self.ready = True

    async def close(self):
        self.ready = False

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
        controller.updated = time.monotonic() - 16
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


class RadioTests(unittest.IsolatedAsyncioTestCase):
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
            self.assertEqual((await client.status())["version"], "0.1.0")
            job = await client.command("refresh")
            self.assertEqual(job["state"], "succeeded")
            self.assertFalse(any(c[0] == 0x11 for c in self.controller.transport.calls))

    async def test_saved_preset_no_motion(self):
        response = await self.client.put("/api/v1/config", json={"presets": {"favorite": 37}})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["presets"], {"favorite": 37, "flat": 0})
        self.assertFalse(any(c[0] == 0x11 for c in self.controller.transport.calls))


if __name__ == "__main__":
    unittest.main()
