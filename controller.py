import serial
import serial.tools.list_ports
import time
import json
import os
import threading
from queue import Queue
import zmq
from datetime import datetime, timezone
import uuid
import re

# -----------------------------
# Globals & constants
# -----------------------------
CONFIG_FILE = "device_config.json"
JOB_STORE_FILE = "job_store.json"

ser = None          # Serial cho BOARD_RELAY
ser_cmd = None      # Serial cho SOFTWARE_COMMAND
cmd_queue = None    # Hàng đợi gửi sang SOFTWARE_COMMAND
config = None       # Toàn bộ cấu hình
ser_lock = threading.Lock()  # Khoá bảo vệ BOARD_RELAY

LOG_LEVELS = {"off": 0, "error": 1, "warn": 2, "info": 3, "debug": 4}

# -----------------------------
# Small helpers
# -----------------------------
def _iso_now():
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _ts_local():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _load_store():
    if os.path.exists(JOB_STORE_FILE):
        try:
            with open(JOB_STORE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"jobs": {}, "sequences": {}}

def _save_store(store):
    with open(JOB_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)

def _log_enabled(level):
    try:
        cur = config.get("logging", {}).get("level", "info").lower()
        return LOG_LEVELS.get(cur, 3) >= LOG_LEVELS.get(level, 3)
    except Exception:
        return True

def log(level, msg):
    if not _log_enabled(level):
        return
    if config.get("logging", {}).get("console", True):
        prefix = f"[{level.upper()}]"
        if config.get("logging", {}).get("timestamps", True):
            prefix = f"[{_ts_local()}] {prefix}"
        print(f"{prefix} {msg}")

def log_json(level, obj):
    if _log_enabled(level) and config.get("logging", {}).get("console", True):
        print(json.dumps(obj, ensure_ascii=False))

def _ok(corr_id, message):
    return {"correlationId": corr_id, "isError": False, "timestamp": _iso_now(), "message": message, "errorMessage": ""}

def _err(corr_id, msg):
    return {"correlationId": corr_id, "isError": True, "timestamp": _iso_now(), "message": {}, "errorMessage": str(msg)}

# -----------------------------
# ASCII/HEX/DEC encoders for SOFTWARE_COMMAND
# -----------------------------
_TOKEN_MAP = {
    "CR": b"\r", "LF": b"\n", "CRLF": b"\r\n", "TAB": b"\t", "ESC": b"\x1B",
    "STX": b"\x02", "ETX": b"\x03", "NUL": b"\x00", "SP": b" "
}

def encode_ascii_with_tokens(text: str) -> bytes:
    """
    Hỗ trợ token dạng <CR>, <LF>, <CRLF>, <STX>, <ETX>, <ESC>, <TAB>, <SP>,
    và <0xNN> (hex), <dNNN> (dec). Phần còn lại coi là ASCII.
    """
    out = bytearray()
    i = 0
    L = len(text)
    while i < L:
        ch = text[i]
        if ch == "<":
            j = text.find(">", i + 1)
            if j == -1:
                # Không có '>' — coi như ký tự thường
                out.extend(ch.encode("latin1", errors="ignore"))
                i += 1
                continue
            token = text[i+1:j].strip()
            up = token.upper()
            if up in _TOKEN_MAP:
                out.extend(_TOKEN_MAP[up])
            elif re.fullmatch(r"0X[0-9A-F]{2}", up):
                out.append(int(up[2:], 16))
            elif re.fullmatch(r"D\d{1,3}", up):
                val = int(up[1:])
                if not (0 <= val <= 255): raise ValueError(f"dec token out of range: <{token}>")
                out.append(val)
            else:
                # không biết token — giữ nguyên "<token>"
                out.extend(("<" + token + ">").encode("latin1", errors="ignore"))
            i = j + 1
        else:
            # ký tự thường: dùng latin-1 (an toàn 0..255)
            out.extend(ch.encode("latin1", errors="ignore"))
            i += 1
    return bytes(out)

def parse_hex_bytes(s: str) -> bytes:
    """
    Cho phép: '02 30 31 03', '0x02,0x30,0x31,0x03', '02-30-31-03'
    """
    toks = re.findall(r"(?:0x)?([0-9A-Fa-f]{2})", s)
    if not toks and s.strip():
        raise ValueError("HEX không hợp lệ")
    return bytes(int(t, 16) for t in toks)

def parse_dec_bytes(s: str) -> bytes:
    """
    Cho phép: '2,48,49,3' hoặc '2 48 49 3'
    """
    toks = re.findall(r"\d+", s)
    if not toks and s.strip():
        raise ValueError("DEC không hợp lệ")
    arr = []
    for t in toks:
        v = int(t)
        if not (0 <= v <= 255):
            raise ValueError(f"DEC out of range: {v}")
        arr.append(v)
    return bytes(arr)

# -----------------------------
# Emit/pulse signal (giữ để dùng nếu cần)
# -----------------------------
def _emit_signal(signal_name, value, ts=None):
    global ser_cmd, cmd_queue
    if ts is None:
        ts = _ts_local()
    if ser_cmd is not None and cmd_queue is not None:
        cmd_queue.put((signal_name, int(value), ts))
    log("debug", f"Emit signal -> SOFTWARE_COMMAND: {signal_name}={int(value)} at {ts}")

def _pulse_signal(signal_name, pulse_ms=200):
    _emit_signal(signal_name, 1)
    threading.Timer(pulse_ms/1000.0, lambda: _emit_signal(signal_name, 0)).start()
    log("info", f"Pulse signal {signal_name} {pulse_ms}ms")

def send_raw_to_software_command(raw_bytes: bytes, repeat=1, delay_ms=0):
    """
    Đẩy bytes raw xuống cổng SOFTWARE_COMMAND (qua writer thread).
    """
    global cmd_queue, ser_cmd
    if ser_cmd is None:
        raise RuntimeError("SOFTWARE_COMMAND chưa được cấu hình (com_port=None)")
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise TypeError("raw_bytes phải là bytes")
    repeat = max(1, int(repeat))
    delay_ms = max(0, int(delay_ms))
    for _ in range(repeat):
        cmd_queue.put(("raw_bytes", bytes(raw_bytes)))
        if delay_ms > 0: time.sleep(delay_ms/1000.0)

# -----------------------------
# Serial & Config
# -----------------------------
def get_available_ports():
    ports = serial.tools.list_ports.comports()
    return [{"device": p.device, "description": p.description, "hwid": p.hwid} for p in ports]

def load_config():
    default_config = {
        "devices": {
            "BOARD_RELAY": {
                "com_port": None, "baud_rate": 9600, "slave_id": 1,
                "read_settings": {"start_address": 129, "num_registers": 8, "interval_ms": 500}
            },
            "SOFTWARE_COMMAND": {
                "com_port": None, "baud_rate": 115200,
                # protocol: ndjson | ascii | raw  (raw = gửi byte đúng như SC lệnh)
                "protocol": "raw",
                "signals": { "Ready": {"index": 0, "mode": "any"}, "Home": {"index": 1, "mode": "any"}, "Reset": {"index": 2, "mode": "any"} },
                "emit_options": { "debounce_ms": 0, "edge_only": False, "min_interval_ms": 0 },
                # Templates cho SC_TEMPLATE (ví dụ, bạn đổi theo tài liệu máy khắc)
                "templates": {
                    "START": "START<CR>",
                    "STOP":  "STOP<CR>",
                    "HOME":  "<STX>HOME<ETX><CR>",
                    "EX_HEX": "",      # ví dụ template hex để trống (sẽ không dùng)
                    "EX_DEC": ""       # ví dụ template dec để trống
                },
                # Phụ trợ: nối thêm sau mỗi lệnh nếu muốn (chỉ dùng khi SC_SEND mode=ascii và appendIfMissing=true)
                "default_append": ""
            }
        },
        "active_device": "BOARD_RELAY",
        "zeromq": {"rep_bind": "tcp://*:5555","rcv_timeout_ms": 1000,"snd_timeout_ms": 1000,"pub_bind": "tcp://*:5556","publish": True},
        "builtin_map": {
            "rt_home": [ { "type":"signal", "name":"Home",  "value":1, "pulse_ms":200 } ],
            "sw_reset":[ { "type":"signal", "name":"Reset", "value":1, "pulse_ms":200 } ]
        },
        "app": { "position": { "x_index": 0, "y_index": 1, "scale": 0.01 } },
        "logging": { "level": "info", "timestamps": True, "console": True, "show_prompt": True }
    }

    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"Lỗi đọc file cấu hình: {e}")
            cfg = {}

    def deep_merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                deep_merge(dst[k], v)
            else:
                dst[k] = v
        return dst

    return deep_merge(default_config.copy(), cfg)

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        log("info", "Đã lưu cấu hình thiết bị")
    except Exception as e:
        log("error", f"Lỗi lưu file cấu hình: {e}")

def setup_com_ports(cfg):
    def list_and_pick(device_key):
        nonlocal cfg
        while True:
            ports = get_available_ports()
            log("info", f"--- Cấu hình cổng cho {device_key} ---")
            saved = cfg["devices"][device_key].get("com_port")
            if saved and any(p["device"] == saved for p in ports):
                log("info", f"  Dùng lại cổng đã lưu: {saved}")
                return
            if saved and not any(p["device"] == saved for p in ports):
                log("warn", f"  Cổng đã lưu ({saved}) không còn, cần chọn lại.")
            if not ports:
                print("  Không tìm thấy cổng serial nào. Cắm thiết bị rồi Enter để refresh.")
                input(); continue
            print("  Danh sách cổng hiện có:")
            for i, p in enumerate(ports, 1):
                print(f"   {i}. {p['device']} - {p['description']}")
            print("  Nhập số (1..n) để chọn, hoặc gõ tên (COM5). Enter = None, 'r' = refresh.")
            raw = input("  Chọn: ").strip()
            if raw == "": cfg["devices"][device_key]["com_port"] = None; return
            if raw.lower() == "r": continue
            if raw.isdigit():
                idx = int(raw)
                if 1 <= idx <= len(ports):
                    cfg["devices"][device_key]["com_port"] = ports[idx-1]["device"]; return
                else:
                    print("  Số không hợp lệ."); continue
            cfg["devices"][device_key]["com_port"] = raw; return

    list_and_pick("BOARD_RELAY")
    list_and_pick("SOFTWARE_COMMAND")
    br = cfg["devices"]["BOARD_RELAY"].get("com_port")
    sc = cfg["devices"]["SOFTWARE_COMMAND"].get("com_port")
    if br and sc and br == sc:
        log("warn", "BOARD_RELAY và SOFTWARE_COMMAND đang dùng CÙNG MỘT cổng.")
    save_config(cfg)

def open_serial_for(device_key, cfg):
    dev = cfg["devices"].get(device_key, {})
    port = dev.get("com_port")
    baud = dev.get("baud_rate", 9600)
    if not port: return None
    try:
        s = serial.Serial(port, baud, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=1)
        log("info", f"[{device_key}] Kết nối tới {port} ({baud}bps) thành công.")
        return s
    except Exception as e:
        log("error", f"[{device_key}] Lỗi mở cổng {port}: {e}")
        return None

# -----------------------------
# Modbus Utilities (BOARD_RELAY)
# -----------------------------
def calculate_crc(data):
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for _ in range(8):
            if crc & 0x0001:
                crc >>= 1; crc ^= 0xA001
            else:
                crc >>= 1
    return crc.to_bytes(2, byteorder="little")

def read_holding_registers(slave_id, start_addr, num_registers):
    global ser
    try:
        data = [slave_id, 0x03, start_addr >> 8, start_addr & 0xFF, num_registers >> 8, num_registers & 0xFF]
        data += list(calculate_crc(data))
        with ser_lock:
            ser.reset_input_buffer()
            ser.write(bytearray(data))
            time.sleep(0.1)
            expected_bytes = 3 + (num_registers * 2) + 2
            response = ser.read(expected_bytes)
        if len(response) != expected_bytes: return None
        if response[-2:] != calculate_crc(response[:-2]): return None
        values = []
        for i in range(3, len(response)-2, 2):
            values.append((response[i] << 8) | response[i+1])
        return values
    except Exception:
        return None

def control_single_relay(slave_id, relay_addr, state_value, retries=2, tx_delay_s=0.02):
    global ser
    if not (1 <= relay_addr <= 12):
        return {"ok": False, "error": "Địa chỉ relay phải 1..12"}
    frame = [slave_id, 0x10, 0x00, relay_addr, 0x00, 0x01, 0x02, state_value, 0x00]
    frame += list(calculate_crc(frame))
    ex_map = {0x01:"Illegal Function",0x02:"Illegal Data Address",0x03:"Illegal Data Value",0x04:"Slave Device Failure",0x05:"Acknowledge",0x06:"Slave Device Busy",0x07:"Negative Acknowledge",0x08:"Memory Parity Error"}

    for attempt in range(retries + 1):
        try:
            with ser_lock:
                ser.reset_input_buffer()
                ser.write(bytearray(frame))
                time.sleep(tx_delay_s)
                resp = ser.read(8)
                if len(resp) == 0: resp = ser.read(8)

            if len(resp) >= 5 and (resp[1] & 0x80):
                ex_resp = resp[:5]
                if ex_resp[-2:] != calculate_crc(ex_resp[:-2]):
                    if attempt < retries: continue
                    return {"ok": False, "error": "CRC sai (exception)", "detail": {"raw": ex_resp.hex()}}
                ex_code = ex_resp[2]
                return {"ok": False, "error": f"Modbus exception: {ex_map.get(ex_code, f'0x{ex_code:02X}')} ({ex_code})", "detail": {"raw": ex_resp.hex()}}

            if len(resp) == 8:
                if resp[-2:] != calculate_crc(resp[:-2]):
                    if attempt < retries: continue
                    return {"ok": False, "error": "CRC sai (FC16)", "detail": {"raw": resp.hex()}}
                ok = (resp[0]==slave_id) and (resp[1]==0x10) and (resp[2]==0x00 and resp[3]==relay_addr) and (resp[4]==0x00 and resp[5]==0x01)
                if ok:
                    return {"ok": True, "detail": {"slave": resp[0], "function": resp[1], "address": (resp[2]<<8)|resp[3], "quantity": (resp[4]<<8)|resp[5]}}
                else:
                    return {"ok": False, "error": "Echo không khớp", "detail": {"raw": resp.hex()}}
            if attempt < retries: continue
            return {"ok": False, "error": f"Độ dài phản hồi bất thường ({len(resp)} bytes)", "detail": {"raw": resp.hex()}}
        except Exception as e:
            if attempt < retries: continue
            return {"ok": False, "error": f"Lỗi khi ghi relay: {e}"}

# -----------------------------
# Command Processing (legacy)
# -----------------------------
def deep_merge_update(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge_update(dst[k], v)
        else:
            dst[k] = v
    return dst

def exec_command(command_json, device_config):
    global config
    if "read" in command_json:
        return {"type": "info", "message": "Đang đọc dữ liệu tự động trong nền"}
    if "commands" in command_json:
        responses = []
        for cmd in command_json["commands"]:
            relay_num = int(cmd["relay"])
            state_value = 1 if int(cmd["state"]) == 1 else 2
            result = control_single_relay(device_config.get("slave_id", 1), relay_num, state_value)
            if result["ok"]:
                responses.append({"type":"control_response","relay":relay_num,"command":"ON" if state_value==1 else "OFF","status":"success","echo": result.get("detail", {})})
            else:
                responses.append({"type":"control_response","relay":relay_num,"command":"ON" if state_value==1 else "OFF","status":"error","message": result.get("error","unknown"),"detail": result.get("detail", {})})
            time.sleep(0.03)
        return {"responses": responses}
    if "all" in command_json:
        state_value = 1 if int(command_json["all"]) == 1 else 2
        responses = []
        for relay_num in range(1, 13):
            result = control_single_relay(device_config.get("slave_id", 1), relay_num, state_value)
            responses.append({"type":"control_response","relay":relay_num,"command":"ON" if state_value==1 else "OFF","status":"success" if result["ok"] else "error","echo": result.get("detail", {}), "message": None if result["ok"] else result.get("error")})
            time.sleep(0.03)
        return {"responses": responses}
    if "config" in command_json:
        deep_merge_update(config, command_json["config"])
        save_config(config)
        return {"type": "config_response", "status": "success", "config": config}
    if command_json.get("read_once"):
        dev = config["devices"]["BOARD_RELAY"]
        values = read_holding_registers(dev.get("slave_id", 1), dev["read_settings"]["start_address"], dev["read_settings"]["num_registers"])
        ts = _ts_local()
        if values is None:
            return {"type":"error","message":"Read timeout/CRC error"}
        summary = make_state_summary(values, dev, config, ts)
        return {"type":"read_once_response","device":"BOARD_RELAY","values": values,"summary": summary,"timestamp": ts}
    return {"type":"error","message":"Cấu trúc lệnh không hợp lệ","valid_commands":[{"commands":[{"relay":1,"state":1},{"relay":2,"state":0}]},{"all":1},{"config":{"devices":{"SOFTWARE_COMMAND":{"protocol":"raw"}}}},{"read_once":True}]}

def process_command(command_json, device_config):
    try:
        result = exec_command(command_json, device_config)
        log_json("info", result)
    except Exception as e:
        log_json("error", {"type":"error","message":str(e)})

# -----------------------------
# SOFTWARE_COMMAND: state summary (giữ cho PUB/UI, KHÔNG gửi xuống SC nếu protocol=raw)
# -----------------------------
def make_state_summary(values, device_config, cfg, ts):
    soft_cfg = cfg["devices"].get("SOFTWARE_COMMAND", {})
    signals = soft_cfg.get("signals", {})
    idx_ready = int(signals.get("Ready", {}).get("index", 0))
    idx_home  = int(signals.get("Home",  {}).get("index", 1))
    idx_reset = int(signals.get("Reset", {}).get("index", 2))
    def get_val(idx): return 1 if (0 <= idx < len(values) and values[idx]) else 0
    return {"type":"soft_state","device":"SOFTWARE_COMMAND","source":"BOARD_RELAY","address":device_config["read_settings"]["start_address"],"states":{"Ready":get_val(idx_ready),"Home":get_val(idx_home),"Reset":get_val(idx_reset)},"ts":ts}

# -----------------------------
# SOFTWARE_COMMAND writer thread
# -----------------------------
def software_command_writer(stop_event, ser_cmd_local, queue_local, cfg):
    if ser_cmd_local is None: return
    proto = cfg["devices"]["SOFTWARE_COMMAND"].get("protocol", "raw")
    last_emit_at = 0
    min_interval_ms = int(cfg["devices"]["SOFTWARE_COMMAND"].get("emit_options", {}).get("min_interval_ms", 0))
    while not stop_event.is_set():
        try:
            item = queue_local.get(timeout=0.2)
        except Exception:
            continue
        if min_interval_ms > 0:
            now = int(time.time() * 1000)
            if now - last_emit_at < min_interval_ms:
                time.sleep((min_interval_ms - (now - last_emit_at))/1000.0)
        try:
            # 1) Raw bytes gửi xuống SC
            if isinstance(item, tuple) and len(item) == 2 and item[0] == "raw_bytes":
                ser_cmd_local.write(item[1])
                last_emit_at = int(time.time()*1000)
                log("debug", f"SC RAW TX: {item[1].hex(' ')}")
                continue
            # 2) Các chế độ cũ (ndjson/ascii) nếu còn dùng
            if isinstance(item, dict):
                if proto == "ndjson":
                    ser_cmd_local.write((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
                elif proto == "ascii":
                    if item.get("type") == "soft_state":
                        st = item.get("states", {})
                        line = f"STATE Ready={int(st.get('Ready',0))} Home={int(st.get('Home',0))} Reset={int(st.get('Reset',0))}\n"
                        ser_cmd_local.write(line.encode("ascii"))
                    else:
                        ser_cmd_local.write((json.dumps(item, ensure_ascii=False) + "\n").encode("ascii"))
                # if proto == "raw": ignore dict items
                last_emit_at = int(time.time()*1000)
            elif isinstance(item, tuple) and len(item) == 3:
                signal, value, ts = item
                if proto == "ndjson":
                    payload = {"type":"soft_command","device":"SOFTWARE_COMMAND","signal":signal,"value":int(value),"source":"BOARD_RELAY","ts":ts}
                    ser_cmd_local.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                elif proto == "ascii":
                    ser_cmd_local.write(f"{signal.upper()}={int(value)}\n".encode("ascii"))
                # if proto == "raw": ignore soft_command
                last_emit_at = int(time.time()*1000)
        except Exception as e:
            log("error", f"Lỗi gửi SOFTWARE_COMMAND: {e}")

# -----------------------------
# Reader + PUB + edge detect
# -----------------------------
def background_read(stop_event, last_values, error_count, device_config):
    global ser_cmd, cmd_queue, config
    soft_cfg = config["devices"].get("SOFTWARE_COMMAND", {})
    signals = soft_cfg.get("signals", {})
    emit_opts = soft_cfg.get("emit_options", {})
    debounce_ms = int(emit_opts.get("debounce_ms", 0))
    edge_only = bool(emit_opts.get("edge_only", False))
    proto = soft_cfg.get("protocol", "raw")

    # PUB
    pub = None
    try:
        zmq_cfg = config.get("zeromq", {})
        if zmq_cfg.get("publish", True) and zmq_cfg.get("pub_bind"):
            ctx = zmq.Context.instance()
            pub = ctx.socket(zmq.PUB)
            pub.sndtimeo = 1000
            pub.bind(zmq_cfg["pub_bind"])
            log("info", f"[ZMQ] PUB bound at {zmq_cfg['pub_bind']}")
            time.sleep(0.3)
    except Exception as e:
        log("error", f"[ZMQ] PUB init error: {e}")
        pub = None

    last_emit_time = {}
    index_to_name = {}
    for name, spec in signals.items():
        try: index_to_name[int(spec.get("index"))] = name
        except Exception: continue

    try:
        while not stop_event.is_set():
            try:
                values = read_holding_registers(device_config.get("slave_id", 1),
                                                device_config["read_settings"]["start_address"],
                                                device_config["read_settings"]["num_registers"])
                if values:
                    error_count["count"] = 0
                    prev = last_values.get("values")
                    if values != prev:
                        diff = []
                        if prev is None:
                            for i, v in enumerate(values):
                                diff.append({"index": i, "old": None, "new": v})
                        else:
                            for i, v in enumerate(values):
                                ov = prev[i] if i < len(prev) else None
                                if ov != v: diff.append({"index": i, "old": ov, "new": v})

                        response = {"type":"read_response","device":"BOARD_RELAY","address":device_config["read_settings"]["start_address"],"values":values,"timestamp":_ts_local()}
                        summary  = make_state_summary(values, device_config, config, response["timestamp"])
                        # KHÔNG gửi xuống SC nếu protocol=raw (theo yêu cầu mới)
                        if ser_cmd is not None and proto in ("ndjson","ascii"):
                            cmd_queue.put(summary)

                        log_json("debug", response)
                        log_json("info", summary)
                        if pub is not None:
                            try:
                                pub.send_multipart([b"read_response", json.dumps(response, ensure_ascii=False).encode("utf-8")])
                                pub.send_multipart([b"soft_state",    json.dumps(summary,  ensure_ascii=False).encode("utf-8")])
                                pub.send_multipart([b"io_change",     json.dumps({"type":"io_change","changes": diff,"timestamp": response["timestamp"]}, ensure_ascii=False).encode("utf-8")])
                            except Exception as e:
                                log("error", f"[ZMQ] PUB send error: {e}")

                        if config.get("logging", {}).get("show_prompt", True) and _log_enabled("info"):
                            print("Nhập lệnh (? để xem hướng dẫn): ", end="", flush=True)

                        # Edge events -> chỉ PUB (và NDJSON nếu proto != raw)
                        if ser_cmd is not None or pub is not None:
                            for idx, name in index_to_name.items():
                                if not (0 <= idx < len(values)): continue
                                new_val = 1 if values[idx] else 0
                                old_val = None
                                if prev is not None and idx < len(prev): old_val = 1 if prev[idx] else 0
                                need_emit = False
                                mode = signals[name].get("mode", "any")

                                if old_val is None:
                                    if mode=="any" or (mode=="rising" and new_val==1) or (mode=="falling" and new_val==0): need_emit = True
                                else:
                                    if new_val != old_val:
                                        if mode=="any": need_emit=True
                                        elif mode=="rising" and old_val==0 and new_val==1: need_emit=True
                                        elif mode=="falling" and old_val==1 and new_val==0: need_emit=True

                                if edge_only and mode=="any": pass

                                if need_emit and debounce_ms > 0:
                                    now_ms = int(time.time()*1000)
                                    last_ts = last_emit_time.get(name, 0)
                                    if now_ms - last_ts < debounce_ms:
                                        need_emit = False
                                    else:
                                        last_emit_time[name] = now_ms

                                if need_emit:
                                    ts = response["timestamp"]
                                    if ser_cmd is not None and proto in ("ndjson","ascii"):
                                        cmd_queue.put((name, new_val, ts))
                                    if pub is not None:
                                        try:
                                            evt = {"type":"soft_command","device":"SOFTWARE_COMMAND","signal":name,"value":int(new_val),"source":"BOARD_RELAY","ts":ts}
                                            pub.send_multipart([b"soft_command", json.dumps(evt, ensure_ascii=False).encode("utf-8")])
                                        except Exception as e:
                                            log("error", f"[ZMQ] PUB send error: {e}")

                        last_values["values"] = values
                else:
                    error_count["count"] += 1
                    if error_count["count"] == 5:
                        log("warn", "Không thể đọc dữ liệu. Kiểm tra kết nối.")
                time.sleep(device_config["read_settings"]["interval_ms"]/1000.0)
            except Exception as e:
                error_count["count"] += 1
                if error_count["count"] == 5:
                    log("error", f"Lỗi: {str(e)}")
                time.sleep(1)
    finally:
        if pub is not None:
            try: pub.close(0)
            except: pass

# -----------------------------
# RPC router (schema mới)
# -----------------------------
def _rpc_read_once_snapshot():
    dev = config["devices"]["BOARD_RELAY"]
    values = read_holding_registers(dev.get("slave_id", 1), dev["read_settings"]["start_address"], dev["read_settings"]["num_registers"])
    if values is None: raise RuntimeError("Read timeout/CRC error")
    ts_local = _ts_local()
    summary = make_state_summary(values, dev, config, ts_local)
    return values, summary

def handle_envelope(envelope):
    store = _load_store()
    message_id = envelope.get("messageId") or str(uuid.uuid4())
    cmd = (envelope.get("command") or "").upper().strip()
    payload = envelope.get("payload") or {}

    # Health-check
    if envelope.get("read") is True:
        return _ok(message_id, {"note": "background reader running"})

    # ==== SOFTWARE_COMMAND: gửi lệnh xuống máy khắc ====
    # SC_SEND: {"mode":"ascii|hex|dec", "text":"...", "data":"...", "bytes":[..], "append":"<CR>", "appendIfMissing":true, "repeat":1, "delayMs":0}
    if cmd == "SC_SEND":
        if ser_cmd is None:
            return _err(message_id, "SOFTWARE_COMMAND chưa cấu hình (com_port=None)")
        mode = (payload.get("mode") or "ascii").lower()
        repeat = int(payload.get("repeat", 1))
        delay_ms = int(payload.get("delayMs", 0))
        append = payload.get("append")  # chỉ áp dụng cho ascii
        append_if_missing = bool(payload.get("appendIfMissing", True))
        try:
            if mode == "ascii":
                text = payload.get("text", "")
                if append:
                    # nếu chưa có chuỗi append ở cuối thì nối vào
                    ap = encode_ascii_with_tokens(append)
                    tx = encode_ascii_with_tokens(text)
                    if append_if_missing and not tx.endswith(ap):
                        tx = tx + ap
                    raw = tx
                else:
                    raw = encode_ascii_with_tokens(text)
            elif mode == "hex":
                if "bytes" in payload:
                    raw = bytes(int(b) & 0xFF for b in payload["bytes"])
                else:
                    raw = parse_hex_bytes(payload.get("data", ""))
            elif mode == "dec":
                if "bytes" in payload:
                    raw = bytes(int(b) & 0xFF for b in payload["bytes"])
                else:
                    raw = parse_dec_bytes(payload.get("data", ""))
            else:
                return _err(message_id, f"Unknown mode '{mode}'")
            send_raw_to_software_command(raw, repeat=repeat, delay_ms=delay_ms)
            return _ok(message_id, {"tx": raw.hex(" "), "len": len(raw)})
        except Exception as e:
            return _err(message_id, f"SC_SEND error: {e}")

    # SC_TEMPLATE: {"name":"START","repeat":1,"delayMs":0, "override":{"append":"<CR>"}}
    if cmd == "SC_TEMPLATE":
        if ser_cmd is None:
            return _err(message_id, "SOFTWARE_COMMAND chưa cấu hình (com_port=None)")
        name = (payload.get("name") or "").strip()
        tpl = config["devices"]["SOFTWARE_COMMAND"].get("templates", {}).get(name)
        if not tpl:
            return _err(message_id, f"Template '{name}' không tồn tại")
        override = payload.get("override") or {}
        append = override.get("append", config["devices"]["SOFTWARE_COMMAND"].get("default_append", ""))
        repeat = int(payload.get("repeat", 1))
        delay_ms = int(payload.get("delayMs", 0))
        try:
            tx = encode_ascii_with_tokens(tpl)
            if append:
                ap = encode_ascii_with_tokens(append)
                if not tx.endswith(ap):
                    tx = tx + ap
            send_raw_to_software_command(tx, repeat=repeat, delay_ms=delay_ms)
            return _ok(message_id, {"template": name, "tx": tx.hex(" "), "len": len(tx)})
        except Exception as e:
            return _err(message_id, f"SC_TEMPLATE error: {e}")

    # ==== Điều khiển BOARD_RELAY ====
    if cmd == "RELAY_WRITE":
        dev = config["devices"]["BOARD_RELAY"]
        slave = dev.get("slave_id", 1)
        items = payload.get("items", [])
        if not isinstance(items, list) or not items:
            return _err(message_id, "payload.items is required (non-empty)")
        responses = []
        for it in items:
            r = int(it.get("relay"))
            state_value = 1 if int(it.get("state", 1)) == 1 else 2
            res = control_single_relay(slave, r, state_value)
            responses.append({"relay": r, "command": "ON" if state_value==1 else "OFF", "ok": res.get("ok", False), "detail": res.get("detail") or res.get("error")})
        return _ok(message_id, {"responses": responses})

    if cmd == "RELAY_ALL":
        dev = config["devices"]["BOARD_RELAY"]
        slave = dev.get("slave_id", 1)
        state_value = 1 if int(payload.get("state", 1)) == 1 else 2
        responses = []
        for relay_num in range(1, 13):
            res = control_single_relay(slave, relay_num, state_value)
            responses.append({"relay": relay_num, "command": "ON" if state_value==1 else "OFF", "ok": res.get("ok", False)})
        return _ok(message_id, {"responses": responses})

    # ==== Log level ====
    if cmd == "SET_LOG_LEVEL":
        level = str(payload.get("level", "info")).lower()
        show_prompt = payload.get("showPrompt")
        if level not in LOG_LEVELS:
            return _err(message_id, f"Invalid level '{level}'")
        config.setdefault("logging", {})["level"] = level
        if show_prompt is not None:
            config["logging"]["show_prompt"] = bool(show_prompt)
        save_config(config)
        return _ok(message_id, {"level": level, "showPrompt": config["logging"]["show_prompt"]})

    # ==== Một số lệnh cũ / khung ví dụ ====
    if cmd == "BUILTIN_COMMAND":
        state = (payload.get("state") or "").strip()
        actions = config.get("builtin_map", {}).get(state)
        if not actions: return _err(message_id, f"Unknown builtin state '{state}'")
        for act in actions:
            if act.get("type") == "signal":
                name = act.get("name", ""); val = int(act.get("value",1)); pulse= int(act.get("pulse_ms",0))
                if pulse > 0: _pulse_signal(name, pulse)
                else: _emit_signal(name, val)
        return _ok(message_id, {"state": state})

    if cmd == "GET_READY_STATUS":
        try:
            _, summary = _rpc_read_once_snapshot()
            return _ok(message_id, {"isReady": bool(summary.get("states",{}).get("Ready",0))})
        except Exception as e:
            return _err(message_id, e)

    if cmd == "GET_POSITION":
        pos_cfg = config.get("app", {}).get("position", {})
        xi = int(pos_cfg.get("x_index", 0)); yi = int(pos_cfg.get("y_index", 1)); scale = float(pos_cfg.get("scale", 1.0))
        try:
            values, _ = _rpc_read_once_snapshot()
            x = (values[xi] if 0 <= xi < len(values) else 0) * scale
            y = (values[yi] if 0 <= yi < len(values) else 0) * scale
            return _ok(message_id, {"X": x, "Y": y})
        except Exception as e:
            return _err(message_id, e)

    # Unknown
    return _err(message_id, f"Unknown command '{cmd}'")

# -----------------------------
# ZeroMQ REP server
# -----------------------------
def zmq_rep_server(stop_event, device_config, cfg):
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REP)
    rep_bind = cfg.get("zeromq", {}).get("rep_bind", "tcp://*:5555")
    sock.RCVTIMEO = int(cfg.get("zeromq", {}).get("rcv_timeout_ms", 1000))
    sock.SNDTIMEO = int(cfg.get("zeromq", {}).get("snd_timeout_ms", 1000))
    sock.bind(rep_bind)
    log("info", f"[ZMQ] REP server bound at {rep_bind}")

    try:
        while not stop_event.is_set():
            raw = None
            try:
                raw = sock.recv()
            except zmq.Again:
                continue
            except Exception as e:
                log("error", f"[ZMQ] recv error: {e}")
                continue
            try:
                text = raw.decode("utf-8")
                cmd = json.loads(text)
                if isinstance(cmd, dict) and "command" not in cmd and any(k in cmd for k in ("read_once","commands","all","config","read")):
                    result_old = exec_command(cmd, device_config)
                    corr = cmd.get("messageId") or str(uuid.uuid4())
                    reply = _ok(corr, result_old)
                else:
                    reply = handle_envelope(cmd if isinstance(cmd, dict) else {})
                sock.send_string(json.dumps(reply, ensure_ascii=False))
                if isinstance(cmd, dict):
                    log("debug", f"RPC handled: {cmd.get('command') or 'legacy'}")
            except Exception as e:
                try:
                    parsed = json.loads(text) if raw else {}
                except Exception:
                    parsed = {}
                corr = parsed.get("messageId") if isinstance(parsed, dict) else str(uuid.uuid4())
                err = _err(corr or str(uuid.uuid4()), str(e))
                try: sock.send_string(json.dumps(err, ensure_ascii=False))
                except Exception: pass
                log("error", f"RPC error: {e}")
    finally:
        try: sock.close(0)
        except: pass

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    config = load_config()
    setup_com_ports(config)

    # BOARD_RELAY
    device_config = config["devices"]["BOARD_RELAY"]
    if not device_config.get("com_port"):
        print("BOARD_RELAY chưa có COM. Cấu hình lại rồi chạy tiếp.")
        exit(1)
    try:
        ser = serial.Serial(device_config["com_port"], device_config.get("baud_rate", 9600),
                            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=1)
        log("info", f"Kết nối tới {device_config['com_port']} (BOARD_RELAY) thành công.")
    except Exception as e:
        log("error", f"Lỗi mở cổng {device_config.get('com_port')}: {e}")
        exit(1)

    # SOFTWARE_COMMAND
    ser_cmd = open_serial_for("SOFTWARE_COMMAND", config)

    cmd_queue = Queue()
    last_values = {"values": None}
    error_count = {"count": 0}
    stop_event = threading.Event()

    # Reader + PUB
    read_thread = threading.Thread(target=background_read, args=(stop_event, last_values, error_count, device_config), daemon=True)
    read_thread.start()

    # Writer SOFTWARE_COMMAND
    if ser_cmd is not None:
        writer_thread = threading.Thread(target=software_command_writer, args=(stop_event, ser_cmd, cmd_queue, config), daemon=True)
        writer_thread.start()
    else:
        writer_thread = None

    # REP RPC
    zmq_thread = threading.Thread(target=zmq_rep_server, args=(stop_event, device_config, config), daemon=True)
    zmq_thread.start()

    if config.get("logging", {}).get("show_prompt", True):
        print("\nChương trình đang tự động đọc dữ liệu. Nhập lệnh để điều khiển.")
        print("Nhập ? để xem hướng dẫn (legacy console).")

    try:
        while True:
            try:
                if not config.get("logging", {}).get("show_prompt", True):
                    time.sleep(0.5)
                    continue
                command = input("\nNhập lệnh: ").strip()
                if command == "?":
                    print('Ví dụ legacy: {"read_once": true} hoặc {"commands":[{"relay":1,"state":1}]}'); continue
                if not command: continue
                process_command(json.loads(command), device_config)
            except KeyboardInterrupt:
                raise
            except json.JSONDecodeError:
                log_json("error", {"type":"error","message":"Định dạng JSON không hợp lệ"})
            except Exception as e:
                log_json("error", {"type":"error","message":str(e)})
    except KeyboardInterrupt:
        log("info", "Đang dừng chương trình...")
        stop_event.set()
        if read_thread.is_alive(): read_thread.join(timeout=1)
        if writer_thread and writer_thread.is_alive(): writer_thread.join(timeout=1)
    finally:
        try:
            if ser: ser.close()
        except: pass
        try:
            if ser_cmd: ser_cmd.close()
        except: pass
        log_json("info", {"type":"system","message":"Đã đóng cổng serial"})
