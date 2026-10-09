"""One authorized enterprise attempt. Secrets stay in RAM and never in logs."""
import getpass
import argparse
import glob
import json
import os
import termios
import time
from pathlib import Path




def opened(port):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[2] &= ~termios.HUPCL
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def receive(fd, wanted, seconds):
    data = b""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            chunk = os.read(fd, 8192)
            if chunk:
                data += chunk
        except BlockingIOError:
            time.sleep(.05)
        for line in data.decode(errors="replace").splitlines():
            if "@yyc " not in line:
                continue
            try:
                frame = json.loads(line.split("@yyc ", 1)[1])
            except ValueError:
                continue
            if frame.get("type") == wanted:
                return frame
    return None


def main():
    parser = argparse.ArgumentParser(description="Secure, RAM-only PEAP provisioning")
    parser.add_argument("--port")
    parser.add_argument("--ssid", required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--ca-cert", required=True)
    args = parser.parse_args()
    ports = [args.port] if args.port else glob.glob("/dev/cu.usbmodem*")
    if len(ports) != 1:
        raise SystemExit("Specify the intended USB modem port")
    fd = opened(ports[0])
    os.write(fd, b'{"cmd":"status"}\n')
    status = receive(fd, "telemetry", 5)
    if not status or status.get("board") != "M5Stack StickS3" or not status.get("enterprise_supported"):
        os.close(fd)
        raise SystemExit("Device identity or enterprise firmware readiness not verified")
    if status.get("enterprise_active"):
        os.close(fd)
        raise SystemExit("An enterprise attempt is already active; no automatic retry")
    account = getpass.getpass("Account (hidden): ").strip()
    password = getpass.getpass("Password (hidden): ")
    ca = Path(args.ca_cert).read_text()
    command = {"cmd": "enterprise.configure", "ssid": args.ssid, "username": account,
               "password": password, "server_name": args.server_name,
               "ca_cert": ca, "unix_time": int(time.time())}
    wire = bytearray((json.dumps(command) + "\n").encode())
    sent = 0
    while sent < len(wire):
        try:
            sent += os.write(fd, wire[sent:])
        except BlockingIOError:
            time.sleep(.02)
    for i in range(len(wire)):
        wire[i] = 0
    command["password"] = ""
    password = ""
    account = ""
    ack = receive(fd, "enterprise", 8)
    print("Enterprise API:", json.dumps(ack), flush=True)
    result = {"ssid": args.ssid, "server_name": args.server_name,
              "credentials_persisted": False, "ack": ack, "states": []}
    deadline = time.monotonic() + 55
    while ack and ack.get("ok") and time.monotonic() < deadline:
        os.write(fd, b'{"cmd":"status"}\n')
        frame = receive(fd, "telemetry", 3)
        if frame:
            state = {k: frame.get(k) for k in ("wifi", "ip", "enterprise_active", "wifi_disconnect_reason")}
            result["states"].append(state)
            print("Enterprise state:", json.dumps(state), flush=True)
            if frame.get("wifi"):
                result["connected"] = True
                break
            # The intentional leave from Le-WiFi is reason 8, not an EAP failure.
            if frame.get("wifi_disconnect_reason") not in (None, 0, 8):
                result["connected"] = False
                break
        time.sleep(2)
    result.setdefault("connected", False)
    os.close(fd)
    print("Enterprise result:", "CONNECTED" if result["connected"] else "NOT CONNECTED; no retry", flush=True)


if __name__ == "__main__":
    main()
