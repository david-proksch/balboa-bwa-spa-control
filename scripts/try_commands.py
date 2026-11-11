"""Send each command to the real tub once, then put everything back.

!! THIS SCRIPT CHANGES THE TUB. Unlike the other scripts here it lowers the
set temperature by one degree, runs both pumps for a few seconds and switches
the heat mode to Rest, restoring each before moving on. Run it only with the
owner's agreement and preferably within sight or earshot of the tub. It
refuses to run without ``--yes``.

Purpose
-------
The adapter's tests use a stand-in for the Balboa library. This is the
acceptance check against the real controller: does each command arrive, does
the spa confirm it, and how long does that take? Record what it shows in
``docs/balboa-notes.md``.

It does not exercise the switch between the low and high temperature ranges,
because the adapter offers no way to switch back and the tub would be left in
a different range from the one it started in.

Usage
-----
    uv run python scripts/try_commands.py --yes
    uv run python scripts/try_commands.py --yes 192.168.1.50
    uv run python scripts/try_commands.py --yes --debug     # show every message
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable
from datetime import UTC, datetime

from hottub.domain import (
    HeatMode,
    HotTubError,
    PumpSpeed,
    SpaState,
    SpaStateChanged,
    SystemClock,
    Temperature,
)
from hottub.spa.balboa import BalboaSettings, BalboaSpaAdapter

#: Tail of the Wi-Fi module's MAC address, which is also the end of the
#: network name it advertises (``BWGSpa_XXXXXX``). Taken from the environment
#: variable HOTTUB_SPA_MAC_SUFFIX; without it, the first spa that answers is used.
MAC_SUFFIX = os.environ.get("HOTTUB_SPA_MAC_SUFFIX", "").upper() or None

#: Pause after each command, in seconds, to see what the spa does next.
SETTLE_SECONDS = 4


def _silence(adapter: BalboaSpaAdapter) -> str:
    """How long ago the spa was last heard from, for the console."""
    heard = adapter.last_message_at
    if heard is None:
        return "never heard from"
    return f"last heard {(datetime.now(UTC) - heard).total_seconds():.1f} s ago"


def _summary(state: SpaState | None) -> str:
    """One line describing a state, for the console."""
    if state is None:
        return "no state"
    return (
        f"water={state.water_temperature} set={state.set_temperature} "
        f"mode={state.heat_mode.value} heater={state.heater.value} "
        f"pump1={state.pump1.value} pump2={'on' if state.pump2_on else 'off'}"
    )


async def _step(adapter: BalboaSpaAdapter, label: str, command: Awaitable[None]) -> bool:
    """Run one command, report how it went, and let the spa settle."""
    print(f"\n> {label}")
    started = time.monotonic()
    try:
        await command
    except HotTubError as error:
        print(f"  FAILED after {time.monotonic() - started:.1f} s: {error} ({_silence(adapter)})")
        return False
    print(f"  returned after {time.monotonic() - started:.1f} s: {_summary(adapter.state)}")
    await asyncio.sleep(SETTLE_SECONDS)
    print(f"  {SETTLE_SECONDS} s later: {_summary(adapter.state)} ({_silence(adapter)})")
    return True


async def main() -> int:
    """Run the sequence. Returns a process exit code."""
    arguments = [argument for argument in sys.argv[1:] if not argument.startswith("--")]
    debug = "--debug" in sys.argv[1:]
    if "--yes" not in sys.argv[1:]:
        print(__doc__)
        print("Not run: pass --yes to confirm that the tub may be changed.")
        return 2
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="  %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = arguments[0] if arguments else None
    adapter = BalboaSpaAdapter(SystemClock(), BalboaSettings(host=host, mac_suffix=MAC_SUFFIX))

    def on_change(event: SpaStateChanged) -> None:
        print(f"  [change] {_summary(event.current)}")

    try:
        await adapter.connect()
    except HotTubError as error:
        print(f"Could not connect: {error}")
        await adapter.disconnect()
        return 1

    original = adapter.state
    if original is None:
        print("The spa reported no state; nothing was changed.")
        await adapter.disconnect()
        return 1
    print(f"Before: {_summary(original)}")
    adapter.subscribe(on_change)
    one_lower = Temperature(original.set_temperature.fahrenheit - 1)
    other_mode = HeatMode.REST if original.heat_mode is HeatMode.READY else HeatMode.READY

    results: list[bool] = []
    try:
        results += [
            await _step(
                adapter, f"set temperature {one_lower}", adapter.set_temperature(one_lower)
            ),
            await _step(
                adapter,
                f"set temperature back to {original.set_temperature}",
                adapter.set_temperature(original.set_temperature),
            ),
            await _step(adapter, "pump 1 low", adapter.set_pump1(PumpSpeed.LOW)),
            await _step(adapter, "pump 1 high", adapter.set_pump1(PumpSpeed.HIGH)),
            await _step(adapter, "pump 1 off", adapter.set_pump1(PumpSpeed.OFF)),
            await _step(adapter, "pump 2 on", adapter.set_pump2(True)),
            await _step(adapter, "pump 2 off", adapter.set_pump2(False)),
            await _step(
                adapter, f"heat mode {other_mode.value}", adapter.set_heat_mode(other_mode)
            ),
            await _step(
                adapter,
                f"heat mode back to {original.heat_mode.value}",
                adapter.set_heat_mode(original.heat_mode),
            ),
        ]
    finally:
        # Whatever happened above, try to leave the tub as it was found.
        print("\nRestoring the original settings...")
        for label, restore in (
            ("set temperature", adapter.set_temperature(original.set_temperature)),
            ("heat mode", adapter.set_heat_mode(original.heat_mode)),
            ("pump 1", adapter.set_pump1(original.pump1)),
            ("pump 2", adapter.set_pump2(original.pump2_on)),
        ):
            try:
                await restore
            except HotTubError as error:
                print(f"  COULD NOT RESTORE {label}: {error}")
        print(f"Before: {_summary(original)}")
        print(f"After : {_summary(adapter.state)}")
        await adapter.disconnect()

    print(f"\n{sum(results)} of {len(results)} commands confirmed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
