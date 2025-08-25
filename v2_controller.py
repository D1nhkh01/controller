# -*- coding: utf-8 -*-
"""
server.py — VM2030 + PLC + Board Relay + ZeroMQ
- UI commands: synchronous REP only when complete or timeout (no immediate ACK)
- Dry-run supported with terminal prints and simulated 0x1F
- Relay side-effects:
    on send:  R1 ON (1s pulse) + R2 ON
    on done:  R2 OFF + R3 ON
- PLC input edges (Home/Reset) trigger machine commands without Ready gating.
- GET_JOB: send %J{n}_B<CR>, collect ASCII until 0x1F, normalize to JobCncModel.
"""
import serial, serial.tools.list_ports
import time, json, os, threading, re, uuid
from queue import Queue
from datetime import datetime, timezone
import zmq
import random  

# -----------------------------
# Files & Globals
# -----------------------------
CONFIG_FILE     = "device_config.json"
JOB_STORE_FILE  = "job_store.json"

ser = None              # BOARD_RELAY (Modbus)
ser_cmd = None          # SOFTWARE_COMMAND -> VM2030
cmd_queue = None        # queue to writer
ser_lock = threading.Lock()     # lock cho BOARD_RELAY

# SC RX state
sc_rx_lock = threading.Lock()   # lock cho VM2030 RX cache/buffer
sc_rx_cv = threading.Condition(sc_rx_lock)
_last_status_code = {"code": None, "ts": None}  # last byte (0x1F on complete)
_sc_rx_buffer = bytearray()      # buffer thu ASCII cho các lệnh GET/UPLOAD

pub_socket = None       # global PUB socket

LOG_LEVELS = {"off":0, "error":1, "warn":2, "info":3, "debug":4}

# -----------------------------
# Small helpers
# -----------------------------
def _iso_now():
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")

def _ts_local():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _load_store():
    if os.path.exists(JOB_STORE_FILE):
        try:
            with open(JOB_STORE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {"jobs": {}, "sequences": {}}

def _save_store(store):
    with open(JOB_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)

def _log_enabled(level):
    try:
        cur = config.get("logging", {}).get("level","info").lower()
        return LOG_LEVELS.get(cur,3) >= LOG_LEVELS.get(level,3)
    except:
        return True

def log(level, msg):
    if not _log_enabled(level): return
    if config.get("logging", {}).get("console", True):
        prefix = f"[{level.upper()}]"
        if config.get("logging", {}).get("timestamps", True):
            prefix = f"[{_ts_local()}] {prefix}"
        print(f"{prefix} {msg}")

def log_json(level, obj):
    if _log_enabled(level) and config.get("logging", {}).get("console", True):
        print(json.dumps(obj, ensure_ascii=False))

def _ok(corr_id, message):
    return {"CorrelationId": corr_id, "IsError": False, "ErrorMessage": "", "Message": message}

def _err(corr_id, msg):
    return {"CorrelationId": corr_id, "IsError": True, "ErrorMessage": str(msg), "Message": {}}

def publish(topic: str, obj: dict):
    global pub_socket
    if pub_socket is None: return
    try:
        pub_socket.send_multipart([topic.encode("utf-8"), json.dumps(obj, ensure_ascii=False).encode("utf-8")])
    except Exception as e:
        log("error", f"[ZMQ] PUB send error: {e}")

# ---- Mongo-like ObjectId generator (24 hex) ----
def new_object_id() -> str:
    return f"{random.randrange(16**24):024x}"

def _ensure_job_id(store: dict, job_number: int) -> str:
    key = str(job_number)
    store.setdefault("jobs", {})
    job = store["jobs"].setdefault(key, {})
    if not job.get("Id"):
        job["Id"] = new_object_id()
    return job["Id"]

# -----------------------------
# ASCII/HEX tokens (VM2030)
# -----------------------------
_TOKEN_MAP = {
    "CR": b"\r", "LF": b"\n", "CRLF": b"\r\n", "TAB": b"\t", "ESC": b"\x1B",
    "STX": b"\x02", "ETX": b"\x03", "NUL": b"\x00", "SP": b" "
}

def encode_ascii_with_tokens(text: str) -> bytes:
    out = bytearray(); i = 0; L = len(text or "")
    while i < L:
        ch = text[i]
        if ch == "<":
            j = text.find(">", i+1)
            if j == -1:
                out.extend(ch.encode("latin1", errors="ignore")); i += 1; continue
            token = text[i+1:j].strip(); up = token.upper()
            if up in _TOKEN_MAP:
                out.extend(_TOKEN_MAP[up])
            elif re.fullmatch(r"0X[0-9A-F]{2}", up):
                out.append(int(up[2:], 16))
            elif re.fullmatch(r"D\d{1,3}", up):
                val = int(up[1:])
                if not (0 <= val <= 255): raise ValueError(f"dec token out of range: <{token}>")
                out.append(val)
            else:
                out.extend(("<"+token+">").encode("latin1", errors="ignore"))
            i = j + 1
        else:
            out.extend(ch.encode("latin1", errors="ignore")); i += 1
    return bytes(out)

def ensure_even_before_cr(payload: bytes) -> bytes:
    """
    Spec: nếu tổng byte gửi đến CR/CRLF là lẻ, chèn LF (0x0A) TRƯỚC CR để thành chẵn.
    Áp dụng khi chuỗi kết thúc bằng CR hoặc CRLF.
    """
    if not payload: return payload
    if payload.endswith(b"\r\n"):
        core = payload[:-2]
        if (len(core) + 2) % 2 == 1:
            return core + b"\n\r\n"[-2:]
        return payload
    if payload.endswith(b"\r"):
        core = payload[:-1]
        if (len(core) + 1) % 2 == 1:
            return core + b"\n\r"
        return payload
    return payload

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
                "com_port": None, "baud_rate": 9600,
                "protocol": "ascii",
                "xonxoff": True,
                "emit_options": { "debounce_ms": 100, "edge_only": False, "min_interval_ms": 0 },
                "default_append": "<CR>",
                "dry_run": True,
                "dry_run_complete_ms": 1000,
                "print_mode": "hex_ascii",
                "templates": {
                    "HOME": "%H<CR>",
                    "RESET": "<0x1D>"
                }
            }
        },
        "zeromq": {"rep_bind":"tcp://*:5555","rcv_timeout_ms":1000,"snd_timeout_ms":1000,"pub_bind":"tcp://*:5556","publish":True},
        "app": { "position": { "x_index": 0, "y_index": 1, "scale": 0.01 } },
        "logging": { "level": "info", "timestamps": True, "console": True, "show_prompt": False },
        "timeouts": {
            "sc_complete_ms": 5000,
            "ui_op_timeout_ms": 20000,
            "get_job_ms": 4000
        }
    }
    cfg={}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE,"r",encoding="utf-8") as f: cfg=json.load(f)
        except Exception as e:
            print(f"Lỗi đọc file cấu hình: {e}"); cfg={}
    def deep_merge(dst, src):
        for k,v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict): deep_merge(dst[k], v)
            else: dst[k] = v
        return dst
    return deep_merge(default_config.copy(), cfg)

def save_config(cfg):
    try:
        with open(CONFIG_FILE,"w",encoding="utf-8") as f: json.dump(cfg,f,indent=2,ensure_ascii=False)
        log("info","Đã lưu cấu hình")
    except Exception as e:
        log("error", f"Lỗi lưu cấu hình: {e}")

def setup_com_ports(cfg):
    def list_and_pick(device_key):
        nonlocal cfg
        while True:
            ports = get_available_ports()
            log("info", f"--- Cấu hình cổng cho {device_key} ---")
            saved = cfg["devices"][device_key].get("com_port")
            if saved and any(p["device"]==saved for p in ports):
                log("info", f"  Dùng lại cổng đã lưu: {saved}"); return
            if saved and not any(p["device"]==saved for p in ports):
                log("warn", f"  Cổng đã lưu ({saved}) không còn, cần chọn lại.")
            if not ports:
                print("  Không tìm thấy cổng serial nào. Cắm thiết bị rồi Enter để refresh.")
                input(); continue
            for i,p in enumerate(ports,1): print(f"   {i}. {p['device']} - {p['description']}")
            print("  Nhập số (1..n) để chọn, hoặc gõ tên (COM5). Enter = None, 'r' = refresh.")
            raw = input("  Chọn: ").strip()
            if raw=="": cfg["devices"][device_key]["com_port"]=None; return
            if raw.lower()=="r": continue
            if raw.isdigit():
                idx=int(raw)
                if 1<=idx<=len(ports):
                    cfg["devices"][device_key]["com_port"]=ports[idx-1]["device"]; return
                else: print("  Số không hợp lệ."); continue
            cfg["devices"][device_key]["com_port"]=raw; return

    list_and_pick("BOARD_RELAY")
    list_and_pick("SOFTWARE_COMMAND")
    br = cfg["devices"]["BOARD_RELAY"].get("com_port")
    sc = cfg["devices"]["SOFTWARE_COMMAND"].get("com_port")
    if br and sc and br == sc: log("warn","BOARD_RELAY và SOFTWARE_COMMAND đang dùng CÙNG MỘT cổng.")
    save_config(cfg)

def open_serial_for(device_key, cfg):
    dev = cfg["devices"].get(device_key, {})
    port = dev.get("com_port"); baud = int(dev.get("baud_rate",9600))
    if not port: return None
    try:
        if device_key=="SOFTWARE_COMMAND":
            s = serial.Serial(port, baud, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                              timeout=1, xonxoff=bool(dev.get("xonxoff", True)))
        else:
            s = serial.Serial(port, baud, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=1)
        log("info", f"[{device_key}] Kết nối tới {port} ({baud}bps) thành công.")
        return s
    except Exception as e:
        log("error", f"[{device_key}] Lỗi mở cổng {port}: {e}"); return None

# -----------------------------
# Modbus (BOARD_RELAY)
# -----------------------------
def calculate_crc(data):
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for _ in range(8):
            if crc & 0x0001: crc >>= 1; crc ^= 0xA001
            else: crc >>= 1
    return crc.to_bytes(2, byteorder="little")

def read_holding_registers(slave_id, start_addr, num_registers):
    global ser
    try:
        data = [slave_id, 0x03, start_addr>>8, start_addr&0xFF, num_registers>>8, num_registers&0xFF]
        data += list(calculate_crc(data))
        with ser_lock:
            ser.reset_input_buffer(); ser.write(bytearray(data)); time.sleep(0.1)
            expected = 3 + num_registers*2 + 2
            response = ser.read(expected)
        if len(response) != expected: return None
        if response[-2:] != calculate_crc(response[:-2]): return None
        vals=[]
        for i in range(3, len(response)-2, 2):
            vals.append((response[i]<<8)|response[i+1])
        return vals
    except: return None

def control_single_relay(slave_id, relay_addr, state_value, retries=2, tx_delay_s=0.02):
    """
    FC16 write single register: relay_addr 1..12, state_value: 1=ON, 2=OFF (theo board)
    """
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

def _relay_on(relay, on):
    dev = config["devices"]["BOARD_RELAY"]; slave = dev.get("slave_id",1)
    state = 1 if on else 2
    res = control_single_relay(slave, relay, state)
    if not res.get("ok"):
        log("warn", f"[RELAY] set {relay}={on} failed: {res.get('error')}")

def _relay_pulse(relay, pulse_ms):
    _relay_on(relay, True)
    threading.Timer(pulse_ms/1000.0, lambda: _relay_on(relay, False)).start()

def _relay_side_effects_on_send():
    # R1 pulse 1s, R2 ON
    _relay_pulse(1, 1000)
    _relay_on(2, True)

def _relay_side_effects_on_complete():
    # R2 OFF, R3 ON (giữ nguyên)
    _relay_on(2, False)
    _relay_on(3, True)

# -----------------------------
# VM2030 Command builders
# -----------------------------
def sc_build_home():
    return ensure_even_before_cr(encode_ascii_with_tokens("%H<CR>"))

def sc_build_reset():
    # RESET là 0x1D (một byte), không CR
    return encode_ascii_with_tokens("<0x1D>")

def sc_build_set_job(job_index:int, text:str):
    # %J{index}_[{text}]<CR>
    return ensure_even_before_cr(encode_ascii_with_tokens(f"%J{int(job_index)}_{text}<CR>"))

def sc_build_set_sequence(seq_index:int, cmd_string:str):
    # %S{index}_[{cmd_string}]<CR>
    return ensure_even_before_cr(encode_ascii_with_tokens(f"%S{int(seq_index)}_{cmd_string}<CR>"))

def sc_build_start_job(job_index:int):
    # %J{index}_N<CR>
    return ensure_even_before_cr(encode_ascii_with_tokens(f"%J{int(job_index)}_N<CR>"))

def sc_build_start_sequence(seq_index:int):
    # %S{index}_N<CR>
    return ensure_even_before_cr(encode_ascii_with_tokens(f"%S{int(seq_index)}_N<CR>"))

def sc_build_get_job_info(job_index:int):
    # %J{index}_B<CR>  (tải thông tin job cụ thể)
    return ensure_even_before_cr(encode_ascii_with_tokens(f"%J{int(job_index)}_B<CR>"))

# -----------------------------
# VM2030 writer/reader + dry-run / print
# -----------------------------
def _sc_dump_bytes(b: bytes):
    mode = (config["devices"]["SOFTWARE_COMMAND"].get("print_mode") or "hex_ascii").lower()
    if mode in ("hex","hex_ascii","ascii_hex"):
        hx = b.hex(" ").upper()
        if mode == "hex":
            log("info", f"[SC TX] {hx}")
        else:
            try:
                asc = "".join((chr(x) if 32<=x<=126 else ".") for x in b)
            except:
                asc = ""
            log("info", f"[SC TX] HEX: {hx} | ASCII: {asc}")
    elif mode == "ascii":
        try:
            asc = b.decode("latin1", errors="replace")
        except:
            asc = str(b)
        log("info", f"[SC TX] {asc}")
    else:
        log("info", f"[SC TX] {b.hex(' ').upper()}")

def send_raw_to_software_command(raw_bytes: bytes, repeat=1, delay_ms=0):
    global cmd_queue, ser_cmd, config
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise TypeError("raw_bytes phải là bytes")
    repeat = max(1, int(repeat)); delay_ms = max(0, int(delay_ms))
    dry_run = bool(config["devices"]["SOFTWARE_COMMAND"].get("dry_run", False))

    # In ra terminal luôn khi dry-run hoặc không có COM
    if dry_run or ser_cmd is None:
        for _ in range(repeat):
            _sc_dump_bytes(bytes(raw_bytes))
            if delay_ms > 0: time.sleep(delay_ms/1000.0)
        return

    for _ in range(repeat):
        cmd_queue.put(("raw_bytes", bytes(raw_bytes)))
        if delay_ms > 0: time.sleep(delay_ms/1000.0)

def software_command_writer(stop_event, ser_cmd_local, queue_local, cfg):
    if ser_cmd_local is None: return
    min_interval_ms = int(cfg["devices"]["SOFTWARE_COMMAND"].get("emit_options", {}).get("min_interval_ms", 0))
    last_emit_at = 0
    while not stop_event.is_set():
        try:
            item = queue_local.get(timeout=0.2)
        except:
            continue
        if min_interval_ms > 0:
            now = int(time.time()*1000)
            if now - last_emit_at < min_interval_ms:
                time.sleep((min_interval_ms - (now - last_emit_at))/1000.0)
        try:
            if isinstance(item, tuple) and item[0] == "raw_bytes":
                payload = item[1]
                ser_cmd_local.write(payload)
                last_emit_at = int(time.time()*1000)
                log("debug", f"[SC TX RAW] {payload.hex(' ')}")
        except Exception as e:
            log("error", f"Lỗi gửi SOFTWARE_COMMAND: {e}")

def software_command_reader(stop_event, ser_cmd_local, cfg):
    if ser_cmd_local is None: return
    ser_cmd_local.reset_input_buffer()
    while not stop_event.is_set():
        try:
            b = ser_cmd_local.read(1)
            if not b:
                time.sleep(0.01); continue
            code = b[0]
            with sc_rx_lock:
                if code == 0x1F:
                    _last_status_code["code"] = code
                    _last_status_code["ts"] = _ts_local()
                    sc_rx_cv.notify_all()
                else:
                    _sc_rx_buffer.extend(b)
        except Exception as e:
            log("error", f"[SC RX] read error: {e}")
            time.sleep(0.2)

def sc_schedule_dryrun_complete():
    ms = int(config["devices"]["SOFTWARE_COMMAND"].get("dry_run_complete_ms", 1000))
    def _complete():
        with sc_rx_lock:
            _last_status_code["code"] = 0x1F
            _last_status_code["ts"] = _ts_local()
            sc_rx_cv.notify_all()
        log("debug", "[SC DRYRUN] Simulated complete (0x1F)")
    threading.Timer(ms/1000.0, _complete).start()

def sc_clear_rx():
    with sc_rx_lock:
        _sc_rx_buffer.clear()
        _last_status_code["code"] = None
        _last_status_code["ts"] = _ts_local()

def sc_read_until_complete_collect(timeout_ms:int) -> bytes:
    """
    Thu toàn bộ byte từ reader thread cho đến khi nhận complete (0x1F) hoặc timeout.
    Trả về payload đã nhận (không gồm 0x1F). Yêu cầu *đã gọi sc_clear_rx()* trước khi gửi lệnh.
    """
    end = time.time() + (timeout_ms/1000.0)
    with sc_rx_lock:
        # nếu đã có complete trước đó thì trả luôn buffer
        if _last_status_code["code"] == 0x1F:
            data = bytes(_sc_rx_buffer)
            _sc_rx_buffer.clear()
            return data
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                data = bytes(_sc_rx_buffer)
                _sc_rx_buffer.clear()
                return data
            sc_rx_cv.wait(timeout=remaining)
            if _last_status_code["code"] == 0x1F:
                data = bytes(_sc_rx_buffer)
                _sc_rx_buffer.clear()
                return data

def sc_wait_complete(timeout_ms:int):
    end = time.time() + (timeout_ms/1000.0)
    last = None
    while time.time() < end:
        with sc_rx_lock:
            last = _last_status_code["code"]
        if last == 0x1F:
            return {"ok": True, "code": last}
        time.sleep(0.02)
    return {"ok": False, "code": last}

# -----------------------------
# PLC → soft_state summary
# -----------------------------
def make_state_summary(values, device_config, cfg, ts):
    # map: Ready=index0, Home=index1, Reset=index2
    idx_ready = 0; idx_home = 1; idx_reset = 2
    def get_val(idx): return 1 if (0 <= idx < len(values) and values[idx]) else 0
    return {"type":"soft_state","device":"SOFTWARE_COMMAND","source":"BOARD_RELAY",
            "address":device_config["read_settings"]["start_address"],
            "states":{"Ready":get_val(idx_ready),"Home":get_val(idx_home),"Reset":get_val(idx_reset)}, "ts":ts}

def _is_ready_now():
    dev = config["devices"]["BOARD_RELAY"]
    vals = read_holding_registers(dev.get("slave_id",1), dev["read_settings"]["start_address"], dev["read_settings"]["num_registers"])
    if vals is None: return False
    return 1 if (len(vals)>0 and vals[0]) else 0

# -----------------------------
# JobCncModel normalization
# -----------------------------
def _blank_job_model(n:int) -> dict:
    now = _iso_now()
    return {
        "Id": "",
        "CreatedAt": now,
        "LastRunAt": now,
        "JobNumber": int(n),
        "JobName": "",
        "CharacterString": "",
        "StartX": 0.0, "StartY": 0.0,
        "PitchX": 0.0, "PitchY": 0.0,
        "Size": 1.0,
        "Speed": 100,
        "Direction": 0,
        "Increment": None,
        "Calendar": None,
        "CircularMarking": None
    }

_keymap_num = {
    "X": "StartX", "Y": "StartY",
    "PX": "PitchX", "PITCHX": "PitchX",
    "PY": "PitchY", "PITCHY": "PitchY",
    "SIZE": "Size", "H": "Size", "HEIGHT": "Size",
    "SPEED": "Speed", "V": "Speed",
    "DIR": "Direction", "DIRECTION": "Direction"
}
_keymap_text = {"TEXT": "CharacterString", "STR": "CharacterString", "NAME": "JobName", "JOBNAME": "JobName"}

def parse_job_ascii_to_model(raw_ascii:str, job_index:int, from_store:dict|None) -> dict:
    m = _blank_job_model(job_index)
    # Ưu tiên đổ dữ liệu lưu trong store (nếu có)
    if from_store:
        for k in ("JobName","CharacterString","StartX","StartY","PitchX","PitchY","Size","Speed","Direction"):
            if k in from_store: m[k] = from_store[k]

    s = raw_ascii or ""
    # số dạng key=value / key:value
    for k, v in re.findall(r"([A-Za-z]+)\s*[:=]\s*([-+]?[\d.]+)", s):
        kk = k.strip().upper()
        if kk in _keymap_num:
            fld = _keymap_num[kk]
            if fld in ("Speed","Direction"):
                try: m[fld] = int(float(v))
                except: pass
            else:
                try: m[fld] = float(v)
                except: pass

    # chuỗi text trong "..."
    for k, v in re.findall(r"([A-Za-z]+)\s*[:=]\*?\"([^\"]*)\"", s):
        kk = k.strip().upper()
        if kk in _keymap_text:
            m[_keymap_text[kk]] = v

    # nếu còn trống, thử lấy trong [ ... ]
    if not m["CharacterString"]:
        br = re.search(r"\[([^\]]]{1,64})\]", s)
        if br: m["CharacterString"] = br.group(1)

    return m

# -----------------------------
# SC operation execution (sync for UI)
# -----------------------------
def exec_sc_operation(op_id:str, command:str, raw:bytes, source:str, meta:dict=None, wait=True):
    """
    Thực thi 1 lệnh SC + side-effects relay.
    - on send: R1 pulse 1s + R2 ON
    - on complete: R2 OFF + R3 ON
    - Publish 'op_result' cho giám sát
    - Trả về dict để REP dùng trả lời UI (completed or timeout)
    """
    meta = meta or {}
    _relay_side_effects_on_send()
    send_raw_to_software_command(raw)
    if config["devices"]["SOFTWARE_COMMAND"].get("dry_run", False):
        sc_schedule_dryrun_complete()

    if not wait:
        return {"ok": True, "code": None, "note": "queued"}

    tout = int(config.get("timeouts",{}).get("ui_op_timeout_ms", 20000))
    res = sc_wait_complete(tout)
    if res.get("ok"):
        _relay_side_effects_on_complete()
        publish("op_result", {
            "type": "op_result",
            "opId": op_id, "command": command, "ok": True,
            "code": res["code"], "timestamp": _iso_now(), "source": source, "meta": meta
        })
        return {"ok": True, "code": res["code"], "timeoutMs": tout}
    else:
        publish("op_result", {
            "type": "op_result",
            "opId": op_id, "command": command, "ok": False,
            "isTimeout": True, "timeoutMs": tout, "lastCode": res.get("code"),
            "timestamp": _iso_now(), "source": source, "meta": meta
        })
        return {"ok": False, "isTimeout": True, "lastCode": res.get("code"), "timeoutMs": tout}

def _ensure_sc_available_or_err(message_id):
    sc_cfg = config["devices"]["SOFTWARE_COMMAND"]
    if sc_cfg.get("dry_run", False):
        return None
    if ser_cmd is None:
        return _err(message_id, "SOFTWARE_COMMAND COM is not connected (dry_run=false)")
    return None

# -----------------------------
# Background reader (Relay) + Input edges
# -----------------------------
def background_read(stop_event, last_values, error_count, device_config):
    global pub_socket, config
    # Prepare PUB
    try:
        zmq_cfg = config.get("zeromq", {})
        if zmq_cfg.get("publish", True) and zmq_cfg.get("pub_bind"):
            ctx = zmq.Context.instance()
            psock = ctx.socket(zmq.PUB); psock.sndtimeo = 1000
            psock.bind(zmq_cfg["pub_bind"])
            log("info", f"[ZMQ] PUB bound at {zmq_cfg['pub_bind']}")
            time.sleep(0.3)
            # expose
            globals()["pub_socket"] = psock
        else:
            globals()["pub_socket"] = None
    except Exception as e:
        log("error", f"[ZMQ] PUB init error: {e}")
        globals()["pub_socket"] = None

    # Edge detection for Home/Reset
    last_emit_time = {}
    debounce_ms = int(config["devices"]["SOFTWARE_COMMAND"].get("emit_options", {}).get("debounce_ms", 100))
    idx_ready, idx_home, idx_reset = 0, 1, 2

    def handle_input_edge(name, new_val):
        # Only rising edges trigger operation (1)
        if new_val != 1: return
        # Fire SC command in a new thread (do not block reader)
        def _run():
            try:
                err = _ensure_sc_available_or_err(str(uuid.uuid4()))
                if err:
                    log("warn", f"[INPUT] {name} ignored: {err['ErrorMessage']}")
                    return
                raw = sc_build_home() if name=="Home" else sc_build_reset()
                # Use a fresh opId for input-originated ops
                op_id = str(uuid.uuid4())
                # Source = "input"
                exec_sc_operation(op_id, name.upper(), raw, "input", {"signal": name}, wait=True)
            except Exception as e:
                log("error", f"[INPUT] exec {name} error: {e}")
        threading.Thread(target=_run, daemon=True).start()

    try:
        while not stop_event.is_set():
            try:
                values = read_holding_registers(device_config.get("slave_id",1),
                                                device_config["read_settings"]["start_address"],
                                                device_config["read_settings"]["num_registers"])
                if values:
                    error_count["count"]=0
                    prev = last_values.get("values")
                    if values != prev:
                        ts = _ts_local()
                        response = {"type":"read_response","device":"BOARD_RELAY",
                                    "address":device_config["read_settings"]["start_address"],
                                    "values":values,"timestamp":ts}
                        summary  = make_state_summary(values, device_config, config, ts)
                        log_json("debug", response); log_json("info", summary)
                        publish("read_response", response)
                        publish("soft_state", summary)

                        # Input edges: Home/Reset from PLC (not UI)
                        if prev is not None:
                            # home
                            old_home = 1 if prev[idx_home] else 0
                            new_home = 1 if values[idx_home] else 0
                            if new_home != old_home and new_home == 1:
                                # debounce
                                now_ms = int(time.time()*1000)
                                if now_ms - last_emit_time.get("Home",0) >= debounce_ms:
                                    last_emit_time["Home"] = now_ms
                                    handle_input_edge("Home", 1)
                            # reset
                            old_reset = 1 if prev[idx_reset] else 0
                            new_reset = 1 if values[idx_reset] else 0
                            if new_reset != old_reset and new_reset == 1:
                                now_ms = int(time.time()*1000)
                                if now_ms - last_emit_time.get("Reset",0) >= debounce_ms:
                                    last_emit_time["Reset"] = now_ms
                                    handle_input_edge("Reset", 1)

                        last_values["values"]=values
                else:
                    error_count["count"] += 1
                    if error_count["count"] == 5:
                        log("warn","Không thể đọc dữ liệu. Kiểm tra kết nối.")
                time.sleep(device_config["read_settings"]["interval_ms"]/1000.0)
            except Exception as e:
                error_count["count"] += 1
                if error_count["count"] == 5:
                    log("error", f"Lỗi: {str(e)}")
                time.sleep(1)
    finally:
        sock = globals().get("pub_socket")
        if sock is not None:
            try: sock.close(0)
            except: pass
        globals()["pub_socket"] = None

# -----------------------------
# RPC (ZeroMQ REP)
# -----------------------------
def handle_envelope(envelope):
    store = _load_store()
    message_id = envelope.get("messageId") or str(uuid.uuid4())
    cmd = (envelope.get("command") or "").upper().strip()
    payload = envelope.get("payload") or {}

    # Health check
    if envelope.get("read") is True:
        return _ok(message_id, {"note":"background reader running"})

    # ----------------- BUILTIN (HOME / RESET) -----------------
    if cmd == "BUILTIN_COMMAND":
        state = (payload.get("state") or "").strip()
        err = _ensure_sc_available_or_err(message_id)
        if err: return err
        if state not in ("rt_home", "sw_reset"):
            return _err(message_id, f"Unknown builtin state '{state}'")

        try:
            raw = sc_build_home() if state == "rt_home" else sc_build_reset()
            result = exec_sc_operation(message_id, state.upper(), raw, "ui", {"state": state}, wait=True)
            if result.get("ok"):
                return _ok(message_id, {"state": state})
            else:
                return _err(message_id, f"Timeout {result.get('timeoutMs',0)} ms (lastCode={result.get('lastCode')})")
        except Exception as e:
            return _err(message_id, f"BUILTIN_COMMAND error: {e}")

    # ----------------- SET_JOB -----------------
    if cmd == "SET_JOB":
        idx = int(payload.get("JobNumber") or payload.get("index") or 1)
        text = str(payload.get("CharacterString") or payload.get("text") or "").strip()
        if not text: return _err(message_id, "CharacterString/text is required")
        if not _is_ready_now(): return _err(message_id, "NOT_READY")
        err = _ensure_sc_available_or_err(message_id)
        if err: return err
        try:
            # ensure Id ổn định
            job_id = _ensure_job_id(store, idx)
            # persist/cập nhật
            store["jobs"][str(idx)].update({
                "Id": job_id,
                "JobNumber": idx,
                "CharacterString": text,
                "JobName": payload.get("JobName",""),
                "CreatedAt": store["jobs"][str(idx)].get("CreatedAt", _iso_now()),
                "LastRunAt": _iso_now()
            })
            _save_store(store)

            raw = sc_build_set_job(idx, text)
            result = exec_sc_operation(message_id, "SET_JOB", raw, "ui", {"index": idx}, wait=True)
            if result.get("ok"):
                # trả tối thiểu, kèm Id
                return _ok(message_id, {"Id": job_id, "JobNumber": idx})
            else:
                return _err(message_id, f"Timeout {result.get('timeoutMs',0)} ms (lastCode={result.get('lastCode')})")
        except Exception as e:
            return _err(message_id, f"SET_JOB error: {e}")

    # ----------------- GET_JOB (%J{n}_B<CR>) -----------------
    if cmd == "GET_JOB":
        try:
            idx = int(payload.get("JobNumber") or payload.get("index") or 1)
            err = _ensure_sc_available_or_err(message_id)
            if err: return err

            dry_run = bool(config["devices"]["SOFTWARE_COMMAND"].get("dry_run", False))
            tout = int(config.get("timeouts",{}).get("get_job_ms", 4000))

            raw_cmd = sc_build_get_job_info(idx)

            if dry_run:
                # print + fake complete + dựng payload từ store
                send_raw_to_software_command(raw_cmd)
                sc_schedule_dryrun_complete()
                # dựng ASCII "best-effort" từ store
                from_store = store["jobs"].get(str(idx)) or {}
                fake_ascii = (
                    f'NAME="{from_store.get("JobName","")}",'
                    f'TEXT="{from_store.get("CharacterString","")}",'
                    f'X={from_store.get("StartX",0)},Y={from_store.get("StartY",0)},'
                    f'PX={from_store.get("PitchX",0)},PY={from_store.get("PitchY",0)},'
                    f'SIZE={from_store.get("Size",1)},SPEED={from_store.get("Speed",100)},'
                    f'DIR={from_store.get("Direction",0)}'
                )
                _ = sc_wait_complete(tout)
                model = parse_job_ascii_to_model(fake_ascii, idx, from_store)
                # ensure Id
                job_id = _ensure_job_id(store, idx)
                model["Id"] = job_id
                model["CreatedAt"] = from_store.get("CreatedAt", _iso_now())
                model["LastRunAt"] = _iso_now()
                # cache
                store["jobs"][str(idx)] = {**store["jobs"].get(str(idx), {}), **model}
                _save_store(store)
                return _ok(message_id, model)

            # Real COM: dọn RX, gửi, đọc đến 0x1F
            sc_clear_rx()
            send_raw_to_software_command(raw_cmd)
            payload_bytes = sc_read_until_complete_collect(tout)
            ascii_resp = ""
            try: ascii_resp = payload_bytes.decode("latin1","ignore")
            except: pass

            from_store = store["jobs"].get(str(idx)) or {}
            model = parse_job_ascii_to_model(ascii_resp, idx, from_store)
            # ensure Id & times
            job_id = _ensure_job_id(store, idx)
            model["Id"] = job_id
            model["CreatedAt"] = from_store.get("CreatedAt", _iso_now())
            model["LastRunAt"] = _iso_now()
            # cache lại
            store["jobs"][str(idx)] = {**from_store, **model}
            _save_store(store)
            return _ok(message_id, model)
        except Exception as e:
            return _err(message_id, f"GET_JOB error: {e}")

    # ----------------- SET_SEQUENCE -----------------
    if cmd == "SET_SEQUENCE":
        idx = int(payload.get("index", 1))
        cmdstr = str(payload.get("commandString","")).strip()
        if not cmdstr: return _err(message_id, "payload.commandString is required")
        if not _is_ready_now(): return _err(message_id, "NOT_READY")
        err = _ensure_sc_available_or_err(message_id)
        if err: return err
        try:
            store["sequences"][str(idx)] = {"index": idx, "commandString": cmdstr, "updatedAt": _iso_now()}
            _save_store(store)

            raw = sc_build_set_sequence(idx, cmdstr)   # %S{idx}_[{cmd}]<CR>
            result = exec_sc_operation(message_id, "SET_SEQUENCE", raw, "ui", {"index": idx}, wait=True)
            if result.get("ok"):
                return _ok(message_id, {"index": idx})
            else:
                return _err(message_id, f"Timeout {result.get('timeoutMs',0)} ms (lastCode={result.get('lastCode')})")
        except Exception as e:
            return _err(message_id, f"SET_SEQUENCE error: {e}")

    # ----------------- GET_SEQUENCE (not supported by VM2030) -----------------
    if cmd == "GET_SEQUENCE":
        return _err(message_id, "GET_SEQUENCE is not supported; use UPLOAD_ALL (code 83) to retrieve all.")

    # ----------------- START_SEQUENCE -----------------
    if cmd == "START_SEQUENCE":
        idx = int(payload.get("index", 1))
        if not _is_ready_now(): return _err(message_id, "NOT_READY")
        err = _ensure_sc_available_or_err(message_id)
        if err: return err
        try:
            raw = sc_build_start_sequence(idx)
            result = exec_sc_operation(message_id, "START_SEQUENCE", raw, "ui", {"index": idx}, wait=True)
            if result.get("ok"):
                return _ok(message_id, {"index": idx})
            else:
                return _err(message_id, f"Timeout {result.get('timeoutMs',0)} ms (lastCode={result.get('lastCode')})")
        except Exception as e:
            return _err(message_id, f"START_SEQUENCE error: {e}")

    # ----------------- START_JOB -----------------
    if cmd == "START_JOB":
        idx = int(payload.get("index", 1))
        if not _is_ready_now(): return _err(message_id, "NOT_READY")
        err = _ensure_sc_available_or_err(message_id)
        if err: return err
        try:
            raw = sc_build_start_job(idx)
            result = exec_sc_operation(message_id, "START_JOB", raw, "ui", {"index": idx}, wait=True)
            if result.get("ok"):
                return _ok(message_id, {"index": idx})
            else:
                return _err(message_id, f"Timeout {result.get('timeoutMs',0)} ms (lastCode={result.get('lastCode')})")
        except Exception as e:
            return _err(message_id, f"START_JOB error: {e}")

    # ----------------- READY / POSITION -----------------
    if cmd == "GET_READY_STATUS":
        try:
            dev = config["devices"]["BOARD_RELAY"]
            values = read_holding_registers(dev.get("slave_id",1), dev["read_settings"]["start_address"], dev["read_settings"]["num_registers"])
            if values is None: return _err(message_id, "Read timeout/CRC error")
            summary = make_state_summary(values, dev, config, _ts_local())
            return _ok(message_id, {"isReady": bool(summary.get("states",{}).get("Ready",0))})
        except Exception as e:
            return _err(message_id, e)

    if cmd == "GET_POSITION":
        pos_cfg = config.get("app", {}).get("position", {})
        xi = int(pos_cfg.get("x_index", 0)); yi = int(pos_cfg.get("y_index", 1)); scale = float(pos_cfg.get("scale", 1.0))
        try:
            dev = config["devices"]["BOARD_RELAY"]
            values = read_holding_registers(dev.get("slave_id",1), dev["read_settings"]["start_address"], dev["read_settings"]["num_registers"])
            if values is None: return _err(message_id, "Read timeout/CRC error")
            x = (values[xi] if 0 <= xi < len(values) else 0) * scale
            y = (values[yi] if 0 <= yi < len(values) else 0) * scale
            return _ok(message_id, {"X": x, "Y": y})
        except Exception as e:
            return _err(message_id, e)

    # ----------------- LOG LEVEL -----------------
    if cmd == "SET_LOG_LEVEL":
        level = str(payload.get("level","info")).lower()
        show_prompt = payload.get("showPrompt")
        if level not in LOG_LEVELS: return _err(message_id, f"Invalid level '{level}'")
        config.setdefault("logging", {})["level"] = level
        if show_prompt is not None: config["logging"]["show_prompt"] = bool(show_prompt)
        save_config(config)
        return _ok(message_id, {"level": level, "showPrompt": config["logging"]["show_prompt"]})

    return _err(message_id, f"Unknown command '{cmd}'")

def zmq_rep_server(stop_event, cfg):
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REP)
    rep_bind = cfg.get("zeromq",{}).get("rep_bind","tcp://*:5555")
    sock.RCVTIMEO = int(cfg.get("zeromq",{}).get("rcv_timeout_ms",1000))
    sock.SNDTIMEO = int(cfg.get("zeromq",{}).get("snd_timeout_ms",1000))
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
                log("error", f"[ZMQ] recv error: {e}"); continue
            try:
                cmd = json.loads(raw.decode("utf-8"))
                print (f"[ZMQ] REQ → {json.dumps(cmd, ensure_ascii=False)}")
                reply = handle_envelope(cmd if isinstance(cmd, dict) else {})
                sock.send_string(json.dumps(reply, ensure_ascii=False))
                if isinstance(cmd, dict): log("debug", f"RPC handled: {cmd.get('command') or 'unknown'}")
            except Exception as e:
                corr = str(uuid.uuid4())
                try: parsed = json.loads(raw.decode("utf-8")) if raw else {}
                except: parsed = {}
                if isinstance(parsed, dict) and parsed.get("messageId"): corr = parsed["messageId"]
                err = _err(corr, str(e))
                try: sock.send_string(json.dumps(err, ensure_ascii=False))
                except: pass
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

    # Open BOARD_RELAY
    dev = config["devices"]["BOARD_RELAY"]
    if not dev.get("com_port"):
        print("BOARD_RELAY chưa có COM. Cấu hình lại rồi chạy tiếp.")
        exit(1)
    try:
        ser = serial.Serial(dev["com_port"], dev.get("baud_rate",9600), parity=serial.PARITY_NONE,
                            stopbits=serial.STOPBITS_ONE, timeout=1)
        log("info", f"Kết nối tới {dev['com_port']} (BOARD_RELAY) thành công.")
    except Exception as e:
        log("error", f"Lỗi mở cổng {dev.get('com_port')}: {e}"); exit(1)

    # Open SOFTWARE_COMMAND (VM2030) — optional when dry_run=true
    sc_cfg = config["devices"]["SOFTWARE_COMMAND"]
    ser_cmd = open_serial_for("SOFTWARE_COMMAND", config) if sc_cfg.get("com_port") else None
    cmd_queue = Queue()

    stop_event = threading.Event()
    last_values = {"values": None}; error_count = {"count": 0}

    # Threads
    t_read_relay = threading.Thread(target=background_read, args=(stop_event,last_values,error_count,dev), daemon=True)
    t_read_relay.start()

    t_sc_writer = None
    t_sc_reader = None
    if ser_cmd is not None:
        t_sc_writer = threading.Thread(target=software_command_writer, args=(stop_event, ser_cmd, cmd_queue, config), daemon=True)
        t_sc_writer.start()
        t_sc_reader = threading.Thread(target=software_command_reader, args=(stop_event, ser_cmd, config), daemon=True)
        t_sc_reader.start()

    t_rep = threading.Thread(target=zmq_rep_server, args=(stop_event, config), daemon=True)
    t_rep.start()

    try:
        while True: time.sleep(0.5)
    except KeyboardInterrupt:
        log("info","Đang dừng chương trình...")
        stop_event.set()
        for t in [t_read_relay, t_sc_writer, t_sc_reader, t_rep]:
            if t and hasattr(t,"is_alive") and t.is_alive(): t.join(timeout=1)
    finally:
        try:
            if ser: ser.close()
            if ser_cmd: ser_cmd.close()
        except: pass
        log_json("info", {"type":"system","message":"Đã đóng cổng serial"})
