# Balboa controller notes

What we have learned about this specific spa and the `pybalboa` library.
Update this file whenever the tub teaches us something new; the code comments
refer back to it.

## Phase 0 findings (2026-11-11)

Captured with `scripts/spike_discover.py` (read-only) using `pybalboa` 1.1.4.

### Identity and network

| Item | Value |
|------|-------|
| Wi-Fi module MAC | `00:15:27:AB:CD:EF` (placeholder; `00:15:27` is Balboa's prefix, and the last six characters match the module's network name `BWGSpa_ABCDEF`) |
| Address on the day | `192.168.1.50` (placeholder; given by DHCP, so reserve it on the router) |
| Discovery | Answers a UDP broadcast of `Discovery` on port 30303 within a second. Reply hostname is `BWGSPA`. |
| Control port | TCP 4257 |
| Controller | Balboa **BP7**, software `M100_221 V57.0`, config signature `ff69032a` |
| Heater | 240 V, type "standard" |

### Equipment

| Device | Options | Notes |
|--------|---------|-------|
| Pump 1 | `OFF`, `LOW`, `HIGH` | Two-speed. Was on `LOW` while heating: there is no separate circulation pump, so the spa runs pump 1 low itself to heat and filter. |
| Pump 2 | `OFF`, `ON` | Single-speed. |
| Circulation pump | none | |
| Blower 1, Light 1 | present | Not used by this project. |

### Temperature

- Unit: Fahrenheit.
- The spa has **two temperature ranges**, each with its own remembered set temperature:
  - Low range: 50–99 °F
  - High range: 80–104 °F
- On the day: low range active, set temperature 99 °F, water 99 °F.
- `SpaClient.set_temperature()` validates against the **active** range only and raises `ValueError` outside it. To set 102 °F while in the low range, the range must be switched first.
- Switching range is a toggle message; the spa then adopts that range's own remembered set temperature until a new one is sent.

### Heat mode

- Settable: `READY`, `REST`. `READY_IN_REST` is reported but cannot be set (the library rejects it). The project displays it as Rest.
- Heat state values: `OFF`, `HEATING`, `HEAT_WAITING`.

### Filter cycles

- Cycle 1: 09:00 for 30 minutes. Cycle 2: 18:00 for 30 minutes, enabled.
- Pump 1 runs on low during these regardless of our commands, so pump runtime includes them.

### Status updates

- The spa sends status messages continuously; `SpaClient.last_message_received` advances with every one.
- The library's `EVENT_UPDATE` event fires **only when something changed** (one event in ten seconds on a steady tub).
- Consequence: detect a silent connection from `last_message_received`, not from the absence of events.

### Faults

- The fault log is readable: 20 entries are kept; the latest was "Priming Mode" on 2026-11-03 12:00.
- It is a **history log, not a live flag**, and is only sent when requested (`request_fault_log()`).
- Consequence: the watchdog requests the latest entry every few minutes and alerts when a new one appears.

### Clock

- The spa's own clock was about one minute behind the computer. The project schedules from the Pi's clock and never from the spa's, so this does not matter; the spa clock only governs its filter cycles.

## Phase 2 findings (2026-11-11)

Found while writing `src/hottub/spa/balboa.py`, by reading the library source.

- **`send_message()` does nothing when disconnected** and swallows write
  errors (it only logs them). A command sent on a dead connection looks like
  a success. The adapter checks the connection before every command and
  waits for the status to confirm it.
- **Pumps, heat mode and temperature range are toggles.** The library works
  out how many "next state" messages to send from the state it last saw
  (off to high on pump 1 is two toggles). Two commands worked out from the
  same stale state would overshoot, so the adapter sends one command at a
  time and waits for the status to catch up in between.
- **Event listeners are shared by every object.** `EventMixin._listeners` is
  a class attribute, so a callback registered with `client.on(...)` is also
  called for every control (each pump, the heat mode...) and for every other
  `SpaClient` in the process. Two consequences:
  - Controls fire in the middle of parsing a status message, when later
    fields still hold old values. The adapter defers its work to the next
    event-loop turn so it only ever sees complete messages.
  - A callback outlives the client it was registered on. The adapter always
    unsubscribes before discarding a client.
- **An exception in a callback would stop the library's reader task**
  (`_process_message` is called outside its `try`). The adapter's callback
  cannot raise.
- **The library reconnects by itself, but only to the address it was created
  with.** The adapter does not rely on it: when the connection is lost it
  discards the client and builds a new one, trying the last address first
  and then discovery, so a new DHCP address is followed.
- **`connected` only means the socket is open.** If the Wi-Fi module loses
  power the socket can stay open and silent. The adapter treats 60 seconds
  without any message as a lost connection.
- **Fault-log replies do not raise an update event** and carry a naive
  timestamp on the spa's clock (local time). The adapter polls for the reply
  and converts the time to UTC.
- Verified read-only against the tub with `scripts/check_adapter.py`:
  discovery, state translation and the fault-log request all work, and the
  low range's limits (the active range that day) match the table above.

### Commands on the real tub (2026-11-11)

Measured with `scripts/try_commands.py`; every setting was put back.

| Command | Result | Time to confirm |
|---|---|---|
| Set temperature 99 to 98 and back | Taken up | 0.2 to 0.3 s |
| Pump 1 low to high | Taken up | 0.2 to 0.4 s |
| Pump 2 on, off | Taken up | 0.1 to 0.3 s |
| Heat mode Ready to Rest and back | Taken up | 0.2 to 0.3 s |
| Pump 1 off while the spa wants it running | Overruled: settles on low | never (5 s wait) |

- **Raising the set temperature starts pump 1 on low** without being asked,
  even by one degree with the water already at the new value. The spa is
  circulating water to measure it. The pump stopped by itself after between
  one and a half and four and a half minutes (two observations).
- **While the spa wants pump 1 running, "off" is overruled.** From high, the
  toggle lands on low and stays there. From low, the adapter's two toggles
  (low to high to off) run the pump on high for a moment before it settles
  back on low. The adapter logs this and does not treat it as an error.
- Pump 2's raw state value is 2 when on; the library reports it as on, and
  one toggle switches it off.

The same actions were repeated through the JSON API (phase 3) with the
same results and timings: ceiling 99 to 98 and back, pump 2 on and off,
heat mode Rest and back, pump 1 high, and pump 1 off overruled to low
because the set temperature had just been raised.

### Stalls of the Wi-Fi module

The module periodically stops talking. A read-only probe over 110 seconds
(one fault-log request every 2 seconds) showed:

- status messages at about 3.3 a second for some 50 seconds, then **complete
  silence for 18 to 20 seconds**, then messages again, repeating;
- the TCP connection stays open during the silence and pings are answered
  normally (about 20 ms, no loss), so it is the module, not the Wi-Fi;
- requests sent during the silence are answered late, in a burst, or not at
  all (36 replies to 45 requests);
- once, a stall ended with the module closing the connection, and three
  toggles sent during it were lost;
- a connection opened during a stall does not receive its configuration
  until the stall ends.

A later 50-second run saw no stall at all, so the pattern is not constant.
The cause is not known. Candidates: the module contacting Balboa's cloud
service, or another client (the phone app) holding a connection.

What the adapter does about it (`BalboaSpaAdapter._wait_until` and
`_client_with_live_link`):

- a command is held back until the spa has been heard from in the last
  2 seconds, because a toggle that takes effect 20 seconds late may no
  longer be wanted;
- the wait for confirmation counts only time during which the spa is
  talking, so a stall after sending is waited out;
- if the spa stays silent for 45 seconds the command fails with
  `SpaUnavailableError`;
- the wait for the configuration at connect time is 45 seconds;
- the connection is rebuilt only after 60 seconds of silence, so an ordinary
  stall does not cause a reconnection.

Consequence for the watchdog: "stale data" must not fire on an ordinary
stall. The threshold is 90 seconds (`PLAN.md`, section 4).

### Not yet verified on the real tub

- Switching between the low and high temperature ranges. The adapter can
  switch only as a side effect of a set temperature, and the test would have
  left the tub in a different range.
- Asking for Rest while the spa is in Ready-in-Rest (the adapter sends
  nothing).

## Library cautions

- **Never run `python -m pybalboa` against the tub casually.** That entry point changes the set temperature and toggles every control to test them.
- The limits of the inactive temperature range have no public accessor (`_low_range`, `_high_range` are private). The adapter keeps its own copy of the limits recorded above (`_RANGE_LIMITS_F`) and checks the active range's public limits against it at connect time, logging a warning if they differ.
- `SpaControl.set_state()` returns `True` when the message was sent, not when the spa obeyed. The adapter must confirm by watching the next status update.

### A momentarily missing water temperature (2026-11-11)

In Ready mode, holding 99 °F with the heater off, the once-a-minute record
shows one sample with no water temperature, taken three seconds after the
set temperature was raised from 85 to 99 °F; the samples a minute before and
after read 99 and 98 °F. A scheduler pass made in the same few seconds also
acted as if there were no reading. So the spa (or the library) reports "no
reading" for some seconds at a time even in Ready mode, at least around a
change of set temperature and the pump start that follows it.

Consequences in the code:

- The scheduler does not take a missing reading for a cold tub. It uses the
  last reading recorded within the hour (`SchedulerService`, `recent_water`).
- The watchdog already treats a missing reading as unknown, not as bad.
- The dashboard shows a dash for those seconds.

How often this happens outside a change of set temperature is not known
yet: one-a-minute samples would miss most gaps of a few seconds. Worth a
look during the soak.
