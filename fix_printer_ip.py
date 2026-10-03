#!/usr/bin/env python3
"""Find the P1S after a DHCP change and repoint the bridge at it.

Run this whenever the dashboard says "Bridge Disconnected". A router reboot
reissues DHCP leases and the printer can land on a new address, which leaves the
bridge retrying a dead IP forever.

    python fix_printer_ip.py            # diagnose and fix
    python fix_printer_ip.py --dry-run  # diagnose only, change nothing

Identity is proven by logging in over FTPS with your access code. Matching open
ports alone is not proof; another device could hold 8883/990. FTPS is used
rather than the camera stream because the P1S accepts only ONE camera
connection at a time -- the running bridge holds it, so a camera probe would
reject the very printer we are looking for. FTPS allows concurrent logins.

Standard library only -- no pip install needed.
"""
import argparse
import concurrent.futures
import ftplib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(HERE, ".env")
MQTT_PORT, FTPS_PORT, CAM_PORT = 8883, 990, 6000


def read_env():
    if not os.path.isfile(ENV):
        sys.exit("no .env next to this script (%s)" % ENV)
    cfg = {}
    for line in open(ENV, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """Bambu serves IMPLICIT FTPS on :990; ftplib speaks explicit natively."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=None)
        self._sock = value


def is_our_printer(host, code, timeout=8):
    """True only if this host accepts our access code over FTPS."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    f = _ImplicitFTP_TLS(context=ctx)
    try:
        f.connect(host=host, port=FTPS_PORT, timeout=timeout)
        f.login("bblp", code)
        return True
    except Exception:
        return False
    finally:
        try:
            f.close()
        except Exception:
            pass


def bridge_ok(port=8099):
    """Ask the bridge itself. Cheapest and most authoritative health check."""
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:%d/status.json" % port, timeout=5
        ) as r:
            d = json.loads(r.read())
        return bool(d.get("camera_connected")) and bool(d.get("mqtt_connected")), d
    except Exception:
        return False, None


def local_subnets():
    """/24 prefixes of this machine's private IPv4 addresses."""
    out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                         errors="replace").stdout
    subs = []
    for m in re.finditer(r"IPv4[^:]*:\s*([0-9.]+)", out):
        ip = m.group(1)
        if ip.startswith(("192.168.", "10.")) or re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip):
            pre = ip.rsplit(".", 1)[0]
            if pre not in subs:
                subs.append(pre)
    return subs


def scan(prefix):
    hosts = ["%s.%d" % (prefix, i) for i in range(1, 255)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
        mqtt = set(h for h, ok in zip(hosts, ex.map(lambda h: port_open(h, MQTT_PORT), hosts)) if ok)
    if not mqtt:
        return []
    cand = sorted(mqtt)
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        return [h for h, ok in zip(cand, ex.map(lambda h: port_open(h, FTPS_PORT), cand)) if ok]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = read_env()
    cur, code = cfg.get("BAMBU_HOST", ""), cfg.get("BAMBU_CODE", "")
    if not code:
        sys.exit("BAMBU_CODE missing from .env")
    print("configured printer IP: %s" % cur)

    ok, st = bridge_ok()
    if ok:
        print("  -> bridge reports camera+MQTT connected. Nothing to fix.")
        print("     printer state: %s  frames: %s" % (st.get("gcode_state"), st.get("camera_frames")))
        print("     If the dashboard still disagrees, reload HA templates or restart the bridge:")
        print("       docker compose --project-directory %s up -d --force-recreate bridge" % HERE)
        return 0
    if st is not None:
        print("  -> bridge is up but disconnected (camera=%s mqtt=%s): %s"
              % (st.get("camera_connected"), st.get("mqtt_connected"),
                 st.get("camera_error") or st.get("mqtt_error") or "no error reported"))
    else:
        print("  -> bridge is not answering on :8099 (container down?)")

    if cur and port_open(cur, MQTT_PORT) and is_our_printer(cur, code):
        print("  -> printer IS still at %s; the bridge just needs a restart." % cur)
        if args.dry_run:
            print("[dry-run] would recreate the bridge")
            return 0
        # --force-recreate, never `restart`: `restart` reuses the container's
        # baked-in environment and would silently keep a stale BAMBU_HOST.
        subprocess.run(["docker", "compose", "--project-directory", HERE,
                        "up", "-d", "--force-recreate", "bridge"], check=False)
        print("done. Give it ~15s, then check the 3D Print tab.")
        return 0

    print("  -> printer NOT at %s. Scanning..." % (cur or "(unset)"))

    found = []
    for pre in local_subnets():
        print("  scanning %s.0/24 ..." % pre)
        for host in scan(pre):
            print("    candidate %s (8883+990 open) - verifying access code..." % host)
            if is_our_printer(host, code):
                print("    CONFIRMED: %s accepts your access code" % host)
                found.append(host)
            else:
                print("    rejected: %s refused the access code" % host)
        if found:
            break

    if not found:
        print("\nNo printer found. Check that it is powered on, on Wi-Fi, and that")
        print("LAN Mode is still enabled (Settings > WLAN on the printer screen).")
        print("If you re-enabled LAN Mode, the ACCESS CODE changes - update .env.")
        return 1
    if len(found) > 1:
        print("\nMultiple printers matched: %s -- edit .env by hand." % ", ".join(found))
        return 1

    new = found[0]
    if new == cur:
        print("\nSame IP as configured; the bridge just needs a restart.")
    if args.dry_run:
        print("\n[dry-run] would set BAMBU_HOST=%s and restart the bridge" % new)
        return 0

    txt = open(ENV, encoding="utf-8").read()
    open(ENV, "w", encoding="utf-8").write(
        re.sub(r"^BAMBU_HOST=.*$", "BAMBU_HOST=%s" % new, txt, flags=re.M))
    print("\n.env updated: BAMBU_HOST=%s" % new)

    print("restarting bridge...")
    r = subprocess.run(
        ["docker", "compose", "--project-directory", HERE, "up", "-d",
         "--force-recreate", "bridge"],
        capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print(r.stdout + r.stderr)
        return 1
    print("done. Give it ~15s, then check the 3D Print tab.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
