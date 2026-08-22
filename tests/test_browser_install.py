"""Installing chromium from the GUI.

The request carries no package name. The thing writing it is a web GUI on an
event network, and "install this package for me" with a name attached is a
remote root shell with extra steps.

    python3 tests/test_browser_install.py
"""
import importlib.machinery, importlib.util, sys

loader = importlib.machinery.SourceFileLoader("netcfg", "scripts/pistreamer-netcfg")
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("netcfg", loader))
loader.exec_module(m)

assert "install-chromium" in m.ACTIONS
# No parameter reaches the command line, so a hostile request has nothing to
# ride in on.
calls = []
m.subprocess_env = lambda a, e, t: (calls.append(a), (0, "", ""))[1]
m.shutil.which = lambda _n: None
m.shutil.which = lambda _n: None
ok, _d, msg = m.act_install_chromium({"package": "; rm -rf /", "action": "install-chromium"})
assert not any("rm" in " ".join(c) for c in calls), calls
assert all(c[0] == "apt-get" for c in calls), calls
assert any(c[:2] == ["apt-get", "update"] for c in calls), calls

# which() still says no after a "successful" apt: report failure, not success.
assert not ok, "must verify the binary exists, not trust apt's exit code"

# Chromium alone is not enough: without a compositor it has nothing to draw
# onto here, so the install is not finished and must still fetch cage.
calls.clear()
m.shutil.which = lambda n: "/usr/bin/chromium" if n == "chromium" else None
ok, data, msg = m.act_install_chromium({})
assert any("cage" in c for c in calls), ("cage must still be installed", calls)
assert not any("chromium" in " ".join(c) for c in calls if c[0] == "apt-get"
               and "install" in c), ("chromium must not be reinstalled", calls)

# Both present: do nothing at all.
calls.clear()
m.shutil.which = lambda n: f"/usr/bin/{n}" if n in ("chromium", "cage") else None
ok, data, msg = m.act_install_chromium({})
assert ok and data.get("already") and not calls, (ok, data, calls)

sys.path.insert(0, "src")
from pistreamer import network
assert "install-chromium" in network.ACTIONS

print("ok")
