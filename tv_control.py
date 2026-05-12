import socket
import json
import time
import uuid
import requests
from typing import Optional

SSDP_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    "MAN: \"ssdp:discover\"\r\n"
    "MX: 5\r\n"
    "ST: ssdp:all\r\n"
    "\r\n"
)

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
RECV_BUF = 65535
DISCOVER_TIMEOUT = 6

TV_KEYWORDS = {
    "samsung": ["samsung", "sec", "dlna"],
    "lg": ["lg", "lge", "netcast"],
    "sony": ["sony", "bravia"],
    "vizio": ["vizio"],
    "tcl": ["tcl", "rc602", "rc603"],
    "hisense": ["hisense", "vidia"],
    "panasonic": ["panasonic", "viera"],
    "philips": ["philips", "ambilight"],
    "sharp": ["sharp", "aquos"],
    "toshiba": ["toshiba", "regza"],
    "roku": ["roku"],
    "firetv": ["fire tv", "amazon"],
    "apple tv": ["appletv"],
}

def discover_tvs(timeout: int = DISCOVER_TIMEOUT) -> list[dict]:
    discovered = []
    seen = set()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    sock.settimeout(timeout)
    sock.bind(("0.0.0.0", 0))

    try:
        sock.sendto(SSDP_MSEARCH.encode(), (SSDP_ADDR, SSDP_PORT))
        start = time.time()
        while time.time() - start < timeout:
            try:
                data, addr = sock.recvfrom(RECV_BUF)
            except socket.timeout:
                break
            if addr[0] in seen:
                continue
            seen.add(addr[0])

            text = data.decode("utf-8", errors="replace")
            headers = {}
            for line in text.split("\r\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip().upper()] = v.strip()

            server = (headers.get("SERVER", "") + " " + headers.get("X-USER-AGENT", "")).lower()
            location = headers.get("LOCATION", "")
            st = headers.get("ST", "")
            usn = headers.get("USN", "")
            friendly = headers.get("FRIENDLYNAME", headers.get("FRIENDLY-NAME", headers.get("X-FRIENDLY-NAME", "")))

            tv_type = _identify_tv(server, st, location)
            if not tv_type:
                if any(k in server for k in ["media", "tv", "display", "screen"]) or "mediarenderer" in st.lower():
                    tv_type = "unknown"

            if tv_type:
                discovered.append({
                    "ip": addr[0],
                    "type": tv_type,
                    "server": server[:120],
                    "location": location,
                    "st": st,
                    "usn": usn,
                    "friendly_name": friendly,
                })

    finally:
        sock.close()

    return discovered

def _identify_tv(server: str, st: str, location: str) -> Optional[str]:
    combined = f"{server} {st} {location}".lower()
    for manufacturer, keywords in TV_KEYWORDS.items():
        if any(k in combined for k in keywords):
            return manufacturer
    return None

_active_sessions: dict[str, dict] = {}

def pair_tv(ip: str, tv_type: str, pairing_code: str) -> dict:
    if tv_type == "samsung":
        return _pair_samsung(ip, pairing_code)
    elif tv_type == "lg":
        return _pair_lg(ip, pairing_code)
    elif tv_type in ("roku", "firetv", "apple tv"):
        return _pair_dial(ip, tv_type)
    else:
        result = _pair_samsung(ip, pairing_code)
        if result.get("success"):
            return result
        result = _pair_lg(ip, pairing_code)
        if result.get("success"):
            _active_sessions[ip]["type"] = "lg"
            return result
        return {"success": False, "error": f"Could not pair with {tv_type} TV at {ip}"}

def _pair_samsung(ip: str, pairing_code: str) -> dict:
    try:
        import websocket
    except ImportError:
        return {"success": False, "error": "Missing websocket-client. Install: pip install websocket-client"}

    ws_url = f"ws://{ip}:8002/api/v2/channels/samsung.remote.control"
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
        handshake = json.dumps({
            "method": "ms.remote.control",
            "params": {
                "Cmd": "Pairing",
                "DataOfCmd": pairing_code,
                "Option": "false",
                "TypeOfRemote": "iphone.ios.iap",
                "AppName": "ALF-I",
                "AppId": "com.alfi.remote",
            }
        })
        ws.send(handshake)
        resp = json.loads(ws.recv())
        if resp.get("event") == "ms.remote.control.connected":
            _active_sessions[ip] = {"type": "samsung", "websocket": ws, "ip": ip}
            return {"success": True, "session_id": ip, "tv_type": "samsung"}
        else:
            ws.close()
            return {"success": False, "error": f"Samsung pairing rejected: {resp}"}
    except Exception as e:
        return {"success": False, "error": f"Samsung pairing error: {e}"}

def _pair_lg(ip: str, pairing_code: str) -> dict:
    base_url = f"http://{ip}:8080"
    device_uuid = str(uuid.uuid4())
    try:
        reg = requests.post(f"{base_url}/roap/api/auth", json={
            "parameters": {
                "device_id": device_uuid,
                "device_name": "ALF-I",
                "pairing_type": "code",
            }
        }, timeout=10).json()
        if reg.get("return_code") != 0:
            return {"success": False, "error": f"LG registration failed: {reg}"}

        pair = requests.put(f"{base_url}/roap/api/auth", json={
            "parameters": {"device_id": device_uuid, "pairing_code": pairing_code}
        }, timeout=10).json()
        if pair.get("return_code") == 0:
            _active_sessions[ip] = {"type": "lg", "device_uuid": device_uuid, "ip": ip, "base_url": base_url}
            return {"success": True, "session_id": device_uuid, "tv_type": "lg"}
        return {"success": False, "error": f"LG pairing rejected: {pair}"}
    except Exception as e:
        return {"success": False, "error": f"LG pairing error: {e}"}

def _pair_dial(ip: str, tv_type: str) -> dict:
    try:
        requests.post(f"http://{ip}:8060/keypress/PowerOn", timeout=5)
        _active_sessions[ip] = {"type": tv_type, "ip": ip, "base_url": f"http://{ip}:8060"}
        return {"success": True, "session_id": ip, "tv_type": tv_type}
    except Exception as e:
        return {"success": False, "error": f"{tv_type} pairing error: {e}"}

SAMSUNG_KEYS = {
    "power": "KEY_POWER", "power_off": "KEY_POWER", "power_on": "KEY_POWER",
    "volume_up": "KEY_VOLUP", "volume_down": "KEY_VOLDOWN", "mute": "KEY_MUTE",
    "channel_up": "KEY_CHUP", "channel_down": "KEY_CHDOWN",
    "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
    "enter": "KEY_ENTER", "back": "KEY_RETURN", "exit": "KEY_EXIT",
    "home": "KEY_HOME", "menu": "KEY_MENU", "guide": "KEY_GUIDE", "info": "KEY_INFO",
    "play": "KEY_PLAY", "pause": "KEY_PAUSE", "stop": "KEY_STOP",
    "rewind": "KEY_REWIND", "fastforward": "KEY_FF",
    "source": "KEY_SOURCE", "input": "KEY_SOURCE",
    "0": "KEY_0", "1": "KEY_1", "2": "KEY_2", "3": "KEY_3", "4": "KEY_4",
    "5": "KEY_5", "6": "KEY_6", "7": "KEY_7", "8": "KEY_8", "9": "KEY_9",
}

LG_KEYS = {
    "power": 1, "power_off": 1, "power_on": 1,
    "volume_up": 24, "volume_down": 25, "mute": 8,
    "channel_up": 33, "channel_down": 34,
    "up": 11, "down": 12, "left": 13, "right": 14,
    "enter": 15, "back": 23, "exit": 23,
    "home": 18, "menu": 17, "guide": 32, "info": 31,
    "play": 35, "pause": 36, "stop": 37,
    "rewind": 39, "fastforward": 40,
    "source": 73, "input": 73,
}

def send_remote_command(ip: str, command: str) -> dict:
    session = _active_sessions.get(ip)
    if not session:
        return {"success": False, "error": "Not paired. Call pair_tv first."}

    if session["type"] == "samsung":
        ws = session.get("websocket")
        if not ws:
            return {"success": False, "error": "No websocket session"}
        key = SAMSUNG_KEYS.get(command.lower())
        if not key:
            return {"success": False, "error": f"Unknown command: {command}"}
        try:
            ws.send(json.dumps({
                "method": "ms.remote.control",
                "params": {
                    "Cmd": "Click", "DataOfCmd": key,
                    "Option": "false", "TypeOfRemote": "iphone.ios.iap",
                }
            }))
            return {"success": True, "command": command, "key": key}
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif session["type"] == "lg":
        key = LG_KEYS.get(command.lower())
        if not key:
            return {"success": False, "error": f"Unknown command: {command}"}
        try:
            resp = requests.post(f"{session['base_url']}/roap/api/command", json={
                "parameters": {"device_id": session["device_uuid"], "key": key}
            }, timeout=5).json()
            if resp.get("return_code") == 0:
                return {"success": True, "command": command}
            return {"success": False, "error": f"LG command error: {resp}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif session["type"] in ("roku", "firetv", "apple tv"):
        key_map = {
            "power": "Power", "power_off": "PowerOff", "power_on": "PowerOn",
            "volume_up": "VolumeUp", "volume_down": "VolumeDown", "mute": "VolumeMute",
            "up": "Up", "down": "Down", "left": "Left", "right": "Right",
            "enter": "Select", "back": "Back", "home": "Home",
            "play": "Play", "pause": "Play", "rewind": "Rev", "fastforward": "Fwd",
        }
        key = key_map.get(command.lower())
        if not key:
            return {"success": False, "error": f"Unknown command: {command}"}
        try:
            requests.post(f"{session['base_url']}/keypress/{key}", timeout=5)
            return {"success": True, "command": command}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return {"success": False, "error": f"Unsupported TV type: {session['type']}"}

def disconnect(ip: str):
    session = _active_sessions.pop(ip, None)
    if session and session.get("websocket"):
        try:
            session["websocket"].close()
        except Exception:
            pass

def get_paired_tvs() -> list[dict]:
    return [{"ip": ip, "tv_type": info["type"]} for ip, info in _active_sessions.items()]

TV_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "discover_tvs",
            "description": "Scan the local WiFi network for smart TVs using SSDP/UPnP discovery. Returns a list of found TVs with their IP addresses and detected brands (samsung, lg, sony, etc.).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pair_tv",
            "description": "Pair with a TV using the on-screen pairing code. First run discover_tvs to find TVs and get their IP and type. Then ask the user to check their TV for a pairing code and pass it here.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ip": {"type": "string", "description": "TV IP address from discover_tvs"},
                    "tv_type": {"type": "string", "description": "TV brand type from discover_tvs"},
                    "pairing_code": {"type": "string", "description": "The pairing code shown on the TV screen"},
                },
                "required": ["ip", "tv_type", "pairing_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_remote_command",
            "description": "Send a remote control command to a paired TV. Common commands: power, volume_up, volume_down, mute, channel_up, channel_down, up, down, left, right, enter, back, home, menu, play, pause, stop, source, input, 0-9.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ip": {"type": "string", "description": "TV IP address"},
                    "command": {"type": "string", "description": "Remote command name"},
                },
                "required": ["ip", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_paired_tvs",
            "description": "List all currently paired and connected TVs.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "disconnect_tv",
            "description": "Disconnect from a paired TV and clean up the session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ip": {"type": "string", "description": "TV IP address to disconnect"},
                },
                "required": ["ip"],
            },
        },
    },
]
