"""Joining a Wi-Fi network from the hotspot.

One radio: while the hotspot is up the device is in AP mode and cannot scan, so
a join issued without taking the AP down finds no SSID and quietly does nothing
while the hotspot stays on. That was the bug. The other half matters more —
if the join then fails, the hotspot has to come back, because it is how the
operator is reaching the node.

    python3 tests/test_netjoin.py
"""
import importlib.machinery, importlib.util

loader = importlib.machinery.SourceFileLoader("netcfg", "scripts/pistreamer-netcfg")
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("netcfg", loader))
loader.exec_module(m)
m.have_nmcli = lambda: True
m.time.sleep = lambda _s: None


def trial(hotspot_up, join_ok, got_address=True):
    calls = []

    def fake_run(args, timeout=45):
        calls.append(args)
        if args[:4] == ["nmcli", "dev", "wifi", "connect"]:
            return (0, "", "") if join_ok else (4, "", "No network with SSID found")
        return 0, "", ""

    m.run = fake_run
    m.hotspot_active = lambda: hotspot_up
    m.current_wifi_connection = lambda: ""
    m.has_address = lambda _d: got_address
    m.addresses = lambda: ["192.168.0.39"]
    m.save_previous = lambda _n: None
    ok, data, msg = m.act_join({"ssid": "Studio 5G", "password": "secret"})
    return ok, data, msg, calls


def idx(calls, want):
    return next(i for i, c in enumerate(calls) if c[:len(want)] == want)


# The bug: joining from the hotspot must drop AP mode before it tries to scan.
ok, _d, msg, calls = trial(hotspot_up=True, join_ok=True)
assert ok, msg
assert idx(calls, ["nmcli", "con", "down", m.HOTSPOT_CON]) < \
       idx(calls, ["nmcli", "dev", "wifi", "connect"]), calls
assert idx(calls, ["nmcli", "dev", "wifi", "rescan"]) < \
       idx(calls, ["nmcli", "dev", "wifi", "connect"]), "must rescan out of AP mode"
# Its revert timer would otherwise fire later and drag the node back to the AP.
assert idx(calls, ["systemctl", "stop", m.REVERT_UNIT + ".timer"]) < \
       idx(calls, ["nmcli", "dev", "wifi", "connect"]), calls
assert ["nmcli", "con", "delete", m.HOTSPOT_CON] in calls, "AP is gone once joined"

# A wrong passphrase must not strand the operator with no AP and no network.
ok, data, msg, calls = trial(hotspot_up=True, join_ok=False)
assert not ok
assert data["reverted_to"] == m.HOTSPOT_CON, data
assert ["nmcli", "con", "up", m.HOTSPOT_CON] in calls, calls
assert any(c[0] == "systemd-run" for c in calls), "revert must be re-armed"
assert ["nmcli", "con", "delete", m.HOTSPOT_CON] not in calls, "do not delete the way back"

# Connected but no DHCP lease is still a failure, and still needs the AP back.
ok, data, _m, calls = trial(hotspot_up=True, join_ok=True, got_address=False)
assert not ok and data["reverted_to"] == m.HOTSPOT_CON

# No hotspot involved: unchanged behaviour, no AP calls at all.
ok, _d, _m, calls = trial(hotspot_up=False, join_ok=True)
assert ok and not any(m.HOTSPOT_CON in c for c in calls), calls

print("ok")
