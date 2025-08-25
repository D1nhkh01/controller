import json
import serial
import time
from datetime import datetime

class SoftwareCommandHandler:
    def __init__(self, config):
        self.config = config
        self.last_states = {
            'Ready': None,
            'Home': None,
            'Reset': None
        }
        self.initialize_serial()

    def initialize_serial(self):
        """Khởi tạo kết nối serial cho SOFTWARE_COMMAND"""
        try:
            sw_config = self.config['devices']['SOFTWARE_COMMAND']
            self.ser = serial.Serial(
                port=sw_config['com_port'],
                baudrate=sw_config['baud_rate'],
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1
            )
            print(f"Kết nối tới SOFTWARE_COMMAND ({sw_config['com_port']}) thành công.")
        except Exception as e:
            print(f"Lỗi kết nối SOFTWARE_COMMAND: {e}")
            self.ser = None

    def process_relay_data(self, relay_data):
        """Xử lý dữ liệu từ BOARD_RELAY và tạo lệnh tương ứng"""
        if not self.ser:
            return

        try:
            values = relay_data.get('values', [])
            if len(values) < 3:
                return

            current_states = {
                'Ready': values[0],
                'Home': values[1],
                'Reset': values[2]
            }

            commands = []
            # Kiểm tra từng trạng thái và tạo lệnh nếu có thay đổi
            for signal, value in current_states.items():
                if value != self.last_states[signal]:
                    command = {
                        "type": "command",
                        "device": "SOFTWARE_COMMAND",
                        "signal": signal,
                        "value": value,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    commands.append(command)
                    self.last_states[signal] = value

            # Gửi lệnh nếu có thay đổi
            if commands:
                response = {
                    "type": "software_commands",
                    "commands": commands
                }
                # Gửi lệnh qua cổng COM
                command_str = json.dumps(response) + "\n"
                self.ser.write(command_str.encode())
                print(f"\nGửi lệnh tới SOFTWARE_COMMAND: {json.dumps(response, indent=2)}")

        except Exception as e:
            print(f"\nLỗi xử lý dữ liệu SOFTWARE_COMMAND: {e}")

    def close(self):
        """Đóng kết nối serial"""
        if self.ser:
            self.ser.close()
