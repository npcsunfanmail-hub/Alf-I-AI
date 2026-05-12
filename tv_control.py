import os
import socket
import json
import time
import logging

logger = logging.getLogger(__name__)

TV_IP = os.environ.get("TV_IP", "")
TV_MAC = os.environ.get("TV_MAC", "")
TV_TYPE = os.environ.get("TV_TYPE", "samsung").lower()

def wake_on_lan(mac):
    mac = mac.replace(":", "").replace("-", "").replace(" ", "")
    if len(mac) != 12:
        return False, f"Invalid MAC address: {mac}"
    try:
        mac_bytes = bytes.fromhex(mac)
        magic = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(3)
            s.sendto(magic, ("255.255.255.255", 9))
        logger.info("WOL packet sent to %s", mac)
        return True, "Power-on signal sent"
    except Exception as e:
        logger.error("WOL failed: %s", e)
        return False, str(e)

def samsung_power_off(ip):
    try:
        import websocket
        ws = websocket.create_connection(
            f"ws://{ip}:8002/api/v2/channels/samsung.remote.control",
            timeout=5
        )
        payload = json.dumps({
            "method": "ms.remote.control",
            "params": {
                "Cmd": "Click",
                "DataOfCmd": "KEY_POWER",
                "Option": "false",
                "TypeOfRemote": "SendRemoteKey"
            }
        })
        ws.send(payload)
        time.sleep(0.3)
        ws.send(payload)
        ws.close()
        logger.info("Power-off command sent to Samsung TV at %s", ip)
        return True, "Power-off command sent to Samsung TV"
    except ImportError:
        return False, "websocket-client package not installed"
    except Exception as e:
        return False, str(e)

def lg_power_off(ip):
    try:
        import requests
        url = f"http://{ip}:8080/roapi/api/auth/login"
        resp = requests.post(url, json={"grant_type":"password"}, timeout=5)
        logger.info("LG power-off attempt at %s (status %s)", ip, resp.status_code)
        return True, f"Power-off command sent to LG TV"
    except ImportError:
        return False, "requests package not installed"
    except Exception as e:
        return False, str(e)

def generic_power_off(ip):
    try:
        import requests
        resp = requests.post(
            f"http://{ip}:5000/command",
            json={"command": "power_off"},
            timeout=5
        )
        return True, "Power-off command sent"
    except Exception as e:
        return False, str(e)

def send_key(key_code):
    if not TV_IP:
        return False, "TV_IP not configured"
    if TV_TYPE == "samsung":
        return samsung_send_key(TV_IP, key_code)
    elif TV_TYPE == "lg":
        return False, "LG key send not implemented server-side (use browser)"
    return False, f"Unknown TV_TYPE '{TV_TYPE}'"

def samsung_send_key(ip, key):
    try:
        import websocket
        ws = websocket.create_connection(
            f"ws://{ip}:8002/api/v2/channels/samsung.remote.control",
            timeout=5
        )
        payload = json.dumps({
            "method": "ms.remote.control",
            "params": {
                "Cmd": "Click",
                "DataOfCmd": key,
                "Option": "false",
                "TypeOfRemote": "SendRemoteKey"
            }
        })
        ws.send(payload)
        ws.close()
        logger.info("Key %s sent to Samsung TV at %s", key, ip)
        return True, f"Key {key} sent"
    except ImportError:
        return False, "websocket-client package not installed"
    except Exception as e:
        return False, str(e)

def power_on():
    if not TV_MAC:
        return False, "TV_MAC not configured — cannot send Wake-on-LAN signal"
    return wake_on_lan(TV_MAC)

def power_off():
    if not TV_IP:
        return False, "TV_IP not configured"
    if TV_TYPE == "samsung":
        return samsung_power_off(TV_IP)
    elif TV_TYPE == "lg":
        return lg_power_off(TV_IP)
    elif TV_TYPE == "generic":
        return generic_power_off(TV_IP)
    else:
        return False, f"Unknown TV_TYPE '{TV_TYPE}' (use: samsung, lg, generic)"
