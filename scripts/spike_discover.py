"""Phase 0 spike: discover the spa and report what its controller exposes.

This script is strictly READ-ONLY. It sends discovery broadcasts and
configuration/fault-log *requests*, and never changes a setting, a pump or a
temperature. (Do not run ``python -m pybalboa`` instead: that entry point
exercises every control on the tub.)

Purpose
-------
The build plan (PLAN.md, section 7, phase 0) rests on assumptions about the
Balboa controller that can only be confirmed against the real hardware:

* that the spa answers UDP discovery and can be matched by MAC suffix;
* how many pumps there are and which speeds each supports;
* the valid temperature bands for the low and high ranges;
* that status updates are pushed continuously, and how often;
* that the fault log is readable.

The findings are recorded by hand in ``docs/balboa-notes.md``.

Usage
-----
    uv run python scripts/spike_discover.py                # discover
    uv run python scripts/spike_discover.py 192.168.1.50   # skip discovery

Set HOTTUB_SPA_MAC_SUFFIX (e.g. ``ABCDEF``) to pick one spa among several.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from pybalboa import EVENT_UPDATE, SpaClient
from pybalboa.discovery import async_discover

#: Tail of the Wi-Fi module's MAC address, which is also the end of the
#: network name it advertises (``BWGSpa_XXXXXX``). Taken from the environment
#: variable HOTTUB_SPA_MAC_SUFFIX; without it, the first spa that answers is used.
MAC_SUFFIX = os.environ.get("HOTTUB_SPA_MAC_SUFFIX", "").upper() or None

#: How long to count pushed status updates for, in seconds.
LISTEN_SECONDS = 10


def _normalise_mac(mac: str) -> str:
    """Return ``mac`` upper-cased with separators removed, for suffix matching."""
    return mac.upper().replace(":", "").replace("-", "")


async def find_spa_address() -> str | None:
    """Broadcast for spas and return the address of the one matching MAC_SUFFIX.

    With no suffix set, the first spa that answers is taken. Returns
    ``None`` when no matching spa answers within the timeout.
    """
    print("Discovering spas (UDP broadcast, port 30303, up to 10 s)...")
    found = await async_discover(timeout=10)
    if not found:
        print("  No spa answered the discovery broadcast.")
        return None
    match: str | None = None
    for spa in found:
        if MAC_SUFFIX is None:
            is_ours = match is None
        else:
            is_ours = _normalise_mac(spa.mac_address).endswith(MAC_SUFFIX)
        marker = "  <-- this one" if is_ours else ""
        print(f"  {spa.address}:{spa.port}  mac={spa.mac_address}  host={spa.hostname}{marker}")
        if is_ours and match is None:
            match = spa.address
    if match is None:
        print(f"  None of the spas found has a MAC ending in {MAC_SUFFIX}.")
    return match


async def report(host: str) -> int:
    """Connect to the spa at ``host``, print everything it exposes, disconnect.

    Returns a process exit code: 0 on success, 1 if the spa could not be read.
    """
    print(f"\nConnecting to {host}...")
    async with SpaClient(host) as spa:
        if not await spa.async_configuration_loaded(timeout=20):
            print(f"  Configuration did not load (spa state: {spa.state.name}).")
            return 1

        print("\nIdentification")
        print(f"  MAC address        : {spa.mac_address}")
        print(f"  Model              : {spa.model}")
        print(f"  Software version   : {spa.software_version}")
        print(f"  Config signature   : {spa.configuration_signature}")
        print(f"  Voltage            : {spa.voltage}")
        print(f"  Heater type        : {spa.heater_type}")

        print("\nEquipment")
        print(f"  Pump count         : {spa.pump_count}")
        for pump in spa.pumps:
            speeds = [option.name for option in pump.options]
            print(f"  {pump.name:<19}: speeds {speeds}, now {pump.state.name}")
        print(f"  Circulation pump   : {spa.circulation_pump}")
        print(f"  Blowers            : {[c.name for c in spa.blowers]}")
        print(f"  Lights             : {[c.name for c in spa.lights]}")
        print(f"  Aux / misters      : {[c.name for c in spa.aux + spa.misters]}")

        print("\nTemperature")
        print(f"  Unit               : {spa.temperature_unit.name}")
        print(f"  Water temperature  : {spa.temperature}")
        print(f"  Set temperature    : {spa.target_temperature}")
        print(f"  Active range       : {spa.temperature_range.state.name}")
        print(f"  Active range limits: {spa.temperature_minimum}..{spa.temperature_maximum}")
        # No public accessor exists for the limits of the *inactive* range, so
        # the private attributes are read here. Spike only; the application
        # must not depend on them.
        print(f"  Low range limits   : {spa._low_range}")  # noqa: SLF001
        print(f"  High range limits  : {spa._high_range}")  # noqa: SLF001

        print("\nHeating")
        print(f"  Heat mode          : {spa.heat_mode.state.name}")
        print(f"  Settable modes     : {[o.name for o in spa.heat_mode.options]}")
        print(f"  Heat state         : {spa.heat_state.name}")
        print(f"  Spa state          : {spa.state.name}")

        print("\nFilter cycles")
        print(
            f"  Cycle 1            : starts {spa.filter_cycle_1_start}, "
            f"runs {spa.filter_cycle_1_duration}, running={spa.filter_cycle_1_running}"
        )
        print(
            f"  Cycle 2            : starts {spa.filter_cycle_2_start}, "
            f"runs {spa.filter_cycle_2_duration}, enabled={spa.filter_cycle_2_enabled}, "
            f"running={spa.filter_cycle_2_running}"
        )

        print("\nClock")
        print(
            f"  Spa time           : {spa.time_hour:02d}:{spa.time_minute:02d} "
            f"({'24h' if spa.is_24_hour else '12h'})"
        )
        print(f"  This computer      : {time.strftime('%H:%M')}")

        print("\nFault log")
        await spa.request_fault_log()
        await asyncio.sleep(2)  # the reply arrives asynchronously
        print(f"  Most recent entry  : {spa.fault if spa.fault else 'none returned'}")

        print(f"\nCounting pushed status updates for {LISTEN_SECONDS} s...")
        updates = 0

        def _count() -> None:
            nonlocal updates
            updates += 1

        unsubscribe = spa.on(EVENT_UPDATE, _count)
        await asyncio.sleep(LISTEN_SECONDS)
        unsubscribe()
        print(f"  State-change events: {updates}")
        print(f"  Last message at    : {spa.last_message_received}")
        print(f"  Still connected    : {spa.connected}")

    print("\nDone. Nothing on the spa was changed.")
    return 0


async def main() -> int:
    """Entry point: resolve the spa's address, then print the report."""
    host = sys.argv[1] if len(sys.argv) > 1 else await find_spa_address()
    if host is None:
        return 1
    return await report(host)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
