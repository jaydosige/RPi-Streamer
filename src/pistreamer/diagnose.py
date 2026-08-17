"""Work out *why* frames are being lost.

Dropped frames have two completely different causes that look identical from
the outside:

  * frames never arrive — the network or the sender is at fault
  * frames arrive but cannot be processed in time — the Pi is at fault

One frame counter cannot distinguish them. Two can. The runner measures at the
queue's input (what the NDI receiver handed us) and at the video sink's input
(what actually reached the display), so:

    arrival_fps  <  declared_fps   →  upstream: network or sender
    render_fps   <  arrival_fps    →  downstream: this Pi

Everything else here is corroboration: CPU, throttling, Wi-Fi signal,
interface errors. The verdict is deliberately conservative — it says
"inconclusive" rather than guessing, because sending someone to re-cable a
venue on a hunch is worse than saying nothing.

Pure functions over plain dicts, so this is testable without hardware.
"""

from __future__ import annotations

from typing import Any, Dict, List

# A feed within this fraction of its declared rate is considered fine.
HEALTHY_RATIO = 0.95
# Below this, something is definitely wrong rather than merely jittery.
CLEAR_DEFICIT_RATIO = 0.90

CPU_BUSY = 85.0
CPU_SATURATED = 95.0
TEMP_WARN = 80.0
WIFI_WEAK_DBM = -70.0
WIFI_MARGINAL_DBM = -60.0

VERDICT_IDLE = "idle"
VERDICT_HEALTHY = "healthy"
VERDICT_NETWORK = "network"
VERDICT_PI = "pi"
VERDICT_POWER = "power"
VERDICT_UNKNOWN = "inconclusive"


def _ratio(actual: Any, expected: Any) -> Any:
    try:
        if not expected or expected <= 0 or actual is None:
            return None
        return actual / expected
    except (TypeError, ZeroDivisionError):
        return None


def diagnose(
    stream: Dict[str, Any],
    system: Dict[str, Any],
    player: Dict[str, Any],
) -> Dict[str, Any]:
    evidence: List[Dict[str, str]] = []
    suggestions: List[str] = []

    def note(level: str, text: str) -> None:
        evidence.append({"level": level, "text": text})

    if not player or player.get("mode") != "ndi":
        return {
            "verdict": VERDICT_IDLE,
            "headline": "No NDI stream is playing.",
            "detail": "Start a stream to measure it.",
            "evidence": [],
            "suggestions": [],
        }

    if not stream or stream.get("arrival_fps") is None:
        return {
            "verdict": VERDICT_UNKNOWN,
            "headline": "Waiting for measurements.",
            "detail": "Give the stream a few seconds to report.",
            "evidence": [],
            "suggestions": [],
        }

    declared = stream.get("declared_fps")
    arrival = stream.get("arrival_fps")
    render = stream.get("fps")
    dropped = stream.get("dropped") or 0
    overruns = stream.get("queue_overruns") or 0
    qos = stream.get("qos_events") or 0

    cpu = system.get("cpu_percent")
    cores = [c for c in (system.get("cpu_cores") or []) if c is not None]
    temp = system.get("cpu_temp")
    throttle = system.get("throttled") or {}
    wifi = system.get("wifi") or {}
    nets = system.get("network") or []
    primary = next((n for n in nets if (n.get("addresses") or [])), {})

    arrival_ratio = _ratio(arrival, declared)
    render_ratio = _ratio(render, arrival)

    # --- upstream: are frames even arriving? ---------------------------
    upstream_short = arrival_ratio is not None and arrival_ratio < CLEAR_DEFICIT_RATIO
    # --- downstream: do arriving frames reach the screen? ---------------
    downstream_short = render_ratio is not None and render_ratio < CLEAR_DEFICIT_RATIO
    downstream_short = downstream_short or overruns > 0

    if declared:
        note(
            "bad" if upstream_short else "good",
            f"Sender declares {declared:g} fps; {arrival:g} fps arriving"
            + (f" ({arrival_ratio * 100:.0f}% of expected)" if arrival_ratio else ""),
        )
    if arrival is not None and render is not None:
        note(
            "bad" if downstream_short else "good",
            f"{arrival:g} fps arriving; {render:g} fps reaching the display"
            + (f" ({render_ratio * 100:.0f}%)" if render_ratio else ""),
        )
    if dropped:
        note("warn", f"{dropped:,} frames dropped by the display sink")
    if overruns:
        note("bad", f"{overruns:,} queue overruns — the display path fell behind")
    if qos:
        note("warn", f"{qos:,} QoS events — the sink reported lateness upstream")

    if cpu is not None:
        level = "bad" if cpu >= CPU_SATURATED else "warn" if cpu >= CPU_BUSY else "good"
        note(level, f"CPU at {cpu:.0f}%")
    if cores:
        maxed = sum(1 for c in cores if c >= CPU_SATURATED)
        if maxed:
            note("warn", f"{maxed} of {len(cores)} cores at or above {CPU_SATURATED:.0f}%")
    if temp is not None and temp >= TEMP_WARN:
        note("warn", f"CPU at {temp:.0f} °C")
    for flag in throttle.get("now") or []:
        note("bad", f"Power/thermal: {flag}")
    for flag in throttle.get("since_boot") or []:
        note("warn", f"Power/thermal since boot: {flag}")

    on_wifi = bool(wifi.get("present"))
    if on_wifi:
        signal = wifi.get("signal_dbm")
        if signal is not None:
            level = "bad" if signal <= WIFI_WEAK_DBM else "warn" if signal <= WIFI_MARGINAL_DBM else "good"
            note(level, f"Wi-Fi signal {signal:.0f} dBm on {wifi.get('ssid') or 'unknown SSID'}")
        rate = wifi.get("rx_bitrate_mbps")
        if rate is not None:
            note("warn" if rate < 200 else "good", f"Wi-Fi link rate {rate:g} Mbps")
        if wifi.get("power_save"):
            note("bad", "Wi-Fi power saving is on — it causes periodic frame loss")
    elif primary.get("speed_mbps"):
        note("good", f"Wired link at {primary['speed_mbps']} Mbps")

    rx = primary.get("rx_mbps")
    if rx is not None:
        note("good", f"{rx:g} Mbps inbound")
    if primary.get("rx_errs"):
        note("warn", f"{primary['rx_errs']:,} receive errors/drops on {primary.get('interface')}")

    # --- verdict --------------------------------------------------------
    if throttle.get("now"):
        return {
            "verdict": VERDICT_POWER,
            "headline": "The Pi is being throttled right now.",
            "detail": (
                "Under-voltage or thermal throttling makes the Pi behave exactly "
                "like an overloaded one. Fix the power or cooling before drawing "
                "any conclusion about the network or the stream."
            ),
            "evidence": evidence,
            "suggestions": [
                "Use a genuine 3A USB-C supply — a phone charger is not enough.",
                "Check the temperature; add a heatsink or fan if it is near 80 °C.",
            ],
        }

    if upstream_short and not downstream_short:
        if on_wifi:
            suggestions += [
                "Test the same stream over Ethernet — this is the decisive comparison.",
                "If Wi-Fi is unavoidable, use 5 GHz and get the node closer to the AP.",
                "Confirm Wi-Fi power saving is off.",
            ]
        else:
            suggestions += [
                "Check the cable and switch port — confirm the link negotiated gigabit.",
                "Check whether the sender itself is dropping frames.",
            ]
        suggestions += [
            "Have the sender emit NDI HX instead of full bandwidth — far less data.",
            "Or select the proxy stream in NDI settings.",
            "Raise the receive buffer to ride out jitter.",
        ]
        return {
            "verdict": VERDICT_NETWORK,
            "headline": "Frames are not arriving — this is the network or the sender.",
            "detail": (
                f"The sender declares {declared:g} fps but only {arrival:g} fps are "
                "reaching the receiver. Everything that does arrive is being "
                "displayed, so the Pi is keeping up with what it is given."
            ),
            "evidence": evidence,
            "suggestions": suggestions,
        }

    if downstream_short and not upstream_short:
        suggestions += [
            "Turn off sink QoS — it drops late frames to catch up, which is often the whole problem.",
            "Or turn off clock sync, so every frame is shown as it arrives.",
            "Set scaling to nearest-neighbour, or pin the output to the source resolution so no scaling is needed.",
            "Try colour format 'fastest' to shift work away from the CPU.",
            "Have the sender emit NDI HX, which decodes in hardware; full-bandwidth NDI is CPU-only on a Pi 4.",
        ]
        return {
            "verdict": VERDICT_PI,
            "headline": "Frames arrive but do not reach the screen — this Pi is the bottleneck.",
            "detail": (
                f"{arrival:g} fps are arriving from the network but only "
                f"{render:g} fps are being displayed. The data is getting here; "
                "the Pi cannot process it fast enough."
            ),
            "evidence": evidence,
            "suggestions": suggestions,
        }

    if upstream_short and downstream_short:
        return {
            "verdict": VERDICT_UNKNOWN,
            "headline": "Both the network and the Pi look stressed.",
            "detail": (
                "Frames are short on arrival and short again on display, so the "
                "two effects cannot be separated yet. Remove one variable: run "
                "over Ethernet, or drop to the proxy stream, then look again."
            ),
            "evidence": evidence,
            "suggestions": [
                "Switch to Ethernet to remove the network from the picture.",
                "Or select the proxy stream, which reduces both network and CPU load at once.",
            ],
        }

    healthy_arrival = arrival_ratio is None or arrival_ratio >= HEALTHY_RATIO
    healthy_render = render_ratio is None or render_ratio >= HEALTHY_RATIO
    if healthy_arrival and healthy_render and not dropped:
        return {
            "verdict": VERDICT_HEALTHY,
            "headline": "The stream is healthy.",
            "detail": "Frames are arriving and being displayed at the expected rate.",
            "evidence": evidence,
            "suggestions": [],
        }

    return {
        "verdict": VERDICT_UNKNOWN,
        "headline": "Slight losses, no clear culprit.",
        "detail": (
            "Nothing is far enough out of range to blame confidently. Watch the "
            "ten-minute charts — a slow climb in temperature or a periodic dip in "
            "arrival rate will show up there long before it shows up here."
        ),
        "evidence": evidence,
        "suggestions": [],
    }
