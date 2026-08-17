"""Scenario tests for the dropped-frame diagnosis.

Each case is a situation that actually happens on an event network, with the
verdict it must reach. Pure dicts in, verdict out — no hardware needed.

    python3 tests/test_diagnose.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pistreamer.diagnose import diagnose
P = {"mode": "ndi"}
cases = [
    ("Wi-Fi loses frames upstream",
     {"declared_fps":60,"arrival_fps":41.2,"fps":41.2,"dropped":0,"queue_overruns":0,"qos_events":0},
     {"cpu_percent":48,"cpu_cores":[50,45,49,48],"cpu_temp":62,"throttled":{"ok":True,"now":[],"since_boot":[]},
      "wifi":{"present":True,"signal_dbm":-74,"ssid":"VENUE-5G","rx_bitrate_mbps":130,"power_save":False},
      "network":[{"addresses":["10.0.0.5"],"interface":"wlan0","rx_mbps":88,"rx_errs":0}]}, "network"),
    ("Pi cannot keep up",
     {"declared_fps":60,"arrival_fps":59.8,"fps":38.4,"dropped":1420,"queue_overruns":97,"qos_events":300},
     {"cpu_percent":98,"cpu_cores":[99,98,97,99],"cpu_temp":78,"throttled":{"ok":True,"now":[],"since_boot":[]},
      "wifi":{"present":False},
      "network":[{"addresses":["10.0.0.5"],"interface":"eth0","rx_mbps":131,"speed_mbps":1000,"rx_errs":0}]}, "pi"),
    ("Under-voltage masquerading as everything else",
     {"declared_fps":60,"arrival_fps":52,"fps":30,"dropped":900,"queue_overruns":40,"qos_events":10},
     {"cpu_percent":70,"cpu_cores":[70,70,70,70],"cpu_temp":70,
      "throttled":{"ok":False,"now":["under-voltage now"],"since_boot":["throttling has occurred"]},
      "wifi":{"present":False},"network":[]}, "power"),
    ("All good",
     {"declared_fps":60,"arrival_fps":59.9,"fps":59.9,"dropped":0,"queue_overruns":0,"qos_events":0},
     {"cpu_percent":52,"cpu_cores":[52,50,53,51],"cpu_temp":61,"throttled":{"ok":True,"now":[],"since_boot":[]},
      "wifi":{"present":False},
      "network":[{"addresses":["10.0.0.5"],"interface":"eth0","rx_mbps":130,"speed_mbps":1000,"rx_errs":0}]}, "healthy"),
    ("Both stressed — refuse to guess",
     {"declared_fps":60,"arrival_fps":44,"fps":30,"dropped":500,"queue_overruns":22,"qos_events":90},
     {"cpu_percent":96,"cpu_cores":[96,95,97,96],"cpu_temp":76,"throttled":{"ok":True,"now":[],"since_boot":[]},
      "wifi":{"present":True,"signal_dbm":-72,"ssid":"V","rx_bitrate_mbps":115,"power_save":False},
      "network":[{"addresses":["10.0.0.5"],"interface":"wlan0","rx_mbps":70,"rx_errs":31}]}, "inconclusive"),
]
fails = 0
for name, stream, sysd, expect in cases:
    r = diagnose(stream, sysd, P)
    ok = r["verdict"] == expect
    fails += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {r['verdict']} (expected {expect})")
    print(f"      → {r['headline']}")
r = diagnose({}, {}, {"mode":"idle"})
print(("PASS" if r["verdict"]=="idle" else "FAIL"), " idle mode:", r["headline"])
print("\nSample suggestions for the Pi-bound case:")
for s in diagnose(cases[1][1], cases[1][2], P)["suggestions"][:3]:
    print("   -", s)
sys.exit(1 if fails else 0)
