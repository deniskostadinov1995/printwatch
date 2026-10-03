#!/usr/bin/env python3
"""Keep PrintWatch alive across Docker Desktop outages.

The bridge can re-find the printer by itself, but it cannot recover from Docker
Desktop not running -- the recovery code lives inside the container that is gone.
That is the one failure mode the stack cannot self-heal, and it is silent:
detection simply stops.

This script is the outer loop. Run it on a schedule (see --install-help):

    python watchdog.py              # check once, repair if needed
    python watchdog.py --dry-run    # report only
    python watchdog.py --status     # show state, change nothing

Exit codes: 0 healthy (or repaired), 1 still broken.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = "http://127.0.0.1:8099/healthz"
DOCKER_EXE = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
LOG = os.path.join(HERE, "watchdog.log")
DAEMON_WAIT = 180  # Docker Desktop is slow to start; be patient rather than loop


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def bridge_ok():
    try:
        with urllib.request.urlopen(BRIDGE, timeout=6) as r:
            d = json.loads(r.read())
        return bool(d.get("ok")), d
    except Exception as e:
        return False, str(e)


def run(args, timeout=180):
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, "%s: %s" % (type(e).__name__, e)


def daemon_ok():
    rc, _ = run(["docker", "info"], timeout=30)
    return rc == 0


def desktop_running():
    rc, out = run(["tasklist", "/FI", "IMAGENAME eq Docker Desktop.exe"], timeout=30)
    return "Docker Desktop.exe" in out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--install-help", action="store_true")
    args = ap.parse_args()

    if args.install_help:
        print(__doc__)
        print("Register as a scheduled task (runs every 10 minutes, current user):\n")
        print('  schtasks /Create /TN "PrintWatch Watchdog" /SC MINUTE /MO 10 ^\n'
              '    /TR "pythonw \\"%s\\"" /F' % os.path.join(HERE, "watchdog.py"))
        print("\nRemove it again with:\n")
        print('  schtasks /Delete /TN "PrintWatch Watchdog" /F')
        return 0

    ok, detail = bridge_ok()
    if args.status or args.dry_run:
        print("bridge healthy   :", ok)
        print("docker daemon    :", daemon_ok())
        print("Docker Desktop   :", desktop_running())
        if not ok:
            print("bridge detail    :", detail)
        if args.status:
            return 0 if ok else 1

    if ok:
        return 0

    log("bridge unhealthy (%s)" % (detail if isinstance(detail, str) else "see status"))
    if args.dry_run:
        log("[dry-run] would attempt repair")
        return 1

    # 1. Docker Desktop not running at all -> start it and wait for the daemon.
    if not daemon_ok():
        if not desktop_running():
            if not os.path.isfile(DOCKER_EXE):
                log("Docker Desktop not found at %s" % DOCKER_EXE)
                return 1
            log("starting Docker Desktop")
            try:
                subprocess.Popen([DOCKER_EXE], close_fds=True)
            except Exception as e:
                log("failed to launch Docker Desktop: %s" % e)
                return 1
        else:
            log("Docker Desktop is running but the daemon is not ready; waiting")

        t0 = time.time()
        while time.time() - t0 < DAEMON_WAIT:
            time.sleep(10)
            if daemon_ok():
                log("daemon up after %ds" % int(time.time() - t0))
                break
        else:
            log("daemon did not come up within %ds" % DAEMON_WAIT)
            return 1

    # 2. Daemon is up -- make sure our containers are actually running.
    #    `up -d` is intentional over `start`: it also recreates them if the
    #    compose definition or .env changed while things were down.
    log("bringing the stack up")
    rc, out = run(["docker", "compose", "--project-directory", HERE, "up", "-d"],
                  timeout=300)
    if rc != 0:
        log("compose up failed: %s" % out.strip()[:300])
        return 1

    for _ in range(12):
        time.sleep(5)
        ok, _ = bridge_ok()
        if ok:
            log("RECOVERED: bridge healthy again")
            return 0
    log("stack started but bridge still unhealthy")
    return 1


if __name__ == "__main__":
    sys.exit(main())
