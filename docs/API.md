# Local API v1

Base URL: `http://YOUR-PI:8768/api/v1`. JSON requests/responses; no authentication or API key. Browser CORS is allowed on the trusted LAN.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/status` | Bridge ID/version, bed status, positional presets |
| GET | `/bed/status` | Bed status only |
| POST | `/bed/position` | `{"value":40}`; integer 0–100, head only |
| POST | `/bed/preset` | `{"preset":"flat"}` or a saved positional preset |
| POST | `/bed/light` | `{"enabled":true}` / `false` |
| POST | `/bed/stop` | `{}`; priority Stop and cancellation of queued moves |
| POST | `/bed/refresh` | `{}`; refresh hardware readback |
| POST | `/bed/disconnect` | `{}`; withdraw keepalive, confirm idle, release radio, persist disabled |
| POST | `/bed/reconnect` | `{}`; enable radio and read actual state, persist enabled |
| GET | `/jobs/{job_id}` | Job outcome |
| GET | `/config` | Current configuration |
| PUT | `/config` | Change `name` and/or `presets` while idle |

Successful submissions return HTTP 202 with `{"job_id":123,"state":"queued"}`. Poll `/jobs/123` until `state` is `succeeded`, `failed` or `cancelled`; intermediate values are `queued` and `running`. On success, physical state was confirmed by the controller. Errors describe uncertainty honestly: an acknowledgement timeout can mean the physical command already took effect. Read status before submitting another command. Job history is in memory, bounded to 128 entries, and uses boot-specific time-based IDs. Restarted/expired jobs return HTTP 404 with `state: "unknown"`; they are never replayed.

HTTP 400 rejects malformed JSON, unsupported actions/axes or invalid values. HTTP 409 reports conflicting motion, a full queue or settings changes during an operation. Stop is accepted ahead of normal jobs and signals the current movement before the queue worker waits for its lock. It cancels pending position/preset jobs. A position request is rejected while another position/Stop is pending or running.

`positions.right_head` reports measured 0–100%. `connected` requires a live serial session and status less than 15 seconds old. Cached position can remain present when disconnected; use `connected`/`age_ms` to assess availability. `moving`, `homing`, `fault`, `busy`, and `error` describe the current controller state. `radio.rssi` is in dBm. Light `on` is read back from configured outlets; unknown state is `null`.

Light-on uses a 120-minute base timer. Light-off clears the timer. Brightness changes are not exposed in this release because the intensity command has not been physically verified. The driver does not restore light state after restart.

Save positional presets without moving:

```sh
curl -X PUT http://YOUR-PI:8768/api/v1/config \
  -H 'Content-Type: application/json' \
  -d '{"presets":{"flat":0,"favorite":40,"snore":15}}'
```

This replaces the saved positional preset map (Flat is always retained at 0). The values are installation preferences, not factory-calibrated angles or the base's native preset slots. The configuration file is atomically replaced and fsynced. It must be mounted outside the application directory for persistence across application replacement.
