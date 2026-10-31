"""Standalone LAN web app and asynchronous job API."""

import argparse
import asyncio
from collections import deque
import contextlib
import logging
from pathlib import Path
import time

from aiohttp import web

from . import __version__
from . import config as configuration
from .controller import Controller, MotionCancelled
from .protocol import head_payload

MOTION = {"position", "preset"}
ACTIONS = MOTION | {"stop", "light", "refresh", "connect", "reconnect", "disconnect"}
SERVICE = web.AppKey("service", object)


class Service:
    def __init__(self, config, config_path=None, controller=None):
        self.config, self.config_path = config, config_path
        self.controller = controller or Controller(config)
        self.jobs = {}
        self.pending = deque()
        self.wake = asyncio.Event()
        self.sequence = int(time.time() * 1000)
        self.active = None
        self.worker = None

    def status(self):
        return {"device_id": "sleepnumber_rf_" + self.config["target"],
                "name": self.config["name"], "version": __version__,
                "bed": self.controller.snapshot(), "commands": [],
                "presets": self.config["presets"]}

    def persist(self):
        if self.config_path:
            configuration.save(self.config_path, self.config)

    def submit(self, action, data):
        if action not in ACTIONS:
            raise ValueError("Unknown bed action")
        allowed = {"position": {"value", "axis", "side"}, "preset": {"preset"}, "light": {"enabled"}}
        if set(data) - allowed.get(action, set()):
            raise ValueError("Unexpected command parameters")
        if action == "position":
            head_payload(data.get("value"))
            if data.get("axis", "head") != "head" or data.get("side", "right") != "right":
                raise ValueError("This driver controls one head axis")
        if action == "preset" and data.get("preset") not in self.config["presets"]:
            raise ValueError("Unknown positional preset")
        if action == "light" and type(data.get("enabled")) is not bool:
            raise ValueError("enabled must be true or false")
        if action != "stop" and len(self.pending) >= 8:
            raise web.HTTPConflict(text="Command queue full")
        if action in MOTION and (self.active and self.active["action"] in MOTION | {"stop"} or any(job["action"] in MOTION | {"stop"} for job in self.pending)):
            raise web.HTTPConflict(text="Wait for movement/Stop to finish before another position")
        if action == "stop":
            # Signal before the worker can wait on the active movement operation.
            self.controller.stop_event.set()
            for job in list(self.pending):
                if job["action"] in MOTION:
                    job.update(state="cancelled", message="Cancelled by Stop")
                    self.pending.remove(job)
        elif action in MOTION:
            self.controller.stop_event.clear()
        self.sequence += 1
        job = {"job_id": self.sequence, "action": action, "state": "queued", "message": "Queued", "parameters": data}
        self.jobs[self.sequence] = job
        if action == "stop":
            self.pending.appendleft(job)
        else:
            self.pending.append(job)
        self.wake.set()
        while len(self.jobs) > 128:
            removable = next((key for key, item in self.jobs.items() if item["state"] in ("succeeded", "failed", "cancelled")), None)
            if removable is None:
                break
            del self.jobs[removable]
        return job

    async def run_jobs(self):
        while True:
            await self.wake.wait()
            while self.pending:
                job = self.pending.popleft()
                self.active = job
                job.update(state="running", message="Running")
                try:
                    await self.controller.execute(job["action"], job["parameters"])
                    if job["action"] in ("connect", "reconnect", "disconnect"):
                        self.persist()
                    job.update(state="succeeded", message="Confirmed by base")
                except MotionCancelled as exc:
                    job.update(state="cancelled", message=str(exc))
                except Exception as exc:
                    job.update(state="failed", message=str(exc))
                finally:
                    self.active = None
            self.wake.clear()


@web.middleware
async def responses(request, handler):
    try:
        response = web.Response(status=204) if request.method == "OPTIONS" else await handler(request)
    except (ValueError, KeyError) as exc:
        response = web.json_response({"error": str(exc)}, status=400)
    except web.HTTPException as exc:
        response = web.json_response({"error": exc.text}, status=exc.status)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Cache-Control"] = "no-store"
    return response


async def body(request):
    try:
        data = await request.json()
    except (ValueError, TypeError) as exc:
        raise ValueError("Expected a JSON object") from exc
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


async def index(request):
    return web.FileResponse(Path(__file__).parent / "web" / "index.html")


async def status(request):
    return web.json_response(request.app[SERVICE].status())


async def bed_status(request):
    return web.json_response(request.app[SERVICE].controller.snapshot())


async def command(request):
    service = request.app[SERVICE]
    job = service.submit(request.match_info["action"], await body(request))
    return web.json_response({"job_id": job["job_id"], "state": job["state"]}, status=202)


async def job_status(request):
    service = request.app[SERVICE]
    try:
        job = service.jobs[int(request.match_info["job_id"])]
    except (ValueError, KeyError):
        return web.json_response({"state": "unknown", "message": "Job not found; the bridge may have restarted"}, status=404)
    return web.json_response({key: value for key, value in job.items() if key != "parameters"})


async def settings(request):
    service = request.app[SERVICE]
    if request.method == "PUT":
        update = await body(request)
        if set(update) - {"name", "presets"}:
            raise ValueError("Only name and presets can change online; edit the config file and restart for radio changes")
        if service.active or service.pending:
            raise web.HTTPConflict(text="Wait for commands to finish before changing settings")
        async with service.controller.operation_lock:
            updated = configuration.validate({**service.config, **update})
            if service.config_path:
                configuration.save(service.config_path, updated)
            service.config.clear()
            service.config.update(updated)
    return web.json_response(service.config)


async def lifecycle(app):
    service = app[SERVICE]
    service.controller.start()
    service.worker = asyncio.create_task(service.run_jobs())
    yield
    service.controller.stop_event.set()
    # Allow active position control to withdraw keepalive and verify idle first.
    if service.active:
        for _ in range(100):
            if not service.active:
                break
            await asyncio.sleep(0.1)
    service.worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await service.worker
    await service.controller.close()


def make_app(config, config_path=None, controller=None):
    app = web.Application(middlewares=[responses], client_max_size=16384)
    app[SERVICE] = Service(config, config_path, controller)
    app.router.add_get("/", index)
    app.router.add_get("/api/v1/status", status)
    app.router.add_get("/api/v1/bed/status", bed_status)
    app.router.add_post("/api/v1/bed/{action}", command)
    app.router.add_get("/api/v1/jobs/{job_id}", job_status)
    app.router.add_route("GET", "/api/v1/config", settings)
    app.router.add_route("PUT", "/api/v1/config", settings)
    app.cleanup_ctx.append(lifecycle)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/data/config.json")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = configuration.load(args.config)
    web.run_app(make_app(config, args.config), host=args.host, port=args.port, shutdown_timeout=15)


if __name__ == "__main__":
    main()
