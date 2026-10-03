#!/usr/bin/env python3
"""Bambu P1S -> HTTP bridge for Obico's failure detector.

The P1S publishes telemetry over TLS-MQTT on :8883 and its chamber camera over a
proprietary TLS/JPEG protocol on :6000. Neither is consumable by Home Assistant
directly. This service holds one persistent connection to each and exposes:

    GET  /snapshot.jpg  latest camera frame (JPEG)
    GET  /status.json   printer telemetry
    GET  /detect        run Obico ml_api against the latest frame
    POST /light/on|off  chamber light (detection needs it on)
    GET  /healthz       liveness

Obico's ml_api fetches the image URL itself with a 0.1s connect timeout, so the
newest frame is always held in memory -- /snapshot.jpg never dials the printer.
"""
import concurrent.futures
import ftplib
import http.server
import json
import os
import socket
import socketserver
import ssl
import struct
import sys
import threading
import time
import urllib.request

CODE = os.environ.get("BAMBU_CODE", "")
SERIAL = os.environ.get("BAMBU_SERIAL", "")
PORT = int(os.environ.get("BRIDGE_PORT", "8099"))
ML_URL = os.environ.get("ML_API_URL", "http://ml_api:3333")
ML_TOK = os.environ.get("ML_API_TOKEN", "")
SELF_URL = os.environ.get("SELF_URL", "http://bridge:8099")
STALE = float(os.environ.get("STALE_SECONDS", "30"))
STATE_DIR = os.environ.get("STATE_DIR", "/state")
STATE_FILE = os.path.join(STATE_DIR, "printer_ip")
# Auto-rediscovery after sustained failure. The printer's DHCP lease can move on
# a router reboot; without this the bridge retries a dead IP forever.
AUTO_DISCOVER = os.environ.get("AUTO_DISCOVER", "1") not in ("0", "false", "")
FAILS_BEFORE_DISCOVER = int(os.environ.get("FAILS_BEFORE_DISCOVER", "6"))

_host_lock = threading.Lock()


def _load_host():
    """A rediscovered IP outlives the container; env is only the seed value."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            return saved
    except Exception:
        pass
    return os.environ.get("BAMBU_HOST", "")


def _save_host(ip):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(ip)
        return True
    except Exception:
        return False


_host = _load_host()


def host():
    with _host_lock:
        return _host


def set_host(ip):
    global _host
    with _host_lock:
        changed = ip != _host
        _host = ip
    if changed:
        _save_host(ip)
    return changed

_lock = threading.Lock()
_cam_sock = None
_mqtt_client = None
_frame = None
_stamp = 0.0
_state = {}
_stats = {
    "frames": 0,
    "cam_reconnects": 0,
    "cam_connected": False,
    # cam_error/mqtt_error describe the CURRENT fault only. They are cleared the
    # moment a connection recovers, so a stale string can never imply a live
    # problem -- the reconnect counters are what preserve the history.
    "cam_error": "",
    "mqtt_connected": False,
    "mqtt_error": "",
    "last_discovery": "",
}


# ------------------------------------------------------------- discovery ----
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


def _port_open(h, port, timeout=1.0):
    try:
        with socket.create_connection((h, port), timeout=timeout):
            return True
    except Exception:
        return False


def is_our_printer(h, timeout=6):
    """Identity check via FTPS login.

    Deliberately NOT the camera stream: the P1S accepts only one camera
    connection, and this bridge holds it -- a camera probe would reject the very
    printer we are looking for. FTPS allows concurrent logins.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    f = _ImplicitFTP_TLS(context=ctx)
    try:
        f.connect(host=h, port=990, timeout=timeout)
        f.login("bblp", CODE)
        return True
    except Exception:
        return False
    finally:
        try:
            f.close()
        except Exception:
            pass


def rediscover():
    """Scan the printer's last-known /24 and adopt the host that owns our code."""
    cur = host()
    prefix = os.environ.get("SCAN_SUBNET", "")
    if not prefix:
        if not cur or cur.count(".") != 3:
            return {"ok": False, "error": "no subnet to scan", "ip": cur}
        prefix = cur.rsplit(".", 1)[0]

    hosts = ["%s.%d" % (prefix, i) for i in range(1, 255)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=96) as ex:
        cand = [h for h, ok in zip(hosts, ex.map(lambda h: _port_open(h, 8883), hosts)) if ok]
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
        cand = [h for h, ok in zip(cand, ex.map(lambda h: _port_open(h, 990), cand)) if ok]

    found = [h for h in cand if is_our_printer(h)]
    if not found:
        return {"ok": False, "error": "printer not found on %s.0/24" % prefix,
                "ip": cur, "candidates": cand}
    if len(found) > 1:
        return {"ok": False, "error": "multiple printers matched", "ip": cur,
                "matches": found}

    new = found[0]
    changed = set_host(new)
    if changed:
        _force_reconnect()
    return {"ok": True, "ip": new, "changed": changed,
            "persisted": os.path.isfile(STATE_FILE)}


def _force_reconnect():
    """Drop both links so their loops re-dial the new address immediately."""
    try:
        if _cam_sock:
            _cam_sock.close()
    except Exception:
        pass
    try:
        if _mqtt_client:
            _mqtt_client.disconnect()
    except Exception:
        pass


# ---------------------------------------------------------------- camera ----
def _auth_packet():
    p = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
    p += b"bblp".ljust(32, b"\x00")
    p += CODE.encode("ascii").ljust(32, b"\x00")
    return p


def camera_thread():
    global _frame, _stamp, _cam_sock
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    consecutive_fails = 0
    while True:
        recovered = False
        h = host()
        try:
            with socket.create_connection((h, 6000), timeout=10) as s:
                with ctx.wrap_socket(s, server_hostname=h) as ss:
                    _cam_sock = ss
                    ss.write(_auth_packet())
                    buf, size = bytearray(), 0
                    while True:
                        d = ss.recv(65536)
                        if not d:
                            raise ConnectionError("camera stream closed")
                        buf += d
                        while True:
                            if size == 0:
                                if len(buf) < 16:
                                    break
                                size = struct.unpack("<I", buf[0:4])[0]
                                del buf[:16]
                            if size == 0 or len(buf) < size:
                                break
                            img = bytes(buf[:size])
                            del buf[:size]
                            size = 0
                            if img[:2] == b"\xff\xd8" and img[-2:] == b"\xff\xd9":
                                with _lock:
                                    _frame, _stamp = img, time.time()
                                    _stats["frames"] += 1
                                    # A decoded frame is the only real proof the
                                    # link works: the TCP connect and the auth
                                    # write both succeed even when the stream
                                    # never delivers.
                                    if not recovered:
                                        _stats["cam_connected"] = True
                                        _stats["cam_error"] = ""
                                        recovered = True
                                consecutive_fails = 0
        except Exception as e:
            with _lock:
                _stats["cam_reconnects"] += 1
                _stats["cam_connected"] = False
                _stats["cam_error"] = "%s: %s" % (type(e).__name__, e)
                _frame = None
            consecutive_fails += 1
            # Sustained failure usually means the DHCP lease moved, not that the
            # printer is off. Try to re-find it before giving up for good.
            if AUTO_DISCOVER and consecutive_fails == FAILS_BEFORE_DISCOVER:
                with _lock:
                    _stats["last_discovery"] = "running"
                r = rediscover()
                with _lock:
                    _stats["last_discovery"] = (
                        "found %s" % r["ip"] if r.get("ok") else r.get("error", "failed")
                    )
                if r.get("ok") and r.get("changed"):
                    consecutive_fails = 0
                    continue
            time.sleep(5)


# ------------------------------------------------------------------ mqtt ----
_mqtt_client = None


def mqtt_thread():
    global _mqtt_client
    import paho.mqtt.client as mqtt


    def on_connect(c, u, f, rc, props=None):
        with _lock:
            _stats["mqtt_connected"] = True
            _stats["mqtt_error"] = ""
        c.subscribe("device/%s/report" % SERIAL)
        c.publish(
            "device/%s/request" % SERIAL,
            json.dumps({"pushing": {"sequence_id": "1", "command": "pushall"}}),
        )

    def on_disconnect(c, u, rc, props=None, reason=None):
        with _lock:
            _stats["mqtt_connected"] = False

    def on_message(c, u, msg):
        try:
            d = json.loads(msg.payload)
        except Exception:
            return
        if "print" in d:
            with _lock:
                _state.update(d["print"])

    while True:
        try:
            try:
                cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="printwatch")
            except Exception:
                cl = mqtt.Client(client_id="printwatch")
            cl.username_pw_set("bblp", CODE)
            cl.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
            cl.tls_insecure_set(True)
            cl.on_connect = on_connect
            cl.on_message = on_message
            cl.on_disconnect = on_disconnect
            cl.connect(host(), 8883, 30)
            _mqtt_client = cl
            cl.loop_forever()
        except Exception as e:
            with _lock:
                _stats["mqtt_connected"] = False
                _stats["mqtt_error"] = "%s: %s" % (type(e).__name__, e)
            time.sleep(10)


def print_command(cmd):
    """Send pause / resume / stop to the printer over MQTT.

    Returns (ok, detail). Only reports success when the broker actually accepted
    the publish -- a monitor that claims to have paused a print but did not is
    worse than one that admits it failed.
    """
    if cmd not in ("pause", "resume", "stop"):
        return False, "unsupported command"
    if _mqtt_client is None:
        return False, "mqtt not connected"
    with _lock:
        gs = _state.get("gcode_state")
    if cmd == "pause" and gs != "RUNNING":
        return False, "printer is not running (state=%s)" % gs
    if cmd == "resume" and gs != "PAUSE":
        return False, "printer is not paused (state=%s)" % gs
    try:
        info = _mqtt_client.publish(
            "device/%s/request" % SERIAL,
            json.dumps({"print": {"sequence_id": "3001", "command": cmd}}),
            qos=1,
        )
        info.wait_for_publish(timeout=10)
        if info.rc != 0:
            return False, "publish failed rc=%s" % info.rc
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)

    # A successful publish proves only that the broker took the message, NOT
    # that the printer obeyed it. Cloud-initiated prints (print_type=cloud)
    # silently ignore local pause commands. Claiming a pause that never happened
    # is far worse than reporting the failure, so confirm the state actually
    # changed before reporting success.
    want = {"pause": "PAUSE", "resume": "RUNNING", "stop": "FINISH"}[cmd]
    deadline = time.time() + 12
    while time.time() < deadline:
        time.sleep(0.5)
        with _lock:
            gs = _state.get("gcode_state")
        if gs == want:
            return True, "%s confirmed" % cmd
    with _lock:
        gs = _state.get("gcode_state")
        ptype = _state.get("print_type")
    hint = ""
    if cmd == "pause" and ptype == "cloud":
        hint = (" -- this print was started via Bambu Cloud (print_type=cloud);"
                " local pause is refused. Use LAN-only mode to allow it.")
    return False, ("%s was sent but the printer did not obey (state=%s)%s"
                   % (cmd, gs, hint))


def set_light(on):
    if _mqtt_client is None:
        return False
    _mqtt_client.publish(
        "device/%s/request" % SERIAL,
        json.dumps({
            "system": {
                "sequence_id": "2010",
                "command": "ledctrl",
                "led_node": "chamber_light",
                "led_mode": "on" if on else "off",
                "led_on_time": 500,
                "led_off_time": 500,
                "loop_times": 0,
                "interval_time": 0,
            }
        }),
    )
    return True


# --------------------------------------------------------------- helpers ----
def snapshot():
    with _lock:
        return _frame, (time.time() - _stamp) if _frame else None


def telemetry():
    with _lock:
        s = dict(_state)
        st = dict(_stats)
    light = "unknown"
    for entry in s.get("lights_report", []) or []:
        if entry.get("node") == "chamber_light":
            light = entry.get("mode", "unknown")
    gs = s.get("gcode_state")
    pct = s.get("mc_percent")
    printing = gs == "RUNNING"
    # Only a RUNNING job in the productive middle of a print is worth analysing:
    # the first layers look like an empty plate, and detection needs the light on.
    checkable = bool(
        printing and isinstance(pct, int) and 2 <= pct <= 98 and light == "on"
    )
    return {
        "gcode_state": gs,
        "printing": printing,
        "checkable": checkable,
        "progress": pct,
        "layer": s.get("layer_num"),
        "total_layers": s.get("total_layer_num"),
        "job": s.get("subtask_name"),
        "remaining_min": s.get("mc_remaining_time"),
        "nozzle_temp": s.get("nozzle_temper"),
        "bed_temp": s.get("bed_temper"),
        "chamber_light": light,
        "hms_count": len(s.get("hms", []) or []),
        "printer_ip": host(),
        # cloud-initiated prints refuse local pause commands
        "print_type": s.get("print_type"),
        "pause_possible": s.get("print_type") == "local",
        "mqtt_connected": st["mqtt_connected"],
        "camera_connected": st["cam_connected"],
        "last_discovery": st["last_discovery"],
        "camera_frames": st["frames"],
        "camera_reconnects": st["cam_reconnects"],
        "camera_error": st["cam_error"],
        "mqtt_error": st["mqtt_error"],
    }


def detect():
    f, age = snapshot()
    if f is None:
        return {"ok": False, "error": "no camera frame", "score": 0.0, "n": 0}
    if age is not None and age > STALE:
        return {"ok": False, "error": "frame stale (%.0fs)" % age, "score": 0.0, "n": 0}
    headers = {"Authorization": "Bearer %s" % ML_TOK} if ML_TOK else {}
    req = urllib.request.Request(
        "%s/p/?img=%s/snapshot.jpg" % (ML_URL, SELF_URL), headers=headers
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read()).get("detections", [])
    except Exception as e:
        return {
            "ok": False,
            "error": "%s: %s" % (type(e).__name__, e),
            "score": 0.0,
            "n": 0,
        }
    return {
        "ok": True,
        "score": round(max([x[1] for x in d], default=0.0), 4),
        "n": len(d),
        "frame_age": round(age or 0, 1),
        "ms": int((time.time() - t0) * 1000),
        "detections": d,
    }


# ------------------------------------------------------------------ http ----
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream_mjpeg(self):
        # multipart/x-mixed-replace: the standard MJPEG framing Home Assistant's
        # camera.mjpeg platform consumes. Pushes each new frame as the P1S
        # produces it (~1 fps) and exits cleanly when the viewer disconnects.
        boundary = b"pwframe"
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=%s" % boundary.decode()
        )
        self.end_headers()
        last_sent = 0.0
        try:
            while True:
                with _lock:
                    f, stamp = _frame, _stamp
                if f is not None and stamp != last_sent:
                    last_sent = stamp
                    self.wfile.write(b"--" + boundary + b"\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(f))
                    self.wfile.write(f)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/snapshot.jpg", "/snapshot"):
            f, age = snapshot()
            if f is None:
                return self._send(503, {"error": "no frame"})
            if age > STALE:
                return self._send(503, {"error": "frame stale", "age": round(age, 1)})
            return self._send(200, f, "image/jpeg")
        if p in ("/stream.mjpeg", "/stream"):
            return self._stream_mjpeg()
        if p == "/status.json":
            return self._send(200, telemetry())
        if p == "/detect":
            return self._send(200, detect())
        if p == "/healthz":
            t = telemetry()
            ok = t["mqtt_connected"] and t["camera_frames"] > 0
            return self._send(200 if ok else 503, dict({"ok": ok}, **t))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p in ("/light/on", "/light/off"):
            ok = set_light(p.endswith("/on"))
            return self._send(200 if ok else 503, {"ok": ok})
        if p == "/rediscover":
            r = rediscover()
            return self._send(200 if r.get("ok") else 503, r)
        if p in ("/pause", "/resume", "/stop"):
            ok, detail = print_command(p.lstrip("/"))
            with _lock:
                gs = _state.get("gcode_state")
            return self._send(200 if ok else 409,
                              {"ok": ok, "detail": detail, "gcode_state": gs})
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    missing = [
        k
        for k, v in (
            ("BAMBU_HOST", host()),
            ("BAMBU_CODE", CODE),
            ("BAMBU_SERIAL", SERIAL),
        )
        if not v
    ]
    if missing:
        sys.exit("missing env: " + ", ".join(missing))
    threading.Thread(target=camera_thread, daemon=True).start()
    threading.Thread(target=mqtt_thread, daemon=True).start()
    print("printwatch bridge :%d printer=%s" % (PORT, host()), flush=True)
    Server(("0.0.0.0", PORT), H).serve_forever()
