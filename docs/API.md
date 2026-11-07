# Local API v1

Base URL: `http://YOUR-PI:8768/api/v1`. JSON requests/responses; no authentication or API key. Browser CORS is allowed on the trusted LAN.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/status` | Bridge ID/version, bed status, positional presets |
| GET | `/health` | HTTP 200 when background workers progress; 503 if stuck. Includes separate `bed_connected` flag |
| GET | `/bed/status` | Bed status only |
| POST | `/bed/position` | `{"value":40}`; integer 0–100, head only |
| POST | `/bed/preset` | `{"preset":"flat"}` or a saved positional preset |
| POST | `/bed/light` | `{"enabled":true}` / `false` |
| POST | `/bed/stop` | `{}`; priority Stop and cancellation of queued moves |
| POST | `/bed/refresh` | `{}`; refresh hardware readback |
| POST | `/bed/disconnect` | `{}`; withdraw keepalive, confirm idle, release radio, persist disabled |
| POST | `/bed/reconnect` | `{}`; reopen the radio session, search channels if needed, read actual state and persist enabled |
| GET | `/jobs/{job_id}` | Job outcome |
| GET | `/config` | Current configuration |
| PUT | `/config` | Change `name` and/or `presets` while idle |

Successful submissions return HTTP 202 with `{"job_id":123,"state":"queued"}`. Poll `/jobs/123` until `state` is `succeeded`, `failed` or `cancelled`; intermediate values are `queued` and `running`. On success, physical state was confirmed by the controller. Errors describe uncertainty honestly: an acknowledgement timeout can mean the physical command already took effect. Read status before submitting another command. Job history is in memory, bounded to 128 entries, and uses boot-specific time-based IDs. Restarted/expired jobs return HTTP 404 with `state: "unknown"`; they are never replayed.

HTTP 400 rejects malformed JSON, unsupported actions/axes or invalid values. HTTP 409 reports conflicting motion, a full queue or settings changes during an operation. Stop is accepted ahead of normal jobs and signals the current movement before the queue worker waits for its lock. It cancels pending position/preset jobs. A position request is rejected while another position/Stop is pending or running.

`positions.right_head` reports measured 0–100%. `connected` requires a live serial
session, no failed primary status read, and a non-expired cache (2.5 times the idle
poll interval, minimum 15 seconds; 15 seconds while moving). `age_ms` reports the
actual age; cached positions can remain present while disconnected. `moving`,
`homing`, `fault`, `busy`, and `error` describe the controller state. `radio.rssi`
is in dBm. Light `on` is confirmed from the configured outlets; a failed light
read reports unavailable/`null` without taking the position control offline.

Since 0.1.4, default background polling is position every 60 seconds, hardware
configuration once, and light every 300 seconds. Explicit commands refresh their
own state without waiting for the background interval. Active movement retains
its required keepalive. `polling.idle_seconds`, `light_seconds`, and
`next_check_seconds` describe the scheduler. GET status/health and job reads use
cached memory and do not send RF packets.

`radio.traffic` contains process-lifetime `request_attempts`, `transmitted`,
`replies`, `by_function`, `timeouts`, `uptime_seconds`, and `last_transmit_age_ms`.
Function keys include the qualifier: `0x12/0` is a plain status query; `0x12/2` is
an active-motion keepalive. Attempts include uncertain sends; `transmitted` counts
requests the USB radio accepted. A successful transmit does not prove delivery.

`discovering` indicates RF-channel recovery; `busy` is true during a search.
After three consecutive primary-status failures, one bounded addressed search
checks channels 11–26 and verifies two replies before saving a new channel. All
scans share the physical-operation lock. Unanswered normal queries back off to
ten minutes; full channel searches back off from ten to thirty minutes. The
radio is reinitialized at most once per uninterrupted outage, with no immediate
duplicate scan. `radio.restarts`, `recoveries`, and `failed_searches` expose
process-local counters. Valid status resets the outage backoff. Manual Reconnect
remains available, and no prior physical job is replayed after recovery.

Radio calls and poll cycles have deadlines. A process watchdog requests a graceful
exit if background workers stop progressing; Docker's `unless-stopped` policy
restarts it. The Docker health check uses `/health`, which distinguishes a broken
bridge process from an unplugged/unresponsive base. A base controller that remains
silent after all searches may require its own power cycle; bridge restarts cannot
remove power from the base.

Light-on uses a 120-minute base timer. Light-off clears the timer. Brightness changes are not exposed in this release because the intensity command has not been physically verified. The driver does not restore light state after restart.

Save positional presets without moving:

```sh
curl -X PUT http://YOUR-PI:8768/api/v1/config \
  -H 'Content-Type: application/json' \
  -d '{"presets":{"flat":0,"favorite":40,"snore":15}}'
```

This replaces the saved positional preset map (Flat is always retained at 0). The values are installation preferences, not factory-calibrated angles or the base's native preset slots. The configuration file is atomically replaced and fsynced. It must be mounted outside the application directory for persistence across application replacement.
