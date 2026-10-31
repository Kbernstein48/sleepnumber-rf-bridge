# Sleep Number RF Bridge

Local control of a compatible Sleep Number 360 FlexFit adjustable foundation using an existing HUSBZB-1 USB radio. Includes a standalone web app, JSON API, Python RF driver, Docker deployment, and a reusable Home Assistant custom integration.

The bridge runs independently of Home Assistant. The Home Assistant component talks to its HTTP API, so Home Assistant can be updated, restarted or moved to another host without changing the radio driver. No pump, cloud account, remote handset, ESP32 or bed modification is required for the verified setup.

## Verified setup

| Item | Verified |
| --- | --- |
| Foundation | 360 FlexFit 1K, controller PN 124348 / item 208-0027, March 2023, FCC LPM-9000B |
| Controls | One head axis, actual position feedback, underbed outlet on/off |
| Radio | HUSBZB-1 Zigbee interface, EmberZNet 5.4.1.0 build 962, EZSP v4 |
| Serial | 57600 baud, hardware flow control |
| RF | Channels 15 and 20 observed; proprietary SMAC/MCR over IEEE 802.15.4 |
| Host | Linux ARM64 Raspberry Pi; Python 3.14, bellows 0.49.2 |
| Home Assistant | Container 2026.6.3; custom integration installed separately |

Head movement and readback have been physically observed, including 61% → 39% → 61% and subsequently 0%. Light commands have been confirmed by controller readback; visual illumination remains unconfirmed. This initial release supports the head and light features above. Other controller/radio firmware combinations need verification; sharing the same RF frequency does not establish compatibility.

The Zigbee interface is dedicated to this service while it runs. It cannot simultaneously run ZHA or Zigbee2MQTT. The separate HUSBZB-1 Z-Wave interface remains independent. The driver refuses to enter test mode if the radio reports an existing Zigbee network and does not flash radio firmware.

## Install the standalone bridge

1. Clone or extract this repository on the Linux host connected to the radio.
2. Create persistent configuration:

   ```sh
   mkdir -p data
   cp config.example.json data/config.json
   ```

3. Edit `data/config.json`. Replace `abcd` with the final four hexadecimal digits of the foundation's radio address on its exterior controller label. The full radio address is distinct from the serial number. Check it against a passive survey if uncertain. Set the initial `channel`, serial device and underbed outlets for the installation. The sample controls outlet 3; outlet 4 can be added after verification.
4. Start:

   ```sh
   docker compose up -d --build
   ```

5. Open `http://YOUR-PI:8768`. The bridge reads the actual position and light state. Startup and reconnect never restore motion or light commands.

The `data` directory persists settings and positional presets across power loss and application upgrades. The visual head-angle calibration and theme are stored in each browser. Favorite can be saved from the web app at the current reported head position. These are bridge positional presets; the base's original stored native presets are untouched.

The RF channel is not assumed to stay fixed. After three failed polls and stale position telemetry, the bridge searches channels 11–26 using plain status queries addressed to the configured base. Two replies verify the working channel, which is saved automatically. Searches run under the same lock as physical jobs, never interrupt active movement, and never repeat a failed movement or light command. If no channel answers, the previous channel is restored and another search waits at least 60 seconds.

For a stable host device path, set `RADIO_DEVICE=/dev/serial/by-id/YOUR-ADAPTER-if01-port0` in a local `.env` file. It maps to `/dev/ttyUSB1` inside the container. Check the adapter's interfaces before selecting a path: the verified HUSBZB-1 used `if01` for Zigbee and `if00` for Z-Wave.

### Python installation

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install .
sleepnumber-rf --config /absolute/path/to/data/config.json --port 8768
```

The Linux user needs serial-device permissions, usually membership in `dialout`. Run one bridge process per radio. Configuration changes to device, target or channel take effect after restart. Name and positional presets can be changed online through `PUT /api/v1/config` while idle.

### Passive discovery

With the service stopped, use the same Python environment:

```sh
python -m sleepnumber_rf.discover --device /dev/ttyUSB1 --channel 15 --seconds 30
```

Add `--all-channels --seconds 3` to sample channels 11–26. This lists valid-FCS SMAC candidates, addresses, RSSI and command counts. Match the address to the controller label; a nearby radio alone is not proof of identity. The survey sends no RF requests and restores test mode/channel afterward.

An empty passive survey does not prove that the base is offline. A base that is quiet during the sampling window can still reply to a directed status query. The running bridge's automatic channel recovery uses directed status reads for this reason.

## Home Assistant

1. Copy `custom_components/sleepnumber_rf` into the Home Assistant configuration directory, or run:

   ```sh
   python scripts/install-home-assistant.py /path/to/homeassistant/config
   ```

   The installer backs up an existing component before replacing it. Restart Home Assistant after installing/updating component files.
2. In **Settings → Devices & services → Add integration**, choose **Sleep Number RF** and enter `http://YOUR-PI:8768`.
3. The device exposes **Head position**, a native **0–100% slider** in 1% steps. Drag to the desired percentage and release to move; its reported value follows the actual bed position. **Underbed light**, **Flat**, **Stop**, saved positional preset buttons, and **RF signal** are also available. Favorite/Snore buttons become available after those presets are saved. Reload the integration after adding other named presets.

The slider uses a `number` entity; its exact ID depends on the device/area names and Home Assistant's naming settings. Its display mode is forced to Slider. For a dashboard tile, add the `numeric-input` feature with `style: slider`; adapt the example entity ID in [docs/bed-slider-card.yaml](docs/bed-slider-card.yaml). The older cover entity remains available for existing automations and manual cards, and is hidden from automatic dashboards. No helper, automation or additional custom frontend card is required. Component 0.1.1 also works with bridge 0.1.0.

The custom component directory is self-contained: the bridge Python package and radio dependencies do not need to be installed inside Home Assistant. Copy the same directory to a new Home Assistant installation and enter the existing bridge URL. Unique IDs follow the base address. The entry supports **Reconfigure** when the bridge URL changes.

An optional bootstrap configuration imports the entry on startup:

```yaml
sleepnumber_rf:
  host: http://YOUR-PI:8768
```

Once imported, the entry persists in Home Assistant and that YAML section can be removed. Keep a backup of the Home Assistant config directory when migrating to preserve areas, dashboards and automations. The repository includes the customary custom-component/HACS layout and metadata; publication is independent of listing in any integration store.

### Apple Home / HomeKit

Home Assistant's HomeKit Bridge can expose the existing cover entity as a percentage-adjustable window-covering accessory named **Bed**, alongside the underbed light. Explicitly include the cover entity in the HomeKit filter because it is hidden from automatic Home Assistant dashboards. The native HA number slider remains available separately. Pairing with Apple Home is completed using Home Assistant's HomeKit Pairing notification. Apple Home may retain an accessory name after pairing; its accessory settings let you rename the displayed label.

If Apple Home reports No Response, check `/api/v1/status` and the corresponding HA entities first. A paired HomeKit bridge can be reachable while the bed's RF channel is wrong. Version 0.1.2 adds automatic channel recovery; existing 0.1.x Home Assistant components remain compatible.

## API

The web app and integration use the same unauthenticated local API. It is intended for a trusted LAN. See [the API reference](docs/API.md) for jobs, errors and examples.

```sh
curl http://YOUR-PI:8768/api/v1/status
curl -X POST http://YOUR-PI:8768/api/v1/bed/position \
  -H 'Content-Type: application/json' -d '{"value":40}'
curl -X POST http://YOUR-PI:8768/api/v1/bed/stop \
  -H 'Content-Type: application/json' -d '{}'
```

Stop immediately withdraws the MicroAdjust keepalive, then checks plain status until the base reports idle. The observed base stops through its own motion watchdog when keepalive ceases; this is not an instantaneous hardware emergency stop. The earlier `ForceIdle` command was rejected during active movement and is deliberately excluded from this driver. An uncertain motion write is never automatically repeated. Three missed position replies stop keepalive and fail the job; one lost reply is tolerated.

## Development and releases

```sh
pip install .
python -m unittest discover -s tests -v
python scripts/package.py
```

The package script creates clean source and Home Assistant ZIPs with SHA-256 checksums. Tests cover framing/corruption, routing, head-only payloads, persistence, missed replies, channel recovery and backoff, Stop preemption, job cancellation, startup behavior, API validation and the actual Home Assistant HTTP client. CI runs Python tests and Home Assistant's manifest/translation validator. [Protocol notes](docs/PROTOCOL.md) distinguish measured behavior from remaining work.

## Credits and license

MIT licensed. Independent community project, unaffiliated with Sleep Number. The command-layer analysis in [newtro/SleepNumberApp](https://github.com/newtro/SleepNumberApp/blob/main/docs/PROTOCOL-FROM-APK.md) and [JonGilmore/sleepnumber-ble](https://github.com/JonGilmore/sleepnumber-ble) helped identify foundation status, MicroAdjust and outlet commands. This project supplies the direct raw-RF transport and does not bundle APKs, proprietary firmware, credentials or raw household captures.
