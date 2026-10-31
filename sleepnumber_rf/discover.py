"""Passive SMAC candidate survey; no motion, pairing or write packets."""

import argparse
import asyncio
from collections import Counter
import json

from .config import validate
from .protocol import decode
from .transport import Transport


class Survey(Transport):
    def __init__(self, config):
        super().__init__(config)
        self.candidates = {}

    def received(self, name, values):
        if name != "mfglibRxHandler":
            return
        _, rssi, raw = values
        frame = decode(bytes(raw))
        if not frame or frame.source in (0, 65535) or frame.origin != frame.source:
            return
        key = (self.config["channel"], frame.source)
        row = self.candidates.setdefault(key, {"channel": key[0], "address": f"{key[1]:04x}", "packets": 0, "rssi": -128, "commands": Counter()})
        row["packets"] += 1
        row["rssi"] = max(row["rssi"], int(rssi))
        row["commands"][f"{frame.command:02x}"] += 1


async def run(args):
    radio = Survey(validate({"target": "abcd", "device": args.device, "channel": args.channel,
                             "lock_file": args.lock_file}))
    try:
        await radio.open()
        channels = range(11, 27) if args.all_channels else [args.channel]
        for channel in channels:
            radio.config["channel"] = channel
            (result,) = await radio.radio.mfglibSetChannel(channel=channel)
            radio.check(result, "Survey channel")
            await asyncio.sleep(args.seconds)
        print(json.dumps(sorted(radio.candidates.values(), key=lambda row: -row["packets"]), indent=2))
    finally:
        await radio.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/ttyUSB1")
    parser.add_argument("--channel", type=int, choices=range(11, 27), default=15)
    parser.add_argument("--seconds", type=int, choices=range(1, 121), default=20, help="Seconds per channel")
    parser.add_argument("--all-channels", action="store_true")
    parser.add_argument("--lock-file", default="/tmp/nightstand-husbzb-capture.lock")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
