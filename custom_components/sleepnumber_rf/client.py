"""HTTP client without Home Assistant imports."""

import asyncio
from urllib.parse import urlsplit


class BridgeError(Exception):
    """Communication or command failure."""


def normalize_host(value):
    value = value.strip()
    if "://" not in value:
        value = "http://" + value
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Enter the bridge URL, including port")
    return value.rstrip("/")


class BridgeClient:
    def __init__(self, session, host):
        self.session, self.host = session, normalize_host(host)

    async def request(self, method, path, data=None):
        try:
            async with self.session.request(method, self.host + "/api/v1/" + path,
                                            json=data, timeout=12, allow_redirects=False) as response:
                result = await response.json()
                if not isinstance(result, dict):
                    raise BridgeError("Unexpected bridge response")
                if response.status >= 300:
                    raise BridgeError(result.get("error", result.get("message", "Request failed")))
                return result
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError("Cannot reach the RF bridge") from exc

    async def status(self):
        result = await self.request("GET", "status")
        if not isinstance(result.get("bed"), dict) or not str(result.get("device_id", "")).startswith("sleepnumber_rf_") or "version" not in result:
            raise BridgeError("This is not a Sleep Number RF bridge")
        return result

    async def command(self, action, **parameters):
        submitted = await self.request("POST", "bed/" + action, parameters)
        job_id = submitted.get("job_id")
        if type(job_id) is not int:
            raise BridgeError("Bridge did not return a job ID")
        deadline = asyncio.get_running_loop().time() + 110
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
            job = await self.request("GET", "jobs/" + str(job_id))
            if job.get("state") == "succeeded":
                return job
            if job.get("state") in ("failed", "cancelled", "unknown"):
                raise BridgeError(job.get("message", "Command failed"))
        raise BridgeError("Command outcome unknown; inspect bridge status before repeating")
