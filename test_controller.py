import serial
import serial.tools.list_ports
import time
import json
import os
import threading
from queue import Queue

# -----------------------------
# Globals
# -----------------------------
CONFIG_FILE = "device_config.json"
ser = None          # Serial cho BOARD_RELAY
ser_cmd = None      # Serial cho SOFTWARE_COMMAND
cmd_queue = None    # Hàng đợi gửi sang SOFTWARE_COMMAND
config = None       # Toàn bộ cấu hình

# -----------------------------
# Serial & Config Utilities
# -----------------------------

def get_available_ports():
    """Lấy danh sách các cổng COM có sẵn"""
    ports = serial.tools.list_ports.comports()
    available_ports = []
    for port in ports:
        available_ports.append({
            "device": port.device,
            "description": port.description,
            "hwid": port.hwid
        })
    return available_ports

def load_config():
    """Đọc cấu hình từ file (và trộn với mặc định, ưu tiên file cấu hình)"""
    default_config = {
        "devices": {
            "BOARD_RELAY": {
                "com_port": None,
                "baud_rate": 9600,
                "slave_id": 1,
                "read_settings": {
                    "start_address": 129,
                    "num_registers": 8,
                    "interval_ms": 500
                }
            },
            "SOFTWARE_COMMAND": {
                "com_port": None,
                "baud_rate": 115200,

                # JSON soft messages (soft_state/soft_command)
                "protocol": "ndjson",        # "ndjson" | "ascii" (chỉ áp dụng cho soft_* nếu bật)
                "enable_soft_json": True,    # False => chỉ gửi HEX, không gửi soft_*

                # mapping tín hiệu trong mảng values (0-based)
                # phần tử 1=Ready, 2=Home, 3=Reset -> index 0,1,2
                "signals": {
                    "Ready":  {"index": 0, "mode": "any"},     # any|rising|falling
                    "Home":   {"index": 1, "mode": "any"},
                    "Reset":  {"index": 2, "mode": "any"}
                },

                "emit_options": {
                    "debounce_ms": 0,       # 0: tắt chống dội
                    "edge_only": False,     # True: chỉ gửi khi có sườn
                    "min_interval_ms": 0    # 0: không giới hạn tốc độ phát
                },

                # Hành động gửi HEX thô khi tín hiệu đạt giá trị 'when' (trigger trên sườn lên)
                "hex_actions": [
                    {"signal": "Reset", "when": 1, "spec": "1D"},       # byte 0x1D
                    {"signal": "Home",  "when": 1, "spec": "%H<CR>"}    # 0x25 0x48 0x0D
                ],

                # Logging cho gói HEX
                "hex_log": {
                    "enabled": True,        # True: bật log; False: tắt log
                    "mode": "hex_ascii",    # "hex" | "ascii" | "hex_ascii"
                    "show_ts": True,        # in kèm timestamp
                    "prefix": "HEX>> "      # tiền tố dòng log
                }
            }
        },
        "active_device": "BOARD_RELAY"
    }

    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"Lỗi đọc file cấu hình: {e}")
            cfg = {}

    # Deep-merge: cfg ghi đè default_config
    def deep_merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                deep_merge(dst[k], v)
            else:
                dst[k] = v
        return dst

    result = deep_merge(default_config.copy(), cfg)
    return result

def save_config(cfg):
    """Lưu cấu hình vào file"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        print("Đã lưu cấu hình thiết bị")
    except Exception as e:
        print(f"Lỗi lưu file cấu hình: {e}")

def setup_com_ports(cfg):
    """
    Chọn cổng COM cho cả BOARD_RELAY và SOFTWARE_COMMAND (lần đầu),
    lưu lại để lần sau dùng luôn. Nếu COM đã lưu còn tồn tại thì bỏ qua.
    Cho phép nhập số thứ tự hoặc gõ trực tiếp tên cổng (COM5, /dev/ttyUSB0...).
    """
    def list_and_pick(device_key):
        nonlocal cfg
        while True:
            ports = get_available_ports()
            print(f"\n--- Cấu hình cổng cho {device_key} ---")
            saved = cfg["devices"][device_key].get("com_port")
            if saved:
                still_there = any(p["device"] == saved for p in ports)
                if still_there:
                    print(f"  Đang dùng lại cổng đã lưu: {saved}")
                    return  # dùng lại không hỏi
                else:
                    print(f"  Cổng đã lưu ({saved}) không còn tồn tại, cần chọn lại.")

            if not ports:
                print("  Không tìm thấy cổng serial nào. Cắm thiết bị rồi Enter để thử lại.")
                input()
                continue

            print("  Danh sách cổng hiện có:")
            for i, p in enumerate(ports, 1):
                print(f"   {i}. {p['device']} - {p['description']}")

            print("  Nhập số (1..n) để chọn, hoặc gõ tên cổng (vd: COM5),")
            print("  nhấn Enter để bỏ qua (để None), hoặc gõ 'r' để refresh.")
            raw = input("  Chọn: ").strip()

            if raw == "":
                cfg["devices"][device_key]["com_port"] = None
                return
            if raw.lower() == "r":
                continue

            if raw.isdigit():
                idx = int(raw)
                if 1 <= idx <= len(ports):
                    cfg["devices"][device_key]["com_port"] = ports[idx-1]["device"]
                    return
                else:
                    print("  Số không hợp lệ, thử lại.")
                    continue

            cfg["devices"][device_key]["com_port"] = raw
            return

    list_and_pick("BOARD_RELAY")
    list_and_pick("SOFTWARE_COMMAND")

    # Cảnh báo nếu 2 thiết bị dùng chung cổng
    br = cfg["devices"]["BOARD_RELAY"].get("com_port")
    sc = cfg["devices"]["SOFTWARE_COMMAND"].get("com_port")
    if br and sc and br == sc:
        print("⚠️  CẢNH BÁO: BOARD_RELAY và SOFTWARE_COMMAND đang dùng CÙNG MỘT cổng. Nên tách ra 2 cổng khác nhau.")

    save_config(cfg)

def open_serial_for(device_key, cfg):
    """Mở cổng serial cho device_key dựa trên cấu hình."""
    dev = cfg["devices"].get(device_key, {})
    port = dev.get("com_port")
    baud = dev.get("baud_rate", 9600)
    if not port:
        return None
    try:
        s = serial.Serial(port, baud, parity=serial.PARITY_NONE,
                          stopbits=serial.STOPBITS_ONE, timeout=1)
        print(f"[{device_key}] Kết nối tới {port} ({baud}bps) thành công.")
        return s
    except Exception as e:
        print(f"[{device_key}] Lỗi mở cổng {port}: {e}")
        return None

# -----------------------------
# HEX spec helpers
# -----------------------------

def parse_hex_spec(spec: str):
    """
    Chuyển 'spec' thành list[int] bytes.
    Hỗ trợ 2 kiểu:
      - HEX thuần: '1D', '25 48 0D' -> [0x1D] / [0x25,0x48,0x0D]
      - ASCII + token <>: '%H<CR>'  -> [0x25,0x48,0x0D]; hỗ trợ <CR>, <LF>, <ESC>, <GS>, <TAB>, <NUL>
    """
    if spec is None:
        return []
    s = spec.strip()

    # Nếu toàn ký tự hex và khoảng trắng -> parse theo cặp
    if all(c in "0123456789abcdefABCDEF " for c in s) and len(s.replace(" ", "")) % 2 == 0 and s != "":
        s2 = s.replace(" ", "")
        return [int(s2[i:i+2], 16) for i in range(0, len(s2), 2)]

    # ASCII + tokens
    tokens = {
        "CR": 0x0D, "LF": 0x0A, "ESC": 0x1B, "GS": 0x1D, "TAB": 0x09, "NUL": 0x00
    }
    out = []
    i = 0
    while i < len(s):
        if s[i] == "<":
            j = s.find(">", i+1)
            if j != -1:
                name = s[i+1:j].strip().upper()
                if name in tokens:
                    out.append(tokens[name])
                i = j + 1
                continue
        out.append(ord(s[i]))
        i += 1
    return out

def _fmt_hex(bs):
    return " ".join(f"{b:02X}" for b in bs)

def _fmt_ascii(bs):
    # hiển thị ASCII; byte không in được -> '.'
    return "".join(chr(b) if 32 <= b < 127 else "." for b in bs)

# -----------------------------
# Modbus Utilities
# -----------------------------

def calculate_crc(data):
    """Hàm tính CRC16 Modbus, trả về bytes (little-endian)."""
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for _ in range(8):
            if crc & 0x0001:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return crc.to_bytes(2, byteorder="little")

def read_holding_registers(slave_id, start_addr, num_registers):
    """
    Đọc giá trị từ holding registers (FC=03) từ thiết bị BOARD_RELAY qua 'ser' global.
    :return: List các giá trị đọc được hoặc None nếu có lỗi
    """
    global ser
    try:
        data = [slave_id, 0x03, start_addr >> 8, start_addr & 0xFF, num_registers >> 8, num_registers & 0xFF]
        data += list(calculate_crc(data))
        ser.reset_input_buffer()
        ser.write(bytearray(data))
        time.sleep(0.1)
        expected_bytes = 3 + (num_registers * 2) + 2
        response = ser.read(expected_bytes)

        if len(response) != expected_bytes:
            return None

        received_crc = response[-2:]
        calculated_crc = calculate_crc(response[:-2])
        if received_crc != calculated_crc:
            return None

        values = []
        for i in range(3, len(response)-2, 2):
            value = (response[i] << 8) | response[i+1]
            values.append(value)
        return values
    except Exception:
        return None

def control_single_relay(slave_id, relay_addr, state_value, retries=2, tx_delay_s=0.02):
    """
    Ghi 1 thanh ghi (FC=16/0x10) cho relay_addr (1..12) với state_value (1=bật, 2=tắt).
    Kiểm tra phản hồi:
      - OK: {"ok": True, "detail": {...}}
      - Lỗi: {"ok": False, "error": "...", "detail": {...optional}}
    Hỗ trợ retry khi lỗi CRC/timeout.
    """
    global ser
    if not (1 <= relay_addr <= 12):
        return {"ok": False, "error": "Địa chỉ relay phải 1..12"}

    # Khung yêu cầu: [slave, 0x10, addr_hi, addr_lo, qty_hi, qty_lo, bytecnt, data_hi, data_lo] + CRC
    # Ở đây ghi 1 thanh ghi tại địa chỉ 0x00NN (NN=relay_addr), qty=1, bytecnt=2, data=[state_value, 0x00]
    # NOTE: Nếu board cần hi/lo đảo (0x0001), đổi 2 byte cuối thành [0x00, state_value].
    frame = [slave_id, 0x10, 0x00, relay_addr, 0x00, 0x01, 0x02, state_value, 0x00]
    frame += list(calculate_crc(frame))

    ex_map = {
        0x01: "Illegal Function",
        0x02: "Illegal Data Address",
        0x03: "Illegal Data Value",
        0x04: "Slave Device Failure",
        0x05: "Acknowledge",
        0x06: "Slave Device Busy",
        0x07: "Negative Acknowledge",
        0x08: "Memory Parity Error"
    }

    for attempt in range(retries + 1):
        try:
            ser.reset_input_buffer()
            ser.write(bytearray(frame))
            time.sleep(tx_delay_s)

            # Chuẩn FC16: 8 byte echo; Exception: 5 byte
            resp = ser.read(8)
            if len(resp) == 0:
                resp = ser.read(8)

            # Exception?
            if len(resp) >= 5 and (resp[1] & 0x80):
                ex_resp = resp[:5]
                r_crc = ex_resp[-2:]
                calc_crc = calculate_crc(ex_resp[:-2])
                if r_crc != calc_crc:
                    if attempt < retries:
                        continue
                    return {"ok": False, "error": "CRC sai trong exception response", "detail": {"raw": ex_resp.hex()}}
                ex_code = ex_resp[2]
                return {
                    "ok": False,
                    "error": f"Modbus exception: {ex_map.get(ex_code, f'0x{ex_code:02X}')} ({ex_code})",
                    "detail": {"raw": ex_resp.hex()}
                }

            if len(resp) == 8:
                r_crc = resp[-2:]
                calc_crc = calculate_crc(resp[:-2])
                if r_crc != calc_crc:
                    if attempt < retries:
                        continue
                    return {"ok": False, "error": "CRC sai (phản hồi FC16)", "detail": {"raw": resp.hex()}}

                ok_slave = (resp[0] == slave_id)
                ok_func  = (resp[1] == 0x10)
                ok_addr  = (resp[2] == 0x00 and resp[3] == relay_addr)
                ok_qty   = (resp[4] == 0x00 and resp[5] == 0x01)

                if ok_slave and ok_func and ok_addr and ok_qty:
                    return {
                        "ok": True,
                        "detail": {
                            "slave": resp[0],
                            "function": resp[1],
                            "address": (resp[2] << 8) | resp[3],
                            "quantity": (resp[4] << 8) | resp[5]
                        }
                    }
                else:
                    return {
                        "ok": False,
                        "error": "Phản hồi không khớp echo (slave/func/address/qty)",
                        "detail": {"raw": resp.hex()}
                    }

            if attempt < retries:
                continue
            return {"ok": False, "error": f"Độ dài phản hồi bất thường ({len(resp)} bytes)", "detail": {"raw": resp.hex()}}

        except Exception as e:
            if attempt < retries:
                continue
            return {"ok": False, "error": f"Lỗi khi ghi relay: {e}"}

# -----------------------------
# Command Processing
# -----------------------------

def deep_merge_update(dst, src):
    """Deep-merge src vào dst (in-place)."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge_update(dst[k], v)
        else:
            dst[k] = v
    return dst

def process_command(command_json, device_config):
    """Xử lý lệnh JSON người dùng nhập từ console (không phải SOFTWARE_COMMAND)."""
    global config
    try:
        if "read" in command_json:
            print(json.dumps({
                "type": "info",
                "message": "Đang đọc dữ liệu tự động trong nền"
            }, indent=2))
            return

        # Điều khiển từng relay
        if "commands" in command_json:
            responses = []
            for cmd in command_json["commands"]:
                relay_num = int(cmd["relay"])
                state_value = 1 if int(cmd["state"]) == 1 else 2

                result = control_single_relay(device_config.get("slave_id", 1), relay_num, state_value)
                if result["ok"]:
                    responses.append({
                        "type": "control_response",
                        "relay": relay_num,
                        "command": "ON" if state_value == 1 else "OFF",
                        "status": "success",
                        "echo": result.get("detail", {})
                    })
                else:
                    responses.append({
                        "type": "control_response",
                        "relay": relay_num,
                        "command": "ON" if state_value == 1 else "OFF",
                        "status": "error",
                        "message": result.get("error", "unknown"),
                        "detail": result.get("detail", {})
                    })
                time.sleep(0.03)

            print(json.dumps({"responses": responses}, indent=2))
            return

        # Điều khiển tất cả relay
        if "all" in command_json:
            state_value = 1 if int(command_json["all"]) == 1 else 2
            responses = []
            for relay_num in range(1, 13):
                result = control_single_relay(device_config.get("slave_id", 1), relay_num, state_value)
                if result["ok"]:
                    responses.append({
                        "type": "control_response",
                        "relay": relay_num,
                        "command": "ON" if state_value == 1 else "OFF",
                        "status": "success",
                        "echo": result.get("detail", {})
                    })
                else:
                    responses.append({
                        "type": "control_response",
                        "relay": relay_num,
                        "command": "ON" if state_value == 1 else "OFF",
                        "status": "error",
                        "message": result.get("error", "unknown"),
                        "detail": result.get("detail", {})
                    })
                time.sleep(0.03)
            print(json.dumps({"responses": responses}, indent=2))
            return

        # Cập nhật cấu hình
        if "config" in command_json:
            new_config = command_json["config"]
            deep_merge_update(config, new_config)
            save_config(config)
            print(json.dumps({"type": "config_response", "status": "success", "config": config}, indent=2))
            return

        print(json.dumps({
            "type": "error",
            "message": "Cấu trúc lệnh không hợp lệ",
            "valid_commands": [
                {"commands": [{"relay": 1, "state": 1}, {"relay": 2, "state": 0}]},
                {"all": 1},
                {"config": {"read_settings": {"interval_ms": 1000}}}
            ]
        }, indent=2))

    except Exception as e:
        print(json.dumps({"type": "error", "message": str(e)}, indent=2))

# -----------------------------
# SOFTWARE_COMMAND: payload tổng hợp trạng thái
# -----------------------------

def make_state_summary(values, device_config, cfg, ts):
    """Tạo JSON tổng hợp trạng thái Ready/Home/Reset theo mapping trong config."""
    soft_cfg = cfg["devices"].get("SOFTWARE_COMMAND", {})
    signals = soft_cfg.get("signals", {})

    idx_ready = int(signals.get("Ready", {}).get("index", 0))
    idx_home  = int(signals.get("Home",  {}).get("index", 1))
    idx_reset = int(signals.get("Reset", {}).get("index", 2))

    def get_val(idx):
        return 1 if (0 <= idx < len(values) and values[idx]) else 0

    return {
        "type": "soft_state",
        "device": "SOFTWARE_COMMAND",
        "source": "BOARD_RELAY",
        "address": device_config["read_settings"]["start_address"],
        "states": {
            "Ready": get_val(idx_ready),
            "Home":  get_val(idx_home),
            "Reset": get_val(idx_reset)
        },
        "ts": ts
    }

# -----------------------------
# SOFTWARE_COMMAND writer (sender) thread
# -----------------------------

def software_command_writer(stop_event, ser_cmd_local, queue_local, cfg):
    """Thread gửi lệnh sang SOFTWARE_COMMAND theo hàng đợi queue_local"""
    if ser_cmd_local is None:
        return

    proto = cfg["devices"]["SOFTWARE_COMMAND"].get("protocol", "ndjson")
    enable_soft_json = bool(cfg["devices"]["SOFTWARE_COMMAND"].get("enable_soft_json", True))

    last_emit_at = 0
    min_interval_ms = int(cfg["devices"]["SOFTWARE_COMMAND"]
                          .get("emit_options", {})
                          .get("min_interval_ms", 0))

    while not stop_event.is_set():
        try:
            item = queue_local.get(timeout=0.2)  # item: tuple (signal,value,ts) | dict (soft_state/raw_bytes)
        except Exception:
            continue

        # enforce min interval nếu cần
        if min_interval_ms > 0:
            now = int(time.time() * 1000)
            if now - last_emit_at < min_interval_ms:
                time.sleep((min_interval_ms - (now - last_emit_at)) / 1000.0)

        try:
            if isinstance(item, dict):
                t = item.get("type", "")
                if t == "soft_state":
                    if not enable_soft_json:
                        continue  # tắt hẳn soft_state
                    if proto == "ndjson":
                        ser_cmd_local.write((json.dumps(item) + "\n").encode("utf-8"))
                    elif proto == "ascii":
                        st = item.get("states", {})
                        line = f"STATE Ready={int(st.get('Ready',0))} Home={int(st.get('Home',0))} Reset={int(st.get('Reset',0))}\n"
                        ser_cmd_local.write(line.encode("ascii"))
                elif t == "raw_bytes":
                    data = item.get("data", [])

                    # LOG (nếu bật)
                    hex_log = cfg["devices"]["SOFTWARE_COMMAND"].get("hex_log", {})
                    if hex_log.get("enabled", False):
                        mode = hex_log.get("mode", "hex_ascii")
                        prefix = hex_log.get("prefix", "HEX>> ")
                        ts_flag = hex_log.get("show_ts", True)
                        ts = time.strftime("%Y-%m-%d %H:%M:%S") if ts_flag else ""
                        parts = []
                        if mode in ("hex", "hex_ascii"):
                            parts.append(_fmt_hex(data))
                        if mode in ("ascii", "hex_ascii"):
                            parts.append(f"[{_fmt_ascii(data)}]")
                        line = f"{prefix}{' '.join(parts)}"
                        if ts:
                            line = f"{line} @ {ts}"
                        print(line)

                    # GỬI
                    ser_cmd_local.write(bytes(data))
                else:
                    # fallback: serialize nếu vẫn bật soft_json
                    if enable_soft_json:
                        ser_cmd_local.write((json.dumps(item) + "\n").encode("utf-8"))

                last_emit_at = int(time.time() * 1000)

            else:
                # tuple (signal, value, ts) -> soft_command cũ
                if not enable_soft_json:
                    continue  # tắt hẳn soft_command
                signal, value, ts = item
                if proto == "ndjson":
                    payload = {
                        "type": "soft_command",
                        "device": "SOFTWARE_COMMAND",
                        "signal": signal,
                        "value": int(value),
                        "source": "BOARD_RELAY",
                        "ts": ts
                    }
                    ser_cmd_local.write((json.dumps(payload) + "\n").encode("utf-8"))
                elif proto == "ascii":
                    line = f"{signal.upper()}={int(value)}\n"
                    ser_cmd_local.write(line.encode("ascii"))
                last_emit_at = int(time.time() * 1000)

        except Exception as e:
            print(f"\n[Lỗi gửi SOFTWARE_COMMAND] {e}")

# -----------------------------
# Reader thread (BOARD_RELAY) + change detection -> queue to SOFTWARE_COMMAND
# -----------------------------

def background_read(stop_event, last_values, error_count, device_config):
    """
    Thread đọc dữ liệu từ BOARD_RELAY + phát lệnh SOFTWARE_COMMAND khi values thay đổi
    """
    global ser_cmd, cmd_queue, config

    soft_cfg = config["devices"].get("SOFTWARE_COMMAND", {})
    signals = soft_cfg.get("signals", {})
    emit_opts = soft_cfg.get("emit_options", {})
    debounce_ms = int(emit_opts.get("debounce_ms", 0))
    edge_only = bool(emit_opts.get("edge_only", False))
    hex_actions = soft_cfg.get("hex_actions", [])
    enable_soft_json = bool(soft_cfg.get("enable_soft_json", True))

    # Bộ nhớ chống dội theo tín hiệu
    last_emit_time = {}   # key: signal, val: timestamp ms

    index_to_name = {}
    for name, spec in signals.items():
        try:
            idx = int(spec.get("index"))
            index_to_name[idx] = name
        except Exception:
            continue

    while not stop_event.is_set():
        try:
            values = read_holding_registers(
                device_config.get("slave_id", 1),
                device_config["read_settings"]["start_address"],
                device_config["read_settings"]["num_registers"]
            )

            if values:
                error_count["count"] = 0

                if values != last_values.get("values"):
                    response = {
                        "type": "read_response",
                        "device": "BOARD_RELAY",
                        "address": device_config["read_settings"]["start_address"],
                        "values": values,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    print("\033[K\r" + json.dumps(response))

                    # soft_state (tùy chọn)
                    if enable_soft_json:
                        summary = make_state_summary(values, device_config, config, response["timestamp"])
                        print("\033[K\r" + json.dumps(summary))
                        if ser_cmd is not None:
                            cmd_queue.put(summary)

                    print("\033[K\rNhập lệnh (? để xem hướng dẫn): ", end="", flush=True)

                    # Phát soft_command theo sườn & xử lý hex_actions
                    if ser_cmd is not None:
                        for idx, name in index_to_name.items():
                            if idx < 0 or idx >= len(values):
                                continue
                            new_val = 1 if values[idx] else 0
                            old_val = None
                            if last_values.get("values") and idx < len(last_values["values"]):
                                old_val = 1 if last_values["values"][idx] else 0

                            need_emit = False
                            mode = signals[name].get("mode", "any")

                            if old_val is None:
                                if mode == "any":
                                    need_emit = True
                                elif mode == "rising" and new_val == 1:
                                    need_emit = True
                                elif mode == "falling" and new_val == 0:
                                    need_emit = True
                            else:
                                if new_val != old_val:
                                    if mode == "any":
                                        need_emit = True
                                    elif mode == "rising" and old_val == 0 and new_val == 1:
                                        need_emit = True
                                    elif mode == "falling" and old_val == 1 and new_val == 0:
                                        need_emit = True

                            if edge_only and mode == "any":
                                pass

                            if need_emit and debounce_ms > 0:
                                now_ms = int(time.time() * 1000)
                                last_ts = last_emit_time.get(name, 0)
                                if now_ms - last_ts < debounce_ms:
                                    need_emit = False
                                else:
                                    last_emit_time[name] = now_ms

                            # 1) soft_command (nếu bật)
                            if need_emit and enable_soft_json:
                                ts = response["timestamp"]
                                cmd_queue.put((name, new_val, ts))

                            # 2) HEX actions: chỉ phát khi có sườn lên đạt 'when'
                            if old_val is None:
                                edge_up = (new_val == 1)
                            else:
                                edge_up = (old_val == 0 and new_val == 1)

                            if edge_up:
                                for act in hex_actions:
                                    if act.get("signal") == name and int(act.get("when", 1)) == 1:
                                        data = parse_hex_spec(act.get("spec", ""))
                                        if data:
                                            cmd_queue.put({"type": "raw_bytes", "data": data})

                    last_values["values"] = values

            else:
                error_count["count"] += 1
                if error_count["count"] == 5:
                    print("\033[K\rCảnh báo: Không thể đọc dữ liệu. Kiểm tra kết nối.")
                    print("\033[K\rNhập lệnh (? để xem hướng dẫn): ", end="", flush=True)

            time.sleep(device_config["read_settings"]["interval_ms"] / 1000.0)

        except Exception as e:
            error_count["count"] += 1
            if error_count["count"] == 5:
                print(f"\033[K\rLỗi: {str(e)}")
                print("\033[K\rNhập lệnh (? để xem hướng dẫn): ", end="", flush=True)
            time.sleep(1)

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
        ser = serial.Serial(
            device_config["com_port"],
            device_config.get("baud_rate", 9600),
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1
        )
        print(f"Kết nối tới {device_config['com_port']} (BOARD_RELAY) thành công.")
    except Exception as e:
        print(f"Lỗi mở cổng {device_config.get('com_port')}: {e}")
        exit(1)

    # SOFTWARE_COMMAND (có thể None nếu chưa cấu hình)
    ser_cmd = open_serial_for("SOFTWARE_COMMAND", config)

    # Hàng đợi & biến dùng chung
    cmd_queue = Queue()
    last_values = {"values": None}
    error_count = {"count": 0}
    stop_event = threading.Event()

    # Thread đọc BOARD_RELAY
    read_thread = threading.Thread(
        target=background_read,
        args=(stop_event, last_values, error_count, device_config),
        daemon=True
    )
    read_thread.start()

    # Thread writer SOFTWARE_COMMAND
    if ser_cmd is not None:
        writer_thread = threading.Thread(
            target=software_command_writer,
            args=(stop_event, ser_cmd, cmd_queue, config),
            daemon=True
        )
        writer_thread.start()
    else:
        writer_thread = None

    print("\nChương trình đang tự động đọc dữ liệu. Nhập lệnh để điều khiển.")
    print("Nhập ? để xem hướng dẫn")

    try:
        while True:
            try:
                command = input("\nNhập lệnh: ").strip()

                if command == "?":
                    print("\nHướng dẫn sử dụng JSON:")
                    print("1. Điều khiển relay: {\"commands\": [{\"relay\": 1, \"state\": 1}, {\"relay\": 2, \"state\": 0}]}")
                    print("2. Điều khiển tất cả: {\"all\": 1}")
                    print("3. Tắt JSON soft (chỉ gửi HEX): {\"config\": {\"devices\": {\"SOFTWARE_COMMAND\": {\"enable_soft_json\": false}}}}")
                    print("4. Sửa HEX actions: {\"config\": {\"devices\": {\"SOFTWARE_COMMAND\": {\"hex_actions\": [ {\"signal\":\"Reset\",\"when\":1,\"spec\":\"1D\"}, {\"signal\":\"Home\",\"when\":1,\"spec\":\"%H<CR>\"} ]}}}}")
                    print("5. Cấu hình log HEX: {\"config\": {\"devices\": {\"SOFTWARE_COMMAND\": {\"hex_log\": {\"enabled\": true, \"mode\": \"hex_ascii\", \"show_ts\": true}}}}}")
                    continue

                if not command:
                    continue

                command_json = json.loads(command)
                process_command(command_json, device_config)

            except KeyboardInterrupt:
                raise
            except json.JSONDecodeError:
                print(json.dumps({
                    "type": "error",
                    "message": "Định dạng JSON không hợp lệ"
                }, indent=2))
            except Exception as e:
                print(json.dumps({
                    "type": "error",
                    "message": str(e)
                }, indent=2))

    except KeyboardInterrupt:
        print("\nĐang dừng chương trình...")
        stop_event.set()
        if read_thread.is_alive():
            read_thread.join(timeout=1)
        if writer_thread and writer_thread.is_alive():
            writer_thread.join(timeout=1)

    finally:
        try:
            if ser:
                ser.close()
        except Exception:
            pass
        try:
            if ser_cmd:
                ser_cmd.close()
        except Exception:
            pass
        print(json.dumps({
            "type": "system",
            "message": "Đã đóng cổng serial"
        }, indent=2))
