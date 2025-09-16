# -*- coding: utf-8 -*-
"""
server.py — VM2030 + PLC + Board Relay + ZeroMQ
- UI commands: synchronous REP only when complete or timeout (no immediate ACK)
- Dry-run supported with terminal prints and simulated 0x1F
- Relay side-effects:
    on send:  R1 ON (1s pulse) + R2 ON
    on done:  R2 OFF + R3 ON
- PLC input edges (Home/Reset) trigger machine commands without Ready gating.
- GET_JOB: send %J{n}_B<CR>, collect 2 segments (header + body), normalize to JobCncModel.
"""
import serial, serial.tools.list_ports
import time, json, os, threading, re, uuid, argparse
from queue import Queue
from datetime import datetime, timezone
import zmq
import random

# Seq logging integration
try:
    from logger_setup import setup_logging, get_logger, log_vm2030_command, log_relay_operation, log_zmq_request, log_serial_error
    _HAS_SEQ_LOGGER = True
except ImportError:
    _HAS_SEQ_LOGGER = False
    print("Warning: logger_setup not found. Seq logging disabled.")

# -----------------------------
# Files & Globals
# -----------------------------
CONFIG_FILE     = "device_config.json"
JOB_STORE_FILE  = "job_store.json"

ser = None              # BOARD_RELAY (Modbus)
ser_cmd = None          # SOFTWARE_COMMAND -> VM2030
cmd_queue = None        # queue to writer
ser_lock = threading.Lock()     # lock cho BOARD_RELAY

# Seq logging
seq_logger = None       # Seq logger instance

# Relay error tracking
_relay_errors = []      # Track relay errors during operations

# SC RX state
sc_rx_lock = threading.Lock()   # lock cho VM2030 RX cache/buffer
sc_rx_cv = threading.Condition(sc_rx_lock)
_last_status_code = {"code": None, "ts": None}  # last byte (0x1F on complete)
_sc_rx_buffer = bytearray()      # buffer thu ASCII cho các lệnh GET/UPLOAD

# Reader thread control for GET_JOB
_reader_thread_enabled = threading.Event()  # Controls if reader thread should process data
_reader_thread_enabled.set()  # Default: enabled

LOG_LEVELS = {"off":0, "error":1, "warn":2, "info":3, "debug":4}

DEFAULT_JOB_TAIL = [
    "0.1","0.0","0.0","<NUL>","<NUL>","<NUL>","0","0.0","0.0","0.0","0.0","0.0","0.0","N","1","\"\""
]

# -----------------------------
# Command Line Arguments
# -----------------------------
def parse_arguments():
    """Parse command line arguments for dry run modes"""
    parser = argparse.ArgumentParser(
        description="VM2030 Controller - Laser Marking Machine Controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python controller.py                    # Normal mode (use config.json settings)
  python controller.py --dry-run         # Both devices in dry run mode
  python controller.py --dry-run-relay   # Only BOARD_RELAY in dry run mode  
  python controller.py --dry-run-command # Only SOFTWARE_COMMAND in dry run mode
  python controller.py --dry-run --log-level debug # Dry run với debug logging
        """
    )
    
    parser.add_argument('--dry-run', 
                       action='store_true',
                       help='Enable dry run mode for both BOARD_RELAY and SOFTWARE_COMMAND')
    
    parser.add_argument('--dry-run-relay', 
                       action='store_true', 
                       help='Enable dry run mode for BOARD_RELAY only')
    
    parser.add_argument('--dry-run-command', 
                       action='store_true',
                       help='Enable dry run mode for SOFTWARE_COMMAND only')
    
    parser.add_argument('--log-level',
                       choices=['off', 'error', 'warn', 'info', 'debug'],
                       help='Set logging level (overrides config.json)')
    
    parser.add_argument('--seq-url',
                       help='Seq server URL (overrides environment variable)')
    
    parser.add_argument('--config',
                       default=CONFIG_FILE,
                       help=f'Config file path (default: {CONFIG_FILE})')
    
    return parser.parse_args()

def apply_cli_overrides(config, args):
    """Apply command line argument overrides to config"""
    # Dry run overrides
    if args.dry_run:
        print("CLI Override: Enabling dry run for both devices")
        config["devices"]["BOARD_RELAY"]["dry_run"] = True
        config["devices"]["SOFTWARE_COMMAND"]["dry_run"] = True
        
    if args.dry_run_relay:
        print("CLI Override: Enabling dry run for BOARD_RELAY")
        config["devices"]["BOARD_RELAY"]["dry_run"] = True
        
    if args.dry_run_command:
        print("CLI Override: Enabling dry run for SOFTWARE_COMMAND") 
        config["devices"]["SOFTWARE_COMMAND"]["dry_run"] = True
    
    # Log level override
    if args.log_level:
        print(f"CLI Override: Setting log level to {args.log_level}")
        config["logging"]["level"] = args.log_level
        
    # Seq URL override
    if args.seq_url:
        print(f"CLI Override: Setting Seq URL to {args.seq_url}")
        os.environ["SEQ_URL"] = args.seq_url
        
    return config

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

def log(level, msg, **extra):
    global seq_logger
    if not _log_enabled(level): return
    
    # Console logging (existing)
    if config.get("logging", {}).get("console", True):
        prefix = f"[{level.upper()}]"
        if config.get("logging", {}).get("timestamps", True):
            prefix = f"[{_ts_local()}] {prefix}"
        print(f"{prefix} {msg}")
    
    # Seq logging với structured data (chỉ khi seq_logging=True)
    if seq_logger and _HAS_SEQ_LOGGER and config.get("seq_logging", True):
        try:
            seq_extra = {
                "Signal": "vm2030_controller",
                "Application": "IndustrialController", 
                "Component": "VM2030Controller",
                "DeviceType": "VM2030LaserMarker",
                **extra
            }
            level_mapped = "warning" if level.lower() == "warn" else level.lower()
            log_func = getattr(seq_logger, level_mapped, seq_logger.info)
            log_func(msg, extra=seq_extra)
        except Exception as e:
            print(f"[SEQ ERROR] {e}")

def log_json(level, obj):
    global seq_logger
    if _log_enabled(level) and config.get("logging", {}).get("console", True):
        print(json.dumps(obj, ensure_ascii=False))
    
    # Seq logging cho JSON objects (chỉ khi seq_logging=True)
    if seq_logger and _HAS_SEQ_LOGGER and config.get("seq_logging", True):
        try:
            log_func = getattr(seq_logger, level.lower(), seq_logger.info)
            log_func("JSON data", extra={"JsonData": obj, "DataType": "JSON"})
        except Exception as e:
            print(f"[SEQ ERROR] {e}")

def _ok(corr_id, message):
    return {"CorrelationId": corr_id, "IsError": False, "ErrorMessage": "", "Message": message}

def _err(corr_id, msg):
    return {"CorrelationId": corr_id, "IsError": True, "ErrorMessage": str(msg), "Message": {}}

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
                # Sử dụng UTF-8 thay vì latin1 để hỗ trợ Unicode
                try:
                    out.extend(ch.encode("utf-8"))
                except UnicodeEncodeError:
                    out.extend(ch.encode("ascii", errors="replace"))
                i += 1; continue
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
                try:
                    out.extend(("<"+token+">").encode("utf-8"))
                except UnicodeEncodeError:
                    out.extend(("<"+token+">").encode("ascii", errors="replace"))
            i = j + 1
        else:
            # Sử dụng UTF-8 để hỗ trợ ký tự tiếng Việt
            try:
                out.extend(ch.encode("utf-8"))
            except UnicodeEncodeError:
                out.extend(ch.encode("ascii", errors="replace"))
            i += 1
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
            return core + b"\r\n"[-2:]
        return payload
    if payload.endswith(b"\r"):
        core = payload[:-1]
        if (len(core) + 1) % 2 == 1:
            return core + b"\r"
        return payload
    return payload

def _ascii_with_tokens(b: bytes) -> str:
    out = []
    for x in b:
        if x == 0x0D:
            out.append("<CR>")
        elif x == 0x0A:
            out.append("<LF>")
        elif x == 0x00:
            out.append("<NUL>")
        elif 32 <= x <= 126:
            out.append(chr(x))
        else:
            out.append(f"<0x{x:02X}>")
    return "".join(out)

def _sent_repr(raw: bytes) -> dict:
    # Dùng _ascii_with_tokens để format đúng các ký tự đặc biệt
    return {"ascii": _ascii_with_tokens(raw), "hex": raw.hex(" ").upper()}

# -----------------------------
# Serial & Config
# -----------------------------
def get_available_ports():
    ports = serial.tools.list_ports.comports()
    return [{"device": p.device, "description": p.description, "hwid": p.hwid} for p in ports]

def load_config(config_file_path=CONFIG_FILE):
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
        "zeromq": {"rep_bind":"tcp://*:5555","rcv_timeout_ms":1000,"snd_timeout_ms":1000},
        "app": { "position": { "x_index": 0, "y_index": 1, "scale": 0.01 } },
        "logging": { "level": "info", "timestamps": True, "console": True, "show_prompt": False },
        "timeouts": {
            "sc_complete_ms": 5000,
            "ui_op_timeout_ms": 20000,
            "get_job_ms": 4000
        }
    }
    cfg={}
    if os.path.exists(config_file_path):
        try:
            with open(config_file_path,"r",encoding="utf-8") as f: cfg=json.load(f)
        except Exception as e:
            print(f"Lỗi đọc file cấu hình {config_file_path}: {e}"); cfg={}
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

    # Kiểm tra dry_run cho từng device trước khi chọn port
    board_relay_dry_run = cfg["devices"]["BOARD_RELAY"].get("dry_run", False)
    software_command_dry_run = cfg["devices"]["SOFTWARE_COMMAND"].get("dry_run", False)
    
    if not board_relay_dry_run:
        list_and_pick("BOARD_RELAY")
    else:
        log("debug", "[BOARD_RELAY] Dry run mode enabled - no COM port needed")
        
    if not software_command_dry_run:
        list_and_pick("SOFTWARE_COMMAND")
    else:
        log("debug", "[SOFTWARE_COMMAND] Dry run mode enabled - no COM port needed")
    
    br = cfg["devices"]["BOARD_RELAY"].get("com_port")
    sc = cfg["devices"]["SOFTWARE_COMMAND"].get("com_port")
    if br and sc and br == sc: log("warn","BOARD_RELAY và SOFTWARE_COMMAND đang dùng CÙNG MỘT cổng.")
    save_config(cfg)

def open_serial_for(device_key, cfg):
    dev = cfg["devices"].get(device_key, {})
    
    # Kiểm tra dry_run trước
    if dev.get("dry_run", False):
        log("debug", f"[{device_key}] Dry run mode - no actual serial connection needed")
        return None
        
    port = dev.get("com_port"); baud = int(dev.get("baud_rate",9600))
    if not port: return None
    try:
        if device_key=="SOFTWARE_COMMAND":
            s = serial.Serial(port, baud, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                              timeout=1, xonxoff=bool(dev.get("xonxoff", True)))
        else:
            s = serial.Serial(port, baud, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=1)
        log("debug", f"[{device_key}] Kết nối tới {port} ({baud}bps) thành công.")
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
    
    # Dry run mode: return simulated values
    if ser is None:
        if config["devices"]["BOARD_RELAY"].get("dry_run", False):
            log("debug", f"[BOARD_RELAY DRY_RUN] read_holding_registers: addr={start_addr}, count={num_registers}")
            # Get dry_run_state from config
            dry_state = config["devices"]["BOARD_RELAY"].get("dry_run_state", {})
            ready = dry_state.get("ready", 0)
            home = dry_state.get("home", 0) 
            reset = dry_state.get("reset", 0)
            other_regs = dry_state.get("other_registers", [0] * 5)
            
            # Return simulated values: [Ready, Home, Reset, ...other_registers]
            simulated_values = [ready, home, reset] + other_regs[:num_registers-3]
            # Ensure we return exactly num_registers values
            while len(simulated_values) < num_registers:
                simulated_values.append(0)
            return simulated_values[:num_registers]
        else:
            return {"error": "connection_not_available", "message": "Serial connection is None"}
    
    try:
        data = [slave_id, 0x03, start_addr>>8, start_addr&0xFF, num_registers>>8, num_registers&0xFF]
        data += list(calculate_crc(data))
        with ser_lock:
            ser.reset_input_buffer(); ser.write(bytearray(data)); time.sleep(0.1)
            expected = 3 + num_registers*2 + 2
            response = ser.read(expected)
        if len(response) != expected: 
            return {"error": "invalid_response_length", "message": f"Expected {expected} bytes, got {len(response)}"}
        if response[-2:] != calculate_crc(response[:-2]): 
            return {"error": "crc_mismatch", "message": "Response CRC validation failed"}
        vals=[]
        for i in range(3, len(response)-2, 2):
            vals.append((response[i]<<8)|response[i+1])
        return vals
    except serial.SerialException as e:
        # USB disconnected, port not available, etc.
        error_msg = f"Serial communication error: {str(e)}"
        if seq_logger and _HAS_SEQ_LOGGER:
            log_serial_error(seq_logger, device="BOARD_RELAY", 
                           error_type="serial_exception", error_msg=str(e),
                           function="read_holding_registers", slave_id=slave_id)
        return {"error": "serial_exception", "message": error_msg}
    except serial.SerialTimeoutException as e:
        # Timeout - device not responding
        error_msg = f"Communication timeout: {str(e)}"
        if seq_logger and _HAS_SEQ_LOGGER:
            log_serial_error(seq_logger, device="BOARD_RELAY",
                           error_type="timeout", error_msg=str(e),
                           function="read_holding_registers", slave_id=slave_id)
        return {"error": "timeout", "message": error_msg}
    except OSError as e:
        # OS level error - port disappeared, permission denied, etc.
        error_msg = f"OS error: {str(e)}"
        if seq_logger and _HAS_SEQ_LOGGER:
            log_serial_error(seq_logger, device="BOARD_RELAY",
                           error_type="os_error", error_msg=str(e),
                           function="read_holding_registers", slave_id=slave_id)
        return {"error": "os_error", "message": error_msg}
    except Exception as e:
        # Other unexpected errors
        error_msg = f"Unexpected error: {str(e)}"
        if seq_logger and _HAS_SEQ_LOGGER:
            log_serial_error(seq_logger, device="BOARD_RELAY",
                           error_type="unknown", error_msg=str(e),
                           function="read_holding_registers", slave_id=slave_id)
        return {"error": "unknown", "message": error_msg}

def control_single_relay(slave_id, relay_addr, state_value, retries=2, tx_delay_s=0.02):
    """
    FC16 write single register: relay_addr 1..12, state_value: 1=ON, 2=OFF (theo board)
    """
    global ser
    if not (1 <= relay_addr <= 12):
        return {"ok": False, "error": "Địa chỉ relay phải 1..12"}
    
    # Dry run mode
    if ser is None and config["devices"]["BOARD_RELAY"].get("dry_run", False):
        state_name = "ON" if state_value == 1 else "OFF" if state_value == 2 else f"CODE_{state_value}"
        log("debug", f"[BOARD_RELAY DRY_RUN] Relay {relay_addr} -> {state_name}")
        
        # Log đến Seq
        if seq_logger and _HAS_SEQ_LOGGER:
            log_relay_operation(seq_logger, relay_id=relay_addr, state=state_name, 
                               dry_run=True, slave_id=slave_id)
        
        return {"ok": True, "dry_run": True}
    
    if ser is None:
        return {"ok": False, "error": "Serial connection not available"}
        
    # Log relay operation đến Seq
    if seq_logger and _HAS_SEQ_LOGGER:
        state_name = "ON" if state_value == 1 else "OFF" if state_value == 2 else f"CODE_{state_value}"
        log_relay_operation(seq_logger, relay_id=relay_addr, state=state_name,
                           slave_id=slave_id, retries=retries, dry_run=False)
        
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
        
def control_multi_relays(slave_id, start_relay_addr, state_codes, retries=2, tx_delay_s=0.02):
    """
    Ghi nhiều thanh ghi liên tiếp bằng FC=0x10.
    - start_relay_addr: số kênh bắt đầu (1..12). Ví dụ 2 -> ghi kênh 2,3,...
    - state_codes: list các mã lệnh theo chuẩn board: 1=OPEN, 2=CLOSE, 3=TOGGLE, 4=LATCH, 5=MOMENTARY
      (Mỗi thanh ghi sẽ gửi [code, 0x00])
    """
    global ser
    if not (1 <= start_relay_addr <= 12):
        return {"ok": False, "error": "Địa chỉ bắt đầu phải 1..12"}
    qty = len(state_codes)
    if qty < 1 or (start_relay_addr + qty - 1) > 12:
        return {"ok": False, "error": "Số lượng vượt quá 12 kênh"}

    # Dry run mode
    if ser is None and config["devices"]["BOARD_RELAY"].get("dry_run", False):
        relay_info = []
        for i, code in enumerate(state_codes):
            relay_num = start_relay_addr + i
            state_name = {1:"OPEN", 2:"CLOSE", 3:"TOGGLE", 4:"LATCH", 5:"MOMENTARY"}.get(code, f"CODE_{code}")
            relay_info.append(f"R{relay_num}={state_name}")
        log("debug", f"[BOARD_RELAY DRY_RUN] Multi-relay: {', '.join(relay_info)}")
        return {"ok": True, "dry_run": True}
    
    if ser is None:
        return {"ok": False, "error": "Serial connection not available"}

    # Khung: [slave, 0x10, addr_hi, addr_lo, qty_hi, qty_lo, byte_count, data..., CRC(lo,hi)]
    frame = [slave_id, 0x10, 0x00, start_relay_addr, 0x00, qty, qty * 2]
    for code in state_codes:
        code = int(code)
        if code not in (1,2,3,4,5):
            return {"ok": False, "error": f"Mã lệnh không hợp lệ: {code}"}
        frame += [code, 0x00]  # theo tài liệu: lệnh nằm byte cao, byte thấp = 0x00

    frame += list(calculate_crc(frame))

    for attempt in range(retries + 1):
        try:
            with ser_lock:
                ser.reset_input_buffer()
                ser.write(bytearray(frame))
                time.sleep(tx_delay_s)
                # Echo FC16 chuẩn = 8 byte: slave, 0x10, addr_hi, addr_lo, qty_hi, qty_lo, CRC(lo,hi)
                resp = ser.read(8)
                if len(resp) == 0:
                    resp = ser.read(8)

            if len(resp) == 8:
                if resp[-2:] != calculate_crc(resp[:-2]):
                    if attempt < retries:
                        continue
                    return {"ok": False, "error": "CRC sai (FC16 echo)", "detail": {"raw": resp.hex()}}

                ok = (resp[0] == slave_id and resp[1] == 0x10 and
                      resp[2] == 0x00 and resp[3] == start_relay_addr and
                      resp[4] == 0x00 and resp[5] == qty)
                if ok:
                    return {"ok": True, "detail": {
                        "slave": resp[0],
                        "function": resp[1],
                        "start_address": (resp[2] << 8) | resp[3],
                        "quantity": (resp[4] << 8) | resp[5]
                    }}
                else:
                    return {"ok": False, "error": "Echo không khớp", "detail": {"raw": resp.hex()}}

            if attempt < retries:
                continue
            return {"ok": False, "error": f"Độ dài phản hồi bất thường ({len(resp)} bytes)", "detail": {"raw": resp.hex() if resp else ""}}
        except Exception as e:
            if attempt < retries:
                continue
            return {"ok": False, "error": f"Lỗi khi ghi nhiều relay: {e}"}   

def relay_r2_off_r3_on_simultaneous():
    dev = config["devices"]["BOARD_RELAY"]
    slave = dev.get("slave_id", 1)
    # bắt đầu tại kênh 2, ghi 2 kênh: [2=CLOSE, 1=OPEN] -> tắt R2, bật R3
    return control_multi_relays(slave, 2, [2, 1])

def _relay_on(relay, on):
    dev = config["devices"]["BOARD_RELAY"]; slave = dev.get("slave_id",1)
    state = 1 if on else 2
    res = control_single_relay(slave, relay, state)
    if not res.get("ok"):
        log("warn", f"[RELAY] set {relay}={on} failed: {res.get('error')}")
    return res

def _relay_pulse(relay, pulse_ms):
    _relay_on(relay, True)
    threading.Timer(pulse_ms/1000.0, lambda: _relay_on(relay, False)).start()

def _relay_side_effects_on_send():
    """
    (MỚI) Khi bắt đầu gửi lệnh xuống máy:
      - BẬT DOING (R2) và giữ ON cho đến khi có kết quả.
    """
    global _relay_errors
    _relay_errors.clear()  # Clear previous errors
    
    result = _relay_on(2, True)   # R2 = DOING ON
    if not result.get("ok", True):
        _relay_errors.append(f"Failed to turn on DOING relay: {result.get('error', 'Unknown error')}")

def _relay_side_effects_on_complete():
    """
    Hoàn thành: tắt DOING (R2) + bật FINISH (R3) ngay trong 1 khung,
    sau đó 1s thì tắt R3.
    """
    global _relay_errors
    dev = config["devices"]["BOARD_RELAY"]
    slave = dev.get("slave_id", 1)

    # 1 khung FC16: R2=OFF (2), R3=ON (1)
    res = control_multi_relays(slave, 2, [2, 1])
    if not res.get("ok"):
        error_msg = f"R2 OFF + R3 ON (simul) failed: {res.get('error')}"
        log("warn", f"[RELAY] {error_msg}")
        _relay_errors.append(error_msg)

    # Hẹn giờ 1s rồi tắt R3
    def _off_r3():
        r = control_single_relay(slave, 3, 2)  # 2 = OFF/CLOSE
        if not r.get("ok"):
            error_msg = f"R3 OFF after pulse failed: {r.get('error')}"
            log("warn", f"[RELAY] {error_msg}")
            _relay_errors.append(error_msg)
    threading.Timer(1.0, _off_r3).start()

# -----------------------------
# VM2030 Command builders
# -----------------------------
def sc_build_home():
    return ensure_even_before_cr(encode_ascii_with_tokens("%H<CR>"))

def sc_build_reset():
    # RESET là 0x1D (một byte), không CR
    return encode_ascii_with_tokens("<0x1D>")

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
    # %J{index}_B<CR>  (tải thông tin job cụ thể, trả 2 segment)
    return ensure_even_before_cr(encode_ascii_with_tokens(f"%J{int(job_index)}_B<CR>"))

def sc_build_toggle_echo(echo_enabled:bool):
    # %E_{0|1}<CR> - 0: Tắt echo, 1: Bật echo
    echo_param = "1" if echo_enabled else "0"
    return ensure_even_before_cr(encode_ascii_with_tokens(f"%E_{echo_param}<CR>"))

# Build move axis command for VM2030 (moved here from top)
def build_move_axis_command(axis: str, value: float) -> bytes:
    """
    Xây dựng lệnh di chuyển trục X hoặc Y cho VM2030:
    - axis: 'X' hoặc 'Y'
    - value: float, mm (X: -80.0~+80.0, Y: -30.0~+30.0)
    """
    axis = axis.upper()
    if axis not in ("X", "Y"):
        raise ValueError("Axis must be 'X' or 'Y'")
    if axis == "X" and not (-80.0 <= value <= 80.0):
        raise ValueError("X value out of range (-80.0~80.0)")
    if axis == "Y" and not (-30.0 <= value <= 30.0):
        raise ValueError("Y value out of range (-30.0~30.0)")
    # Format: %P_X{value}<CR> hoặc %P_Y{value}<CR>
    cmd = f"%P_{axis}{value:.1f}<CR>"
    return ensure_even_before_cr(encode_ascii_with_tokens(cmd))

# ----- set-job body builder (round-trip) -----
def _fmt1(v) -> str:
    try:
        return f"{float(v):.1f}"
    except:
        return str(v)

def build_vm2030_set_job_command(job_number: int, payload: dict, cached_tail: list[str] = None) -> bytes:
    """
    Tạo lệnh SET_JOB chuẩn VM2030 theo đúng cấu trúc:
    %J{job}_size_spare_speed_startX_startY_spacingX_spacingY_pitch_arcX_arcY_incFlag_arcFlag_calFlag_incDigits_p1x_p1y_p2x_p2y_p3x_p3y_zeroPad_orientation_"text"suffix<CR>
    
    Example: %J020_2.3_0_500_33.5_10.0_2.2_0.0_0.1_0.0_0.0_{00}_{00}_{00}_1_0.0_0.0_0.0_0.0_0.0_0.0_N_1_"ABC"
    """
    # Format số với 1 chữ số thập phân
    def fmt_float(val):
        return f"{float(val):.1f}"
    
    # Clean character string - bỏ underscore và normalize spaces
    character = re.sub(r"\s+", " ", str(payload.get("CharacterString", ""))).strip().replace("_", " ")
    
    # Thay thế flag thành <NUL> cho format ASCII
    def process_flag(flag):
        if flag == "<NUL>" or flag is None:
            return "<NUL>"
        return str(flag)
    
    # Sử dụng tail từ cache hoặc default (16 thông số mở rộng)
    tail = list(cached_tail) if cached_tail and len(cached_tail) > 0 else list(DEFAULT_JOB_TAIL)
    if len(tail) == 0:
        tail = list(DEFAULT_JOB_TAIL)
    
    # Xây dựng các thành phần theo đúng thứ tự VM2030
    params = [
        fmt_float(payload.get("Size", 1.0)),                    # Character size
        str(int(float(payload.get("Direction", 0)))),          # Spare  
        str(int(float(payload.get("Speed", 100)))),            # Marking speed
        fmt_float(payload.get("StartX", 0.0)),                 # Marking start point X
        fmt_float(payload.get("StartY", 0.0)),                 # Marking start point Y  
        fmt_float(payload.get("PitchX", 0.0)),                 # Character spacing X
        fmt_float(payload.get("PitchY", 0.0)),                 # Character spacing Y
    ]
    
    # Thêm 16 tail parameters từ DEFAULT_JOB_TAIL (bỏ phần tử cuối là "")
    params.extend(tail[:-1])
    
    # Character string ở cuối với format text"" (không có quotes đầu)
    character_suffix = f'{character}""'
    params.append(character_suffix)
    
    # Tạo command string
    body = "_".join(params)
    command = f"%J{job_number:03d}_{body}<CR>"
    
    return ensure_even_before_cr(encode_ascii_with_tokens(command))

# -----------------------------
# VM2030 writer/reader + dry-run / print
# -----------------------------
def _sc_dump_bytes(b: bytes):
    mode = (config["devices"]["SOFTWARE_COMMAND"].get("print_mode") or "hex_ascii").lower()
    if mode in ("hex","hex_ascii","ascii_hex"):
        hx = b.hex(" ").upper()
        if mode == "hex":
            log("warn", f"[SC TX] {hx}",
                OperationType="SerialTX", Device="SOFTWARE_COMMAND", 
                HexData=hx, DataLength=len(b))
        else:
            # Dùng format giống _sent_repr để consistency
            asc = ""
            for x in b:
                if 32 <= x <= 126:
                    asc += chr(x)
                elif x == 0x0D:
                    asc += "<CR>"
                elif x == 0x0A:
                    asc += "<LF>"
                elif x == 0x00:
                    asc += "<NUL>"
                else:
                    asc += f"<0x{x:02X}>"
                    
            log("warn", f"[SC TX] HEX: {hx} | ASCII: {asc}",
                OperationType="SerialTX", Device="SOFTWARE_COMMAND",
                HexData=hx, ASCIIData=asc, DataLength=len(b))
    elif mode == "ascii":
        try:
            asc = b.decode("latin1", errors="replace")
        except:
            asc = str(b)
        log("info", f"[SC TX] {asc}",
            OperationType="SerialTX", Device="SOFTWARE_COMMAND",
            ASCIIData=asc, DataLength=len(b))
    else:
        log("info", f"[SC TX] {b.hex(' ').upper()}",
            OperationType="SerialTX", Device="SOFTWARE_COMMAND",
            HexData=b.hex(' ').upper(), DataLength=len(b))

def send_raw_to_software_command(raw_bytes: bytes, repeat=1, delay_ms=0):
    global cmd_queue, ser_cmd, config
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise TypeError("raw_bytes phải là bytes")
    repeat = max(1, int(repeat)); delay_ms = max(0, int(delay_ms))
    dry_run = bool(config["devices"]["SOFTWARE_COMMAND"].get("dry_run", False))

    # Luôn in ra lệnh gửi xuống máy khắc (ascii/hex)
    _sc_dump_bytes(bytes(raw_bytes))

    # In ra terminal luôn khi dry-run hoặc không có COM
    if dry_run or ser_cmd is None:
        for _ in range(repeat):
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
            # Chỉ xử lý dữ liệu khi reader thread được enable
            if not _reader_thread_enabled.is_set():
                time.sleep(0.01)
                continue
                
            b = ser_cmd_local.read(1)
            if not b:
                time.sleep(0.01); continue
            code = b[0]
            completion_codes = [0x1F, 0x87]  # 0x1F for normal, 0x87 for RESET
            is_completion = code in completion_codes
            code_desc = f"0x{code:02X} {'completion' if is_completion else 'data'}"
            if is_completion:
                code_desc += f" ({hex(code)} = {'normal completion' if code == 0x1F else 'reset completion'})"
            print(f"[SC RX] Received byte: {code_desc}")
            with sc_rx_lock:
                if is_completion:
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

def sc_wait_complete(timeout_ms:int, expected_codes=None):
    """
    Chờ completion signal từ VM2030. 
    expected_codes: list of completion codes to wait for. Default: [0x1F, 0x87]
    """
    if expected_codes is None:
        expected_codes = [0x1F, 0x87]  # 0x1F for normal commands, 0x87 for RESET
    
    print(f"[sc_wait_complete] Waiting for completion codes {[hex(c) for c in expected_codes]}, timeout={timeout_ms}ms...")
    end = time.time() + (timeout_ms/1000.0)
    last = None
    check_count = 0
    while time.time() < end:
        with sc_rx_lock:
            last = _last_status_code["code"]
        check_count += 1
        if check_count % 100 == 0:  # Log every 2 seconds (100 * 0.02s)
            print(f"[sc_wait_complete] Check #{check_count}, current code: {hex(last) if last else None}")
        if last in expected_codes:
            print(f"[sc_wait_complete] SUCCESS: Received {hex(last)} after {check_count} checks")
            return {"ok": True, "code": last}
        time.sleep(0.02)
    print(f"[sc_wait_complete] TIMEOUT: No completion code received, lastCode={hex(last) if last else None}")
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

def _parse_float(s, default=0.0):
    try:
        return float(str(s).strip())
    except:
        return default

def _parse_int(s, default=0):
    try:
        return int(float(str(s).strip()))
    except:
        return default

def _normalize_spaces(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()

def _extract_job_no_from_header(header_bytes: bytes, fallback: int | None) -> int:
    try:
        # Thử UTF-8 trước, nếu không được thì dùng latin1
        s = header_bytes.decode("utf-8", "replace")
    except UnicodeDecodeError:
        try:
            s = header_bytes.decode("latin1", "ignore")
        except:
            s = ""
    m = re.search(r"%J\s*(\d+)\s*_B", s, flags=re.IGNORECASE)
    return int(m.group(1)) if m else int(fallback or 1)

def sc_read_two_segments_for_get_job(total_timeout_ms: int) -> tuple[bytes, bytes]:
    """
    GET_JOB trả 2 lần 0x1F: (1) header '%J{n}_B\\r' (2) body 'J 15_  2.0_0_  2400_ ... ""'
    Đọc nối tiếp 2 segment.
    """
    t1 = max(200, int(total_timeout_ms * 0.4))
    t2 = max(200, int(total_timeout_ms * 0.6))
    seg1 = sc_read_until_complete_collect(t1) or b""
    seg2 = sc_read_until_complete_collect(t2) or b""
    return seg1, seg2

def parse_vm2030_job_body(body_bytes: bytes, job_no: int) -> tuple[dict, list[str]]:
    """
    body_bytes: 'J 15_  2.0_0_  2400_   0.0_   0.0_   0.0_   0.0_  ... _""'
    Trả về (model, tail_tokens). Tail là các token sau 8 trường chính, gồm cả token tên job (cuối).
    """
    # Thử decode UTF-8 trước, nếu không được thì dùng latin1
    try:
        s = body_bytes.decode("utf-8", "replace").replace("\r", "").strip()
    except UnicodeDecodeError:
        s = body_bytes.decode("latin1", "ignore").replace("\r", "").strip()
    
    parts = [p.strip() for p in s.split("_") if p is not None]

    def get(idx, default=""):
        return parts[idx] if 0 <= idx < len(parts) else default

    character = _normalize_spaces(get(0, ""))
    size      = _parse_float(get(1, 1.0), 1.0)
    direction = _parse_int(get(2, 0), 0)
    speed     = _parse_int(get(3, 100), 100)
    start_x   = _parse_float(get(4, 0.0), 0.0)
    start_y   = _parse_float(get(5, 0.0), 0.0)
    pitch_x   = _parse_float(get(6, 0.0), 0.0)
    pitch_y   = _parse_float(get(7, 0.0), 0.0)

    job_name_raw = parts[-1].strip() if parts else ""
    if job_name_raw.startswith('"') and job_name_raw.endswith('"'):
        job_name = job_name_raw[1:-1]
    else:
        job_name = job_name_raw

    model = _blank_job_model(job_no)
    model.update({
        "JobName": job_name,
        "CharacterString": character,
        "StartX": start_x,
        "StartY": start_y,
        "PitchX": pitch_x,
        "PitchY": pitch_y,
        "Size": size,
        "Speed": speed,
        "Direction": direction
    })

    tail = parts[8:] if len(parts) > 8 else []
    return model, tail

# -----------------------------
# SC operation execution (sync for UI)
# -----------------------------
def exec_sc_operation(op_id:str, command:str, raw:bytes, source:str, meta:dict=None, wait=True):
    meta = meta or {}

    # Log VM2030 command đến Seq (chỉ khi seq_logging=True)
    if seq_logger and _HAS_SEQ_LOGGER and config.get("seq_logging", True):
        log_vm2030_command(seq_logger, 
                          command=command,
                          job_number=meta.get("job_number"),
                          operation_id=op_id,
                          source=source,
                          data_length=len(raw),
                          wait_for_complete=wait)

    # GIỮ NGUYÊN VỊ TRÍ GỌI
    _relay_side_effects_on_send()
    
    # CLEAR status code trước khi gửi lệnh mới
    with sc_rx_lock:
        _last_status_code["code"] = None
        _last_status_code["ts"] = _ts_local()
    
    send_raw_to_software_command(raw)

    if config["devices"]["SOFTWARE_COMMAND"].get("dry_run", False):
        sc_schedule_dryrun_complete()

    if not wait:
        return {"ok": True, "code": None, "note": "queued"}

    tout = int(config.get("timeouts",{}).get("ui_op_timeout_ms", 20000))
    res = sc_wait_complete(tout)

    if res.get("ok"):
        # GIỮ NGUYÊN VỊ TRÍ GỌI
        _relay_side_effects_on_complete()
        
        # Check for relay errors and include in response
        global _relay_errors
        has_relay_errors = len(_relay_errors) > 0
        
        # No more publishing - only REP responses
        
        # Return success for VM2030 but include relay error info
        result = {"ok": True, "code": res["code"], "timeoutMs": tout}
        if has_relay_errors:
            result["relay_errors"] = _relay_errors.copy()
            result["has_relay_errors"] = True
        
        return result
    else:
        # >>> THÊM DÒNG NÀY: timeout thì tắt DOING, KHÔNG bật alarm nào
        _relay_on(2, False)  # R2 = DOING OFF

        # No more publishing - timeout handled in response
        return {"ok": False, "isTimeout": True, "lastCode": res.get("code"), "timeoutMs": tout}

def _ensure_sc_available_or_err(message_id):
    sc_cfg = config["devices"]["SOFTWARE_COMMAND"]
    if sc_cfg.get("dry_run", False):
        return None
    if ser_cmd is None:
        return _err(message_id, "SOFTWARE_COMMAND COM is not connected (dry_run=false)")
    return None

# -----------------------------
def attempt_reconnect_relay():
    """
    Thử kết nối lại với BOARD_RELAY khi phát hiện lỗi kết nối
    """
    global ser, config
    
    try:
        if ser is not None:
            try:
                ser.close()
            except:
                pass
            ser = None
            
        device_config = config["devices"]["BOARD_RELAY"]
        port = device_config.get("com_port")
        
        if not port:
            log("warn", "[BOARD_RELAY] Không có cổng COM để kết nối lại")
            return False
            
        # Check if port still exists
        available_ports = [p.device for p in serial.tools.list_ports.comports()]
        if port not in available_ports:
            log("warn", f"[BOARD_RELAY] Cổng {port} không còn tồn tại")
            return False
            
        # Try to reconnect
        new_ser = serial.Serial(
            port=port,
            baudrate=device_config.get("baud_rate", 9600),
            timeout=1,
            write_timeout=1
        )
        
        ser = new_ser
        log("info", f"[BOARD_RELAY] Kết nối lại thành công với {port}")
        return True
        
    except Exception as e:
        log("error", f"[BOARD_RELAY] Lỗi khi kết nối lại: {str(e)}")
        return False

# Background reader (Relay) + Input edges
# -----------------------------
def background_read(stop_event, last_values, error_count, device_config):
    global config
    
    # Edge detection for Home/Reset
    last_emit_time = {}
    debounce_ms = int(config["devices"]["SOFTWARE_COMMAND"].get("emit_options", {}).get("debounce_ms", 100))
    idx_ready, idx_home, idx_reset = 0, 1, 2

    def handle_input_edge(name, new_val):
        if new_val != 1: return
        def _run():
            try:
                err = _ensure_sc_available_or_err(str(uuid.uuid4()))
                if err:
                    log("warn", f"[INPUT] {name} ignored: {err['ErrorMessage']}")
                    return
                raw = sc_build_home() if name=="Home" else sc_build_reset()
                op_id = str(uuid.uuid4())
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
                
                # Check if values is an error dict
                if isinstance(values, dict) and "error" in values:
                    error_count["count"] += 1
                    error_type = values["error"]
                    error_msg = values["message"]
                    
                    # Log different error types with appropriate levels and messages
                    if error_type == "serial_exception":
                        if error_count["count"] == 1:
                            log("warn", f"[BOARD_RELAY] Kết nối USB bị mất: {error_msg}")
                        elif error_count["count"] == 5:
                            log("error", f"[BOARD_RELAY] USB disconnected - trying to reconnect...")
                            # Try to reconnect
                            if attempt_reconnect_relay():
                                error_count["count"] = 0  # Reset error count on successful reconnect
                    elif error_type == "timeout":
                        if error_count["count"] == 1:
                            log("warn", f"[BOARD_RELAY] Thiết bị không phản hồi: {error_msg}")
                        elif error_count["count"] == 5:
                            log("error", f"[BOARD_RELAY] Device timeout - check power and connections")
                    elif error_type == "os_error":
                        if error_count["count"] == 1:
                            log("warn", f"[BOARD_RELAY] Lỗi hệ thống: {error_msg}")
                        elif error_count["count"] == 5:
                            log("error", f"[BOARD_RELAY] OS error - port may have disappeared")
                            # Try to reconnect for OS errors too
                            if attempt_reconnect_relay():
                                error_count["count"] = 0
                    elif error_type == "connection_not_available":
                        if error_count["count"] == 1:
                            log("warn", f"[BOARD_RELAY] Chưa có kết nối: {error_msg}")
                    else:
                        if error_count["count"] == 5:
                            log("error", f"[BOARD_RELAY] Lỗi không xác định: {error_msg}")
                            
                elif values:  # Success case
                    error_count["count"]=0
                    prev = last_values.get("values")
                    if values != prev:
                        ts = _ts_local()
                        response = {"type":"read_response","device":"BOARD_RELAY",
                                    "address":device_config["read_settings"]["start_address"],
                                    "values":values,"timestamp":ts}
                        summary  = make_state_summary(values, device_config, config, ts)
                        log_json("debug", response); log_json("info", summary)
                        # No more publishing - only log the states

                        if prev is not None:
                            old_home = 1 if prev[idx_home] else 0
                            new_home = 1 if values[idx_home] else 0
                            if new_home != old_home and new_home == 1:
                                now_ms = int(time.time()*1000)
                                if now_ms - last_emit_time.get("Home",0) >= debounce_ms:
                                    last_emit_time["Home"] = now_ms
                                    handle_input_edge("Home", 1)
                            old_reset = 1 if prev[idx_reset] else 0
                            new_reset = 1 if values[idx_reset] else 0
                            if new_reset != old_reset and new_reset == 1:
                                now_ms = int(time.time()*1000)
                                if now_ms - last_emit_time.get("Reset",0) >= debounce_ms:
                                    last_emit_time["Reset"] = now_ms
                                    handle_input_edge("Reset", 1)

                        last_values["values"]=values
                else:
                    # Fallback for None values (shouldn't happen with new code)
                    error_count["count"] += 1
                    if error_count["count"] == 5:
                        log("warn","[BOARD_RELAY] Không thể đọc dữ liệu. Kiểm tra kết nối.")
                        
                time.sleep(device_config["read_settings"]["interval_ms"]/1000.0)
            except Exception as e:
                error_count["count"] += 1
                if error_count["count"] == 5:
                    log("error", f"[BOARD_RELAY] Background read error: {str(e)}")
                time.sleep(1)
    finally:
        # No more PUB socket cleanup needed
        pass

# -----------------------------
# RPC (ZeroMQ REP)
# -----------------------------
def handle_envelope(envelope):
    # Di chuyển các biến này lên đầu để tránh lỗi sử dụng trước khi gán giá trị
    store = _load_store()
    message_id = envelope.get("messageId") or str(uuid.uuid4())
    cmd = (envelope.get("command") or "").upper().strip()
    payload = envelope.get("payload") or {}

    # ----------------- MOVE_AXIS -----------------
    if cmd == "MOVE_AXIS":
        axis = str(payload.get("axis", "")).upper()
        value = payload.get("value")
        if axis not in ("X", "Y"):
            return _err(message_id, "Axis must be 'X' or 'Y'")
        # Thêm log để kiểm tra giá trị value
        log("debug", f"[MOVE_AXIS] Received axis={axis}, value={value}")
        
        # Kiểm tra và ánh xạ giá trị từ distance nếu value không tồn tại
        value = payload.get("value")
        if value is None:
            value = payload.get("distance")

        if value is None:
            log("error", f"[MOVE_AXIS] Missing value in payload: {payload}")
            return _err(message_id, "Value is missing in the payload")

        try:
            value = float(value)
        except ValueError as ve:
            log("error", f"[MOVE_AXIS] Invalid value: {value}. Error: {ve}")
            return _err(message_id, f"Value must be a number. Received: {value}")
        except Exception as e:
            log("error", f"[MOVE_AXIS] Unexpected error when parsing value: {e}")
            return _err(message_id, "Unexpected error occurred while parsing value")
        try:
            raw = build_move_axis_command(axis, value)
            result = exec_sc_operation(message_id, f"MOVE_{axis}", raw, "ui", {"axis": axis, "value": value}, wait=True)
            if result.get("ok"):
                return _ok(message_id, {"axis": axis, "value": value, "Sent": _sent_repr(raw)})
            else:
                return _err(message_id, f"Timeout {result.get('timeoutMs',0)} ms (lastCode={result.get('lastCode')})")
        except Exception as e:
            return _err(message_id, f"MOVE_AXIS error: {e}")

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
                # Check if there were relay errors even though VM2030 operation succeeded
                if result.get("has_relay_errors", False):
                    error_msg = f"VM2030 operation succeeded but relay errors occurred: {'; '.join(result.get('relay_errors', []))}"
                    return _err(message_id, error_msg)
                else:
                    return _ok(message_id, {"state": state, "Sent": _sent_repr(raw)})
            else:
                return _err(message_id, f"Timeout {result.get('timeoutMs',0)} ms (lastCode={result.get('lastCode')})")
        except Exception as e:
            return _err(message_id, f"BUILTIN_COMMAND error: {e}")

    # ----------------- SET_JOB (nhận JSON, build body đầy đủ) -----------------
    if cmd == "SET_JOB":
        idx = int(payload.get("JobNumber") or payload.get("index") or 1)
        err = _ensure_sc_available_or_err(message_id)
        if err: return err
        if not _is_ready_now(): return _err(message_id, "NOT_READY")
        try:
            cached_tail = None
            if str(idx) in store.get("jobs", {}):
                cached_tail = store["jobs"][str(idx)].get("_raw_tail")

            raw = build_vm2030_set_job_command(idx, payload, cached_tail)

            job_id = _ensure_job_id(store, idx)
            now_iso = _iso_now()
            model = {
                "Id": job_id,
                "CreatedAt": store["jobs"].get(str(idx), {}).get("CreatedAt", now_iso),
                "LastRunAt": now_iso,
                "JobNumber": idx,
                "JobName": str(payload.get("JobName", "")),
                "CharacterString": _normalize_spaces(str(payload.get("CharacterString", ""))).replace("_", " "),
                "StartX": _parse_float(payload.get("StartX", 0.0), 0.0),
                "StartY": _parse_float(payload.get("StartY", 0.0), 0.0),
                "PitchX": _parse_float(payload.get("PitchX", 0.0), 0.0),
                "PitchY": _parse_float(payload.get("PitchY", 0.0), 0.0),
                "Size": _parse_float(payload.get("Size", 1.0), 1.0),
                "Speed": _parse_int(payload.get("Speed", 100), 100),
                "Direction": _parse_int(payload.get("Direction", 0), 0),
                "Increment": None, "Calendar": None, "CircularMarking": None
            }
            store["jobs"][str(idx)] = {**store["jobs"].get(str(idx), {}), **model,
                                       "_raw_tail": (cached_tail or DEFAULT_JOB_TAIL)}
            _save_store(store)

            result = exec_sc_operation(message_id, "SET_JOB", raw, "ui", {"index": idx}, wait=True)
            if result.get("ok"):
                return _ok(message_id, {"Id": job_id, "JobNumber": idx, "Sent": _sent_repr(raw)})
            else:
                return _err(message_id, f"Timeout {result.get('timeoutMs',0)} ms (lastCode={result.get('lastCode')})")

        except Exception as e:
            return _err(message_id, f"SET_JOB error: {e}")

    # ----------------- GET_JOB (dùng logic từ test_get_job_bridge) -----------------
    if cmd == "GET_JOB":
        try:
            idx = int(payload.get("JobNumber") or payload.get("index") or 1)
            err = _ensure_sc_available_or_err(message_id)
            if err: return err

            dry_run = bool(config["devices"]["SOFTWARE_COMMAND"].get("dry_run", False))
            
            if dry_run:
                # Dry run - trả về dữ liệu giả từ store
                s_job = store["jobs"].get(str(idx)) or {}
                import random
                fake_id = f"{random.randrange(16**24):024x}"
                now = datetime.utcnow().isoformat() + "Z"
                
                reply = {
                    "Id": fake_id,
                    "CreatedAt": now,
                    "LastRunAt": now, 
                    "LastUpdated": now,
                    "LastSyncedToDevice": now,
                    "JobNumber": idx,
                    "JobName": s_job.get("JobName", ""),
                    "CharacterString": s_job.get("CharacterString", f"J {idx}"),
                    "StartX": float(s_job.get("StartX", 0.0)),
                    "StartY": float(s_job.get("StartY", 0.0)),
                    "PitchX": float(s_job.get("PitchX", 0.0)),
                    "PitchY": float(s_job.get("PitchY", 0.0)),
                    "Size": float(s_job.get("Size", 2.0)),
                    "Speed": int(s_job.get("Speed", 500)),
                    "Direction": int(s_job.get("Direction", 0)),
                    "Increment": None,
                    "Calendar": None,
                    "CircularMarking": None,
                    "IsError": False,
                    "ErrorMessage": ""
                }
                return _ok(message_id, reply)
            
            # Real mode - gửi xuống máy thật giống test_get_job_bridge.py
            if ser_cmd:
                # TẮT reader thread tạm thời để tránh nó ăn mất dữ liệu
                print("[GET_JOB] Temporarily disabling reader thread...")
                _reader_thread_enabled.clear()  # Tắt reader thread
                
                try:
                    job_cmd = f"%J{idx}_B\r".encode("ascii")
                    print(f"[GET_JOB] Sending command: {job_cmd}")
                    ser_cmd.reset_input_buffer()  # Clear buffer trước khi gửi
                    ser_cmd.write(job_cmd)
                    ser_cmd.flush()
                    print("[GET_JOB] Command sent, waiting for response...")
                    time.sleep(1.0)  # Tăng từ 0.5s lên 1.0s
                    response = ser_cmd.read(512)  # Tăng từ 256 lên 512 bytes
                    
                    # Nếu không có dữ liệu, thử đọc thêm
                    if len(response) == 0:
                        print("[GET_JOB] No data received, trying again...")
                        time.sleep(0.5)
                        response = ser_cmd.read(512)
                    
                    print(f"[GET_JOB] Received from machine: {response}")
                    log("debug", f"[GET_JOB] Received from machine: {response}")
                finally:
                    # BẬT LẠI reader thread cho các lệnh khác
                    print("[GET_JOB] Re-enabling reader thread...")
                    _reader_thread_enabled.set()  # Bật lại reader thread
                
                # Parse segments theo 0x1F như trong bridge
                segments = response.split(b"\x1F")
                print(f"[DEBUG] Total segments: {len(segments)}")
                for i, seg in enumerate(segments):
                    print(f"[DEBUG] Segment {i}: {seg}")
                    
                if len(segments) >= 3:
                    # Parse body - tìm segment chứa dữ liệu job
                    body_segment = None
                    for i, seg in enumerate(segments):
                        seg_str = seg.decode(errors="replace")
                        if "J 20" in seg_str or seg_str.count("_") > 10:  # segment chứa nhiều _ là body
                            body_segment = seg_str
                            print(f"[DEBUG] Using segment {i} as body: {seg_str}")
                            break
                    
                    if not body_segment:
                        body_segment = segments[2].decode(errors="replace")
                        print(f"[DEBUG] Fallback to segment 2: {body_segment}")
                    
                    body = body_segment.replace("\r","").strip()
                    tokens = [t.strip() for t in body.split("_")]
                    # print(f"[DEBUG] Total tokens: {len(tokens)}")
                    # for i, token in enumerate(tokens):
                    #     print(f"[DEBUG] Token {i}: '{token}'")
                    
                    def get(idx, default=None):
                        return tokens[idx] if idx < len(tokens) else default
                        
                    # Mapping các trường theo phân tích token đúng
                    import datetime
                    import random
                    
                    # JobNumber từ token 0: "J 20" -> 20
                    job_header = get(0, "J 0")
                    job_number = 0
                    if job_header and job_header.startswith("J"):
                        try:
                            job_number = int(job_header[1:].strip())
                        except:
                            job_number = 0
                    
                    # Các trường số theo token index đúng
                    size = float(get(1, "0.0")) if get(1) else 0.0
                    # token 2 là mode/flag, skip
                    speed = int(get(3, "0")) if get(3) else 0
                    start_x = float(get(4, "0.0")) if get(4) else 0.0
                    start_y = float(get(5, "0.0")) if get(5) else 0.0
                    pitch_x = float(get(6, "0.0")) if get(6) else 0.0
                    pitch_y = float(get(7, "0.0")) if get(7) else 0.0
                    
                    # Direction từ token 2
                    direction = int(get(2, "0")) if get(2) and get(2).isdigit() else 0
                    
                    # DEBUG: So sánh Direction ở các vị trí khác nhau
                    direction_token2 = int(get(2, "0")) if get(2) and get(2).isdigit() else 0
                    direction_token21 = get(21, "")
                    direction_token22 = get(22, "")
                    print(f"[DEBUG] Direction comparison: token2='{direction_token2}', token21='{direction_token21}', token22='{direction_token22}'")
                    print(f"[DEBUG] Using Direction = {direction} from token2 (consistent with SET_JOB)")
                    
                    # CharacterString từ token 23 (loại bỏ \r và quotes thừa)
                    character_string = get(23, "")
                    if character_string:
                        # Loại bỏ \r, "" ở cuối và ký tự escape thừa
                        character_string = character_string.replace('\r', '').replace('""', '').replace('"', '').strip()
                    
                    # Tạo fake ID theo yêu cầu
                    fake_id = f"{random.randrange(16**24):024x}"
                    now = datetime.datetime.utcnow().isoformat() + "Z"
                    
                    reply = {
                        "Id": fake_id,
                        "CreatedAt": now,
                        "LastRunAt": now,
                        "LastUpdated": now,
                        "LastSyncedToDevice": now,
                        "JobNumber": job_number,
                        "JobName": "",
                        "CharacterString": character_string,
                        "StartX": start_x,
                        "StartY": start_y,
                        "PitchX": pitch_x,
                        "PitchY": pitch_y,
                        "Size": size,
                        "Speed": speed,
                        "Direction": direction,
                        "Increment": None,
                        "Calendar": None,
                        "CircularMarking": None,
                        "IsError": False,
                        "ErrorMessage": ""
                    }
                    
                    import json
                    print("[GET_JOB] JSON Response:")
                    print(json.dumps(reply, ensure_ascii=False, indent=2))
                    log_json("info", reply)
                    
                    return _ok(message_id, reply)
                else:
                    reply = {
                        "IsError": True,
                        "ErrorMessage": f"Không đủ dữ liệu (nhận {len(response)} bytes)"
                    }
                    log_json("error", reply)
                    return _err(message_id, reply["ErrorMessage"])
            else:
                print("[GET_JOB] SOFTWARE_COMMAND serial not available")
                log("error", "[GET_JOB] SOFTWARE_COMMAND serial not available")
                return _err(message_id, "SOFTWARE_COMMAND serial not available")

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

            raw = sc_build_set_sequence(idx, cmdstr)
            result = exec_sc_operation(message_id, "SET_SEQUENCE", raw, "ui", {"index": idx}, wait=True)
            if result.get("ok"):
                return _ok(message_id, {"index": idx, "Sent": _sent_repr(raw)})
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
            print(f"[START_SEQUENCE] Sending sequence {idx}, waiting for 0x1F completion...")
            print(f"[START_SEQUENCE] Reader thread enabled: {_reader_thread_enabled.is_set()}")
            result = exec_sc_operation(message_id, "START_SEQUENCE", raw, "ui", {"index": idx}, wait=True)
            if result.get("ok"):
                print(f"[START_SEQUENCE] Sequence {idx} completed successfully (received 0x1F)")
                return _ok(message_id, {"index": idx, "Sent": _sent_repr(raw)})
            else:
                print(f"[START_SEQUENCE] Sequence {idx} timed out, lastCode={result.get('lastCode')}")
                return _err(message_id, f"Timeout {result.get('timeoutMs',0)} ms (lastCode={result.get('lastCode')})")
        except Exception as e:
            return _err(message_id, f"START_SEQUENCE error: {e}")

    # ----------------- TOGGLE_ECHO -----------------
    if cmd == "TOGGLE_ECHO":
        echo_enabled = payload.get("echo_enabled", False)
        if not _is_ready_now(): return _err(message_id, "NOT_READY")
        err = _ensure_sc_available_or_err(message_id)
        if err: return err
        try:
            raw = sc_build_toggle_echo(echo_enabled)
           
            result = exec_sc_operation(message_id, "TOGGLE_ECHO", raw, "ui", {"echo_enabled": echo_enabled}, wait=True)
            if result.get("ok"):
                echo_status = "enabled" if echo_enabled else "disabled"
                return _ok(message_id, {"echo_enabled": echo_enabled, "status": f"Echo {echo_status}", "Sent": _sent_repr(raw)})
            else:
                return _err(message_id, f"Timeout {result.get('timeoutMs',0)} ms (lastCode={result.get('lastCode')})")
        except Exception as e:
            return _err(message_id, f"TOGGLE_ECHO error: {e}")

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
                return _ok(message_id, {"index": idx, "Sent": _sent_repr(raw)})
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

    # ----------------- SET_DRY_RUN_STATE -----------------
    if cmd == "SET_DRY_RUN_STATE":
        if not config["devices"]["BOARD_RELAY"].get("dry_run", False):
            return _err(message_id, "BOARD_RELAY dry_run is not enabled")
        
        try:
            # Extract state values from payload
            ready = payload.get("ready")
            home = payload.get("home") 
            reset = payload.get("reset")
            other_registers = payload.get("other_registers")
            
            # Update dry_run_state in config
            dry_state = config["devices"]["BOARD_RELAY"].setdefault("dry_run_state", {})
            
            if ready is not None:
                dry_state["ready"] = int(ready)
            if home is not None:
                dry_state["home"] = int(home)
            if reset is not None:
                dry_state["reset"] = int(reset)
            if other_registers is not None:
                dry_state["other_registers"] = list(other_registers)[:5]  # Limit to 5 registers
            
            # Save config to file
            save_config(config)
            
            log("info", f"[DRY_RUN] Updated state: Ready={dry_state.get('ready',0)}, Home={dry_state.get('home',0)}, Reset={dry_state.get('reset',0)}")
            
            return _ok(message_id, {
                "ready": dry_state.get("ready", 0),
                "home": dry_state.get("home", 0), 
                "reset": dry_state.get("reset", 0),
                "other_registers": dry_state.get("other_registers", [0]*5)
            })
        except Exception as e:
            return _err(message_id, f"SET_DRY_RUN_STATE error: {e}")

    # ----------------- GET_DRY_RUN_STATE -----------------
    if cmd == "GET_DRY_RUN_STATE":
        try:
            dry_state = config["devices"]["BOARD_RELAY"].get("dry_run_state", {})
            return _ok(message_id, {
                "dry_run_enabled": config["devices"]["BOARD_RELAY"].get("dry_run", False),
                "ready": dry_state.get("ready", 0),
                "home": dry_state.get("home", 0),
                "reset": dry_state.get("reset", 0), 
                "other_registers": dry_state.get("other_registers", [0]*5)
            })
        except Exception as e:
            return _err(message_id, f"GET_DRY_RUN_STATE error: {e}")

    return _err(message_id, f"Unknown command '{cmd}'")

def zmq_rep_server(stop_event, cfg):
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REP)
    rep_bind = cfg.get("zeromq",{}).get("rep_bind","tcp://*:5555")
    sock.RCVTIMEO = int(cfg.get("zeromq",{}).get("rcv_timeout_ms",1000))
    sock.SNDTIMEO = int(cfg.get("zeromq",{}).get("snd_timeout_ms",1000))
    sock.bind(rep_bind)
    log("debug", f"[CONTROLLER] REP server bound at {rep_bind}")
    try:
        while not stop_event.is_set():
            raw = None
            try:
                raw = sock.recv()
            except zmq.Again:
                continue
            except Exception as e:
                log("error", f"[CONTROLLER] recv error: {e}"); continue
            try:
                raw_json = raw.decode("utf-8")
                cmd = json.loads(raw_json)
                
                # Log ZMQ request đến Seq với structured data (chỉ khi seq_logging=True)
                if seq_logger and _HAS_SEQ_LOGGER and config.get("seq_logging", True):
                    log_zmq_request(seq_logger, 
                                   command=cmd.get("command", "unknown"),
                                   message_id=cmd.get("messageId", "unknown"),
                                   payload_size=len(raw_json),
                                   target_device=cmd.get("targetDevice", "unknown"))
                
                # Log JSON request từ UI
                log("warn", f"[CONTROLLER] JSON Request: {raw_json}",
                    RequestType="ZMQOperation", 
                    ZMQRequest=True,
                    Command=cmd.get("command", "unknown"),
                    MessageID=cmd.get("messageId", "unknown"),
                    PayloadSize=len(raw_json),
                    ClientRequest=True)
                
                reply = handle_envelope(cmd if isinstance(cmd, dict) else {})
                reply_json = json.dumps(reply, ensure_ascii=False)
                
                # Response được gửi về client (không log trong dry-run)
                # log("warn", f"[CONTROLLER] Response sent: {reply_json}", 
                #     RequestType="ZMQOperation",
                #     ZMQResponse=True, 
                #     MessageID=cmd.get("messageId", "unknown"),
                #     IsError=reply.get("IsError", False),
                #     Command=cmd.get("command", "unknown"),
                #     ResponseSize=len(reply_json),
                #     ProcessingSuccess=True)
                
                sock.send_string(reply_json)
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
    # Parse CLI arguments
    args = parse_arguments()
    
    print("VM2030 Controller Starting...")
    print(f"📁 Using config file: {args.config}")
    
    # Load config with CLI overrides
    config = load_config(args.config)
    config = apply_cli_overrides(config, args)
    
    # Setup Seq logging trước tiên
    if _HAS_SEQ_LOGGER:
        try:
            print("🔄 Setting up Seq logging...")
            log_level = config.get("logging", {}).get("level", "INFO").upper()
            globals()['seq_logger'] = setup_logging(level=log_level)
            print("✅ Seq logging initialized successfully")
        except Exception as e:
            print(f"⚠️  Seq logging setup failed: {e}")
            print(f"⚠️  Error details: {type(e).__name__}: {str(e)}")
            print("Continuing with console logging only...")
            globals()['seq_logger'] = None
    else:
        print("⚠️  Seq logger not available - using console logging only")
        print("⚠️  Make sure 'pip install seqlog' is installed")
        globals()['seq_logger'] = None
    
    # Show dry run status
    relay_dry = config["devices"]["BOARD_RELAY"].get("dry_run", False)
    command_dry = config["devices"]["SOFTWARE_COMMAND"].get("dry_run", False)
    
    if relay_dry or command_dry:
        print("Dry Run Mode Active:")
        if relay_dry:
            print("  BOARD_RELAY: DRY RUN (simulated)")
        if command_dry:
            print("  SOFTWARE_COMMAND: DRY RUN (simulated)")
    else:
        print("Hardware Mode: Using real COM ports")
    
    setup_com_ports(config)

    # Log application startup đến Seq (chỉ khi seq_logging=True)
    if seq_logger and config.get("seq_logging", True):
        seq_logger.debug("VM2030 Controller application started", extra={
            "Signal": "vm2030_controller",
            "Application": "IndustrialController",
            "Component": "VM2030Controller", 
            "DeviceType": "VM2030LaserMarker",
            "ApplicationEvent": "Startup",
            "Version": "1.0.0",
            "ConfigFile": CONFIG_FILE,
            "StartupTimestamp": _iso_now()
        })

    # Open BOARD_RELAY
    dev = config["devices"]["BOARD_RELAY"]
    ser = None
    
    if dev.get("dry_run", False):
        log("info", "BOARD_RELAY dry run mode - no actual connection needed",
            ApplicationEvent="DeviceSetup", Device="BOARD_RELAY", DryRun=True)
        ser = None
    else:
        if not dev.get("com_port"):
            print("BOARD_RELAY chưa có COM. Cấu hình lại rồi chạy tiếp.")
            exit(1)
        try:
            ser = serial.Serial(dev["com_port"], dev.get("baud_rate",9600), parity=serial.PARITY_NONE,
                                stopbits=serial.STOPBITS_ONE, timeout=1)
            log("info", f"Kết nối tới {dev['com_port']} (BOARD_RELAY) thành công.",
                ApplicationEvent="DeviceSetup", Device="BOARD_RELAY", 
                COMPort=dev["com_port"], BaudRate=dev.get("baud_rate",9600),
                ConnectionSuccess=True)
        except Exception as e:
            log("error", f"Lỗi mở cổng {dev.get('com_port')}: {e}",
                ApplicationEvent="DeviceSetup", Device="BOARD_RELAY",
                COMPort=dev.get('com_port'), ConnectionError=str(e))
            exit(1)

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
        # BẬT LẠI reader thread với cơ chế control cho GET_JOB
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
