"""USB provisioning: prompts for credentials without echoing or logging them."""
import argparse
import getpass
import json
import os
import termios
import time
from pathlib import Path
import serial
from dotenv import dotenv_values


def send(port, command, seconds=8):
    s = serial.Serial(port, 115200, timeout=.1)
    attrs = termios.tcgetattr(s.fd)
    attrs[2] &= ~termios.HUPCL
    termios.tcsetattr(s.fd, termios.TCSANOW, attrs)
    s.write((json.dumps(command, ensure_ascii=False) + "\n").encode())
    s.flush()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        line = s.readline().decode("utf-8", "replace").strip()
        if line.startswith("@yyc "):
            print(line)
    s.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", default="/dev/cu.usbmodem101")
    p.add_argument("--status", action="store_true")
    args = p.parse_args()
    if args.status:
        send(args.port, {"cmd": "status"})
    else:
        env = dotenv_values(Path(__file__).with_name(".env"))
        ssid = input("2.4 GHz Wi-Fi SSID: ")
        password = getpass.getpass("Wi-Fi password (not logged): ")
        gateway = input("Gateway URI, e.g. ws://MAC_LAN_IP:8787/ws: ")
        token = env.get("GATEWAY_TOKEN") or getpass.getpass("Gateway token: ")
        send(args.port, {"cmd": "configure", "ssid": ssid, "password": password,
                         "gateway": gateway, "token": token})
