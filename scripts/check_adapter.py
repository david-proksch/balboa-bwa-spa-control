"""Exercise the real spa adapter against the tub, READ-ONLY.

Connects through :class:`hottub.spa.balboa.BalboaSpaAdapter` exactly as the
application will, prints what it reports, and disconnects. It sends only a
discovery broadcast, the connection handshake and one fault-log request; it
never changes a setting, a pump or a temperature.

Use it after changing the adapter, or when the application cannot see the
tub and you want to know whether the fault is in the network or the code.

Usage
-----
    uv run python scripts/check_adapter.py                  # discover
    uv run python scripts/check_adapter.py 192.168.1.50    # skip discovery
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from hottub.domain import HotTubError, SpaStateChanged, SystemClock
from hottub.spa.balboa import BalboaSettings, BalboaSpaAdapter

#: Tail of the Wi-Fi module's MAC address, which is also the end of the
#: network name it advertises (``BWGSpa_XXXXXX``). Taken from the environment
#: variable HOTTUB_SPA_MAC_SUFFIX; without it, the first spa that answers is used.
MAC_SUFFIX = os.environ.get("HOTTUB_SPA_MAC_SUFFIX", "").upper() or None

#: How long to listen for state changes, in seconds.
LISTEN_SECONDS = 10


async def main() -> int:
    """Connect, report, disconnect. Returns a process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    host = sys.argv[1] if len(sys.argv) > 1 else None
    adapter = BalboaSpaAdapter(SystemClock(), BalboaSettings(host=host, mac_suffix=MAC_SUFFIX))

    def on_change(event: SpaStateChanged) -> None:
        print(f"  change: {event.current}")

    try:
        await adapter.connect()
        print(f"Connected       : {adapter.connected}")
        print(f"State           : {adapter.state}")
        print(f"Last message at : {adapter.last_message_at}")
        print(f"Latest fault    : {await adapter.latest_fault()}")

        print(f"Listening for changes for {LISTEN_SECONDS} s...")
        unsubscribe = adapter.subscribe(on_change)
        await asyncio.sleep(LISTEN_SECONDS)
        unsubscribe()
        print(f"Last message at : {adapter.last_message_at}")
    except HotTubError as error:
        print(f"Failed: {error}")
        return 1
    finally:
        await adapter.disconnect()
    print("Done. Nothing on the spa was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
