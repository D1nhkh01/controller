import serial
import serial.tools.list_ports
import time
import json
import os
import threading
from queue import Queue
from software_command_handler import SoftwareCommandHandler

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
    """Đọc cấu hình từ file"""
    config_file = "device_config.json"
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
                "baud_rate": 9600
            }
        },
        "active_device": "BOARD_RELAY"  # Thiết bị đang được sử dụng
    }
    
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                # Cập nhật config mặc định với các giá trị từ file
                default_config.update(config)
        except Exception as e:
            print(f"Lỗi đọc file cấu hình: {e}")
    
    return default_config

def save_config(config):
    """Lưu cấu hình vào file"""
    config_file = "device_config.json"
    try:
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=2)
        print("Đã lưu cấu hình thiết bị")
    except Exception as e:
        print(f"Lỗi lưu file cấu hình: {e}")

def setup_com_port():
    """Thiết lập cổng COM"""
    config = load_config()
    ports = get_available_ports()
    
    if not ports:
        print("Không tìm thấy cổng COM nào!")
        return None
    
    active_device = config["active_device"]
    device_config = config["devices"][active_device]
    
    # Kiểm tra nếu cổng COM đã lưu vẫn còn tồn tại
    if device_config["com_port"]:
        if any(p["device"] == device_config["com_port"] for p in ports):
            return device_config
    
    # Hiển thị danh sách cổng và cho người dùng chọn
    print(f"\nĐang cấu hình cho thiết bị: {active_device}")
    print("\nDanh sách cổng COM có sẵn:")
    for i, port in enumerate(ports, 1):
        print(f"{i}. {port['device']} - {port['description']}")
    
    while True:
        try:
            choice = int(input("\nChọn số thứ tự cổng COM (1-{}): ".format(len(ports))))
            if 1 <= choice <= len(ports):
                device_config["com_port"] = ports[choice-1]["device"]
                save_config(config)
                return device_config
            print("Vui lòng chọn số từ 1 đến", len(ports))
        except ValueError:
            print("Vui lòng nhập một số hợp lệ!")

# Tải cấu hình và thiết lập cổng COM
device_config = setup_com_port()
if not device_config:
    exit()

# Thiết lập các thông số từ cấu hình
SERIAL_PORT = device_config["com_port"]
BAUD_RATE = device_config["baud_rate"]
SLAVE_ID = device_config["slave_id"]

try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=1)
    print(f"Kết nối tới {SERIAL_PORT} thành công.")
except Exception as e:
    print(f"Lỗi mở cổng {SERIAL_PORT}: {e}")
    exit()

# Định danh Slave ID
SLAVE_ID = 1

# Hàm tính CRC16 Modbus
def calculate_crc(data):
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

# Hàm đọc holding registers (Function code 03)
def read_holding_registers(slave_id, start_addr, num_registers):
    """
    Đọc giá trị từ holding registers.
    :param slave_id: ID của thiết bị
    :param start_addr: Địa chỉ bắt đầu đọc
    :param num_registers: Số lượng registers cần đọc
    :return: List các giá trị đọc được hoặc None nếu có lỗi
    """
    try:
        # Tạo lệnh Modbus RTU để đọc holding registers
        data = [slave_id, 0x03, start_addr >> 8, start_addr & 0xFF, num_registers >> 8, num_registers & 0xFF]
        data += list(calculate_crc(data))
        
        # Xóa buffer nhận
        ser.reset_input_buffer()
        
        # Gửi lệnh
        ser.write(bytearray(data))
        
        # Đợi và đọc phản hồi
        time.sleep(0.1)
        
        # Tính toán số byte cần đọc (response = slave_id + func_code + byte_count + data + 2 bytes CRC)
        expected_bytes = 3 + (num_registers * 2) + 2
        response = ser.read(expected_bytes)
        
        if len(response) != expected_bytes:
            return None
        
        # Kiểm tra CRC
        received_crc = response[-2:]
        calculated_crc = calculate_crc(response[:-2])
        if received_crc != calculated_crc:
            return None
        
        # Phân tích dữ liệu
        values = []
        for i in range(3, len(response)-2, 2):
            value = (response[i] << 8) | response[i+1]
            values.append(value)
        
        return values
    except Exception:
        return None

# Hàm điều khiển từng relay
def control_single_relay(slave_id, relay_addr, value):
    """
    Điều khiển một relay đơn lẻ.
    :param slave_id: ID của thiết bị
    :param relay_addr: Địa chỉ relay cần điều khiển (1-12)
    :param value: Giá trị relay (1: bật, 2: tắt)
    """
    # Tạo lệnh Modbus RTU cho một relay (chỉ truyền 1 giá trị)
    data = [slave_id, 0x10, 0x00, relay_addr, 0x00, 1, 2, value, 0x00]
    data += list(calculate_crc(data))
    # print(f"Gửi lệnh relay {relay_addr} {'BẬT' if value == 1 else 'TẮT'}")
    ser.write(bytearray(data))
    time.sleep(0.1)  # Chờ thiết bị xử lý

def process_command(command_json, device_config):
    """Xử lý lệnh JSON"""
    try:
        # Xử lý lệnh đọc dữ liệu
        if "read" in command_json:
            read_config = command_json["read"]
            start_addr = read_config.get("start_address", device_config["read_settings"]["start_address"])
            num_regs = read_config.get("num_registers", device_config["read_settings"]["num_registers"])
            interval = read_config.get("interval_ms", device_config["read_settings"]["interval_ms"])
            
            print(f"Đọc {num_regs} thanh ghi từ địa chỉ {start_addr}...")
            print("Nhấn Ctrl+C để dừng việc đọc...")
            
            while True:
                values = read_holding_registers(SLAVE_ID, start_addr, num_regs)
                if values:
                    response = {
                        "type": "read_response",
                        "address": start_addr,
                        "values": values,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    print("\r" + json.dumps(response), end="    ")
                time.sleep(interval / 1000)
        
        # Xử lý lệnh điều khiển relay
        elif "commands" in command_json:
            responses = []
            for cmd in command_json["commands"]:
                relay_num = cmd["relay"]
                state = 1 if cmd["state"] == 1 else 2
                
                if 1 <= relay_num <= 12:
                    control_single_relay(SLAVE_ID, relay_num, state)
                    response = {
                        "type": "control_response",
                        "relay": relay_num,
                        "state": "ON" if state == 1 else "OFF",
                        "status": "success"
                    }
                else:
                    response = {
                        "type": "control_response",
                        "relay": relay_num,
                        "status": "error",
                        "message": "Số relay không hợp lệ (phải từ 1-12)"
                    }
                responses.append(response)
                time.sleep(0.1)
            
            print(json.dumps({"responses": responses}, indent=2))
        
        # Xử lý lệnh điều khiển tất cả relay
        elif "all" in command_json:
            state = 1 if command_json["all"] == 1 else 2
            responses = []
            
            for relay_num in range(1, 13):
                control_single_relay(SLAVE_ID, relay_num, state)
                response = {
                    "type": "control_response",
                    "relay": relay_num,
                    "state": "ON" if state == 1 else "OFF",
                    "status": "success"
                }
                responses.append(response)
                time.sleep(0.1)
            
            print(json.dumps({"responses": responses}, indent=2))
        
        # Xử lý lệnh cấu hình
        elif "config" in command_json:
            new_config = command_json["config"]
            config.update(new_config)
            save_config(config)
            print(json.dumps({"type": "config_response", "status": "success", "config": config}, indent=2))
        
        else:
            print(json.dumps({
                "type": "error",
                "message": "Cấu trúc lệnh không hợp lệ",
                "valid_commands": [
                    {"read": {"start_address": 129, "num_registers": 8, "interval_ms": 500}},
                    {"commands": [{"relay": 1, "state": 1}, {"relay": 2, "state": 0}]},
                    {"all": 1},
                    {"config": {"read_settings": {"interval_ms": 1000}}}
                ]
            }, indent=2))
            
    except Exception as e:
        print(json.dumps({
            "type": "error",
            "message": str(e)
        }, indent=2))

def background_read(stop_event, last_values, error_count, device_config):
    """Thread đọc dữ liệu trong nền"""
    while not stop_event.is_set():
        try:
            values = read_holding_registers(SLAVE_ID, 
                                         device_config["read_settings"]["start_address"], 
                                         device_config["read_settings"]["num_registers"])
            if values:
                # Reset số lần lỗi khi đọc thành công
                error_count["count"] = 0
                
                # Chỉ in ra khi giá trị thay đổi
                if values != last_values.get("values"):
                    response = {
                        "type": "read_response",
                        "device": "BOARD_RELAY",
                        "address": device_config["read_settings"]["start_address"],
                        "values": values,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    # Xóa dòng hiện tại và in dữ liệu mới
                    print("\033[K\r" + json.dumps(response))
                    print("\033[K\rNhập lệnh (? để xem hướng dẫn): ", end="", flush=True)
                    last_values["values"] = values
            else:
                error_count["count"] += 1
                # Chỉ hiển thị cảnh báo sau 5 lần lỗi liên tiếp
                if error_count["count"] == 5:
                    print("\033[K\rCảnh báo: Không thể đọc dữ liệu. Kiểm tra kết nối.")
                    print("\033[K\rNhập lệnh (? để xem hướng dẫn): ", end="", flush=True)
            
            time.sleep(device_config["read_settings"]["interval_ms"] / 1000)
        except Exception as e:
            error_count["count"] += 1
            if error_count["count"] == 5:
                print(f"\033[K\rLỗi: {str(e)}")
                print("\033[K\rNhập lệnh (? để xem hướng dẫn): ", end="", flush=True)
            time.sleep(1)

if __name__ == "__main__":
    # Lưu trữ giá trị cuối cùng để so sánh
    last_values = {"values": None}
    
    # Đếm số lần lỗi liên tiếp
    error_count = {"count": 0}
    
    # Tạo event để dừng thread
    stop_event = threading.Event()
    
    # Khởi tạo SOFTWARE_COMMAND handler
    config = load_config()  # Load toàn bộ cấu hình
    software_handler = SoftwareCommandHandler(config)
    
    # Khởi động thread đọc dữ liệu
    read_thread = threading.Thread(target=background_read, args=(stop_event, last_values, error_count, device_config))
    read_thread.daemon = True
    read_thread.start()
    
    print("\nChương trình đang tự động đọc dữ liệu. Nhập lệnh để điều khiển.")
    print("Nhập ? để xem hướng dẫn")
    
    try:
        while True:
            try:
                command = input("\nNhập lệnh: ")
                
                if command.strip() == "?":
                    print("\nHướng dẫn sử dụng JSON:")
                    print("1. Điều khiển relay: {\"commands\": [{\"relay\": 1, \"state\": 1}, {\"relay\": 2, \"state\": 0}]}")
                    print("2. Điều khiển tất cả: {\"all\": 1}")
                    print("3. Cấu hình: {\"config\": {\"read_settings\": {\"interval_ms\": 1000}}}")
                    continue
                
                command_json = json.loads(command)
                
                # Bỏ qua lệnh đọc vì đã có thread đọc trong nền
                if "read" in command_json:
                    print(json.dumps({
                        "type": "info",
                        "message": "Đang đọc dữ liệu tự động trong nền"
                    }, indent=2))
                    continue
                    
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
        stop_event.set()  # Dừng thread đọc
        read_thread.join(timeout=1)  # Đợi thread dừng
    finally:
        ser.close()
        print(json.dumps({
            "type": "system",
            "message": "Đã đóng cổng serial"
        }, indent=2))
