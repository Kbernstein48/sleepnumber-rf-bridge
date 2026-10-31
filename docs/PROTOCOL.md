# Direct foundation RF findings

## Framing

The verified LPM-9000B talks proprietary SMAC over the IEEE 802.15.4 PHY, using channel 15. An HUSBZB-1 with EZSP v4 / EmberZNet 5.4.1 can receive and transmit these frames through `mfglibStart(rxCallback=True)`, `mfglibRxHandler` and `mfglibSendPacket`. It is not joined to a network. No adapter firmware was flashed.

Packet: `7e ff` + ten-byte MCR-shaped header + 0–15 payload bytes + IEEE 802.15.4 FCS (CRC-16/KERMIT, reflected polynomial 0x8408, initial zero, little endian).

| Header offset | Meaning |
| --- | --- |
| 0 | Node/type, request `42`, foundation reply `41` |
| 1–2 | Source, **0000** for coordinator requests |
| 3–4 | Destination, foundation's specific 16-bit address, big endian |
| 5 | Inner node/type, `42` requests / `41` replies |
| 6–7 | Repeated source, **0000** requests |
| 8 | Function; response function OR `80` |
| 9 | Qualifier in high nibble; payload length in low nibble |

The last four hex digits of the controller label's radio address matched the received RF source. Changing the request source to zero was the key breakthrough: requests using the USB adapter's identity received no replies. No bind or pairing-button procedure was needed with the verified foundation.

Bellows serializes the LVBytes length itself. Give `mfglibSendPacket` the complete SMAC/header/payload/FCS bytes; do not prepend an additional length byte. The old NCP regenerates the final two FCS bytes. Its receive callback retains a valid FCS. Newer NCP versions may differ and have not been validated.

## Implemented commands

| Function | Qualifier | Payload / result |
| --- | --- | --- |
| `12` | 0 | Plain status → `92`, 15 bytes |
| `12` | 2 | Head MicroAdjust keepalive/status, approximately every 333 ms |
| `25` | 0 | System configuration → `a5`, at least 7 bytes |
| `28` | 0 | Pinch/disconnection state → `a8`, 5 bytes |
| `11` | 0 | Head MicroAdjust: `[target,00,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff]` |
| `14` | outlet 3 or 4 | Outlet read → `94`, `[on,timer_low,timer_high]` |
| `13` | outlet 3 or 4 | Outlet set: `[on,timer_low,timer_high]` |

Write ACKs use an empty payload and qualifier zero, even when the request selected outlet 3. Qualifiers E/F indicate rejection. Responses are checked against source, destination, repeated source, node type, function and qualifier. There is no transaction ID, so a late or missing response cannot prove a write did not occur; writes are not automatically retried.

Status: byte 0 bit 0 moving, bit 5 needs homing, bit 6 configured. Bytes 1/2 are right/left head positions. Bytes 9/10 are head actuator status: bit 0 is normal MOVING; bits 1–7 are treated as faults. This release sends only the first head target and leaves all other MicroAdjust fields FF, following the verified head-only operation. Feet, firmness, native preset writes, intensity and binding/reset commands are outside the implemented feature set.

## Stop and remaining validation

An idle `02` ForceIdle ACK did not establish active-motion Stop: the base later rejected it with qualifier F during actual movement. The production driver therefore withdraws MicroAdjust keepalive and polls plain status until idle. An interrupted keepalive sequence was observed to stop the base mid-travel, with subsequent telemetry confirming idle. Software tests verify that a priority Stop emits no more keepalive requests after the in-flight transaction finishes. Exact physical Stop latency through the final web/HA path has not been measured.

Real head movement was confirmed across a large range and to zero. One target of 40% settled at 39%, so nonzero targets accept the base's small rounding tolerance; Flat requires exactly zero. Light outlet 3 on/off was verified by readback. Visual lighting, outlet 4, brightness adjustment and native hardware presets remain unverified. The controller's right/left intensity was initially 1/100, which can explain dim lighting on one side. These details are intentionally separate from application-level test results.

## Sources

- [APK command-layer analysis](https://github.com/newtro/SleepNumberApp/blob/main/docs/PROTOCOL-FROM-APK.md)
- [Community BLE command investigation](https://github.com/JonGilmore/sleepnumber-ble)
- [FCC internal photographs for LPM9000B](https://fccid.io/LPM9000B/Internal-Photos/Internal-Photos-3323980)
- [NXP MC13201](https://www.nxp.com/products/MC13201)
- [Home Assistant configuration flow documentation](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/)

The public command research describes a pump/BAM BLE transport. The direct source-zero SMAC exchange, FCS checks, hardware movement and readback observations above were established during this project. Raw captures and installation identities are excluded from the repository.
