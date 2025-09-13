# VM2030 Controller

## Mục đích

Điều khiển máy khắc laser VM2030 qua cổng nối tiếp (BOARD_RELAY, SOFTWARE_COMMAND) và giao tiếp ZeroMQ (port 5555). Hỗ trợ chạy thực tế với hardware hoặc dry-run mô phỏng.

## Tính năng chính

- Điều khiển VM2030 và relay qua Modbus RTU
- Giao tiếp UI qua ZeroMQ REP (port 5555)
- Structured logging lên Seq server
- Quản lý job khắc với lưu trữ tự động
- Hỗ trợ dry-run (mô phỏng không cần hardware)

## Yêu cầu hệ thống

- Python 3.11 trở lên
- Windows 10/11 (khuyến nghị)
- Đã cài driver cho thiết bị nối tiếp (nếu cần)

## Cài đặt môi trường

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Cấu hình

- Sửa file `device_config.json` để khai báo cổng COM, baudrate, v.v. cho BOARD_RELAY và SOFTWARE_COMMAND.
- Đảm bảo các file cấu hình (`device_config.json`, `job_store.json`) nằm cùng thư mục với file thực thi.

## Chạy controller

### Chạy với hardware

```powershell
python controller.py
```

### Chạy dry-run (mô phỏng)

```powershell
python controller.py --dry-run
```

## Build file .exe

```powershell
pip install pyinstaller
pyinstaller --onefile --name vm2030_controller controller.py
```

- File `.exe` sẽ nằm trong thư mục `dist/`.
- Copy kèm file cấu hình vào cùng thư mục với `.exe` khi chạy thực tế.

## Logging lên Seq

- Log sẽ tự động gửi lên server Seq theo cấu hình trong `device_config.json` hoặc biến môi trường `SEQ_URL`.
- Để kiểm tra log trên server Seq, lọc theo:
  ```
  Signal == "vm2030_controller"
  ```

## Tham số dòng lệnh

- `--dry-run` : Mô phỏng cả BOARD_RELAY và SOFTWARE_COMMAND
- `--dry-run-relay` : Mô phỏng chỉ BOARD_RELAY
- `--dry-run-command` : Mô phỏng chỉ SOFTWARE_COMMAND
- `--log-level [off|error|warn|info|debug]` : Chọn mức log
- `--seq-url URL` : Ghi đè URL Seq server
- `--config FILE` : Chỉ định file cấu hình

## Troubleshooting

### Lỗi COM port

- Kiểm tra device manager để xác định COM port đúng
- Chạy với quyền Administrator nếu cần
- Sử dụng `--dry-run` để test logic không cần phần cứng

### Lỗi Seq logging

- Kiểm tra network connection tới Seq server
- Logger tự động fallback console nếu Seq không khả dụng
- Sử dụng `--log-level debug` để xem chi tiết

### Lỗi ZeroMQ

- Kiểm tra port 5555 không bị chiếm bởi process khác
- Test bằng: `telnet localhost 5555`

## Hỗ trợ

- Kỹ thuật: D1nhkh01
- Seq server: https://seq-lab.digitalfactory.vn

---

#### Chế độ Dry Run (không cần phần cứng)- COM ports cho BOARD_RELAY và SOFTWARE_COMMAND (nếu không dùng dry run)

``````bash

# Dry run cho cả hai devices### Cài đặt dependencies

python controller.py --dry-run

### Cài đặt dependencies```bash

# Chỉ dry run BOARD_RELAY

python controller.py --dry-run-relay`````bashpip install -r requirements.txt



# Chỉ dry run SOFTWARE_COMMANDpip install -r requirements.txt````

python controller.py --dry-run-command

``````

# Dry run với debug logging

python controller.py --dry-run --log-level debug- REP (RPC) mặc định: `tcp://*:5555`

````

### Chạy controller* PUB (realtime): `tcp://*:5556`

#### Các options khác

```bash- Topic xuất bản:

# Sử dụng config file khác

python controller.py --config my_config.json#### Chế độ bình thường (sử dụng config.json)



# Override Seq server URL```bash  * `read_response`: raw PLC registers (BOARD_RELAY)

python controller.py --seq-url https://my-seq-server.com

python controller.py \* `soft_state`: summary Ready/Home/Reset (0/1)

# Xem tất cả options

python controller.py --help``` *`op_result`: kết quả các lệnh tới VM2030 (ok/timeout)

````

#### Chế độ Dry Run (không cần phần cứng)> Bạn có thể đổi endpoints trong `device_config.json` (`zeromq.rep_bind`, `zeromq.pub_bind`).

### Cấu hình device_config.json

```json````bash

{

"devices": {# Dry run cho cả hai devices## Chạy tester

    "BOARD_RELAY": {

      "com_port": "COM9",python controller.py --dry-run

      "dry_run": false

    },```bash

    "SOFTWARE_COMMAND": {

      "com_port": "COM8", # Chỉ dry run BOARD_RELAY  python tester.py --connect tcp://127.0.0.1:5555 --sub tcp://127.0.0.1:5556 <command> [args...]

      "dry_run": false

    }python controller.py --dry-run-relay```

},

"zeromq": {

    "rep_bind": "tcp://*:5555"

}# Chỉ dry run SOFTWARE_COMMANDVí dụ:

}

`````python controller.py --dry-run-command



## 📡 ZeroMQ API```bash



Controller cung cấp **ZeroMQ REP server trên port 5555 duy nhất** (Request-Reply pattern):# Dry run với debug logging# subscribe mặc định (read_response, soft_state, op_result) + gửi lệnh kiểm tra READY



### Job Operationspython controller.py --dry-run --log-level debugpython tester.py ready

```python

# GET_JOB - Lấy thông tin job từ VM2030````

{

    "command": "GET_JOB",# đọc vị trí X/Y từ relay (tuỳ config app.position)

    "payload": {"JobNumber": 5}

}#### Các options khácpython tester.py pos



# SET_JOB - Gửi job xuống VM2030````bash

{

    "command": "SET_JOB", # Sử dụng config file khác# get job số 15

    "payload": {

        "JobNumber": 20,python controller.py --config my_config.jsonpython tester.py job-get 15

        "CharacterString": "ABC",

        "Size": 2.3,

        "Speed": 500,

        "StartX": 33.5,# Override Seq server URL# set job số 15 (CharacterString="J 15", các thông số cơ bản)

        "StartY": 10.0,

        "PitchX": 2.2,python controller.py --seq-url https://my-seq-server.compython tester.py job-set 15 "J 15" --size 2.0 --dir 0 --speed 2400 --sx 0 --sy 0 --px 0 --py 0 --font FontB

        "PitchY": 0.0,

        "Direction": 1# start job 15

    }

}# Xem tất cả optionspython tester.py job-start 15



# START_JOB - Bắt đầu khắc jobpython controller.py --help

{

    "command": "START_JOB",```# builtin home/reset

    "payload": {"JobNumber": 20}

}python tester.py builtin home

`````

### Cấu hình device_config.jsonpython tester.py builtin reset

### System Operations

`python`json

# HOME - Reset về home position

{{# set sequence index=1 (chuỗi lệnh của VM2030 do bạn định nghĩa)

    "command": "HOME"

} "devices": {python tester.py seq-set 1 "%A%B%C" # ví dụ

# RESET - Reset hệ thống "BOARD_RELAY": {python tester.py seq-start 1

{

    "command": "RESET"        "com_port": "COM9",

}

      "dry_run": false# điều chỉnh log level ở server

# GET_READY_STATUS - Kiểm tra trạng thái Ready

{ },python tester.py log debug --show-prompt=true

    "command": "GET_READY_STATUS"

} "SOFTWARE_COMMAND": {```

`````

      "com_port": "COM8",

## 📁 Cấu trúc file

      "dry_run": falseTắt SUB nếu không cần:

- **`controller.py`**: Main controller với ZeroMQ server

- **`device_config.json`**: Device và timeout configuration    }

- **`job_store.json`**: Job storage (auto-generated)

- **`requirements.txt`**: Python dependencies  }```bash



## 🔧 CLI Arguments}python tester.py --no-sub ready



```bash````

python controller.py [OPTIONS]

## 📡 ZeroMQ APIĐổi topic SUB:

Options:

  --dry-run              Enable dry run for both devicesController cung cấp ZeroMQ REP server trên port 5555:```bash

  --dry-run-relay        Enable dry run for BOARD_RELAY only

  --dry-run-command      Enable dry run for SOFTWARE_COMMAND onlypython tester.py --topics soft_state op_result job-get 15

  --log-level LEVEL      Set log level (off|error|warn|info|debug)

  --seq-url URL          Override Seq server URL### Job Operations```

  --config FILE          Use custom config file

  --help                 Show help message````python

`````

# GET_JOB - Lấy thông tin job từ VM2030Timeout cho REQ/REP (ms, mặc định 15000):

## 🔧 Cấu hình nâng cao

{

### Timeout Settings

`json    "command": "GET_JOB",`bash

{

    "timeouts": {    "payload": {"JobNumber": 5}python tester.py --timeout 20000 job-start 15

        "sc_complete_ms": 5000,

        "ui_op_timeout_ms": 20000,}```

        "get_job_ms": 4000

    }

}

````# SET_JOB - Gửi job xuống VM2030## Shell tương tác



### Relay Actions{

```json

{    "command": "SET_JOB", ```bash

    "relay_actions": {

        "on_send_pulse": {"relay": 1, "pulse_ms": 1000},    "payload": {python tester.py shell

        "hold_during_op": {"relay": 2, "on": true},

        "on_complete_pulse": {"relay": 3, "pulse_ms": 200}        "JobNumber": 20,```

    }

}        "CharacterString": "ABC",

````

        "Size": 2.3,Trong shell:

## 🐛 Troubleshooting

        "Speed": 500,

### Lỗi COM port

- Kiểm tra device manager để xác định COM port đúng "StartX": 33.5,```

- Chạy với quyền Administrator nếu cần

- Sử dụng `--dry-run` để test logic không cần phần cứng "StartY": 10.0,ready

### Lỗi Seq logging "PitchX": 2.2,pos

- Kiểm tra network connection tới Seq server

- Logger tự động fallback console nếu Seq không khả dụng "PitchY": 0.0,job.get 15

- Sử dụng `--log-level debug` để xem chi tiết

        "Direction": 1job.set 15 "J 15" size=2 dir=0 speed=2400 sx=0 sy=0 px=0 py=0 font=FontB

### Lỗi ZeroMQ

- Kiểm tra port 5555 không bị chiếm bởi process khác }job.start 15

- Test bằng: `telnet localhost 5555`

- Chỉ sử dụng 1 port (5555) cho REQ-REP communication}seq.set 1 "%A%B%C"

## 📞 Hỗ trợseq.start 1

- Repository: [VM2030 Controller](https://github.com/D1nhkh01/controller)# START_JOB - Bắt đầu khắc jobbuiltin home

- Issues: Sử dụng GitHub Issues cho bug reports

- Logs: Kiểm tra Seq logs tại https://seq-lab.digitalfactory.vn{log debug showPrompt=true

## 🎯 Examples "command": "START_JOB",quit

`bash    "payload": {"JobNumber": 20}`

# Development với dry run

python controller.py --dry-run --log-level debug}

# Production với hardware```## Ghi chú

python controller.py --log-level info

# Test chỉ relay hardware

python controller.py --dry-run-command### System Operations\* `server.py` **dry_run=true** sẽ in payload và tự mô phỏng **complete (0x1F)** sau `dry_run_complete_ms`.

# Test chỉ VM2030 hardware ```python* Với phần cứng thật: đặt `dry_run=false`và cấu hình`devices.SOFTWARE_COMMAND.com_port`.

python controller.py --dry-run-relay

```# HOME - Reset về home position* Mọi lệnh REQ phải **chờ** RECV (pattern REQ/REP). `tester.py` đảm bảo tuần tự: gửi → nhận.

## ⚡ ZeroMQ Communication{\* Realtime (SUB) in ra sự kiện theo các topic, hữu ích để theo dõi thay đổi **input**/edge và **kết quả lệnh**.

**Controller chỉ sử dụng 1 port ZeroMQ:** "command": "HOME"

- **Port 5555**: REP server cho Request-Reply communication

- **Pattern**: Synchronous REQ-REP (client gửi request, chờ response)}```

- **No Publishing**: Không có PUB/SUB pattern, chỉ trả response qua REP

`````python

# Client example (Python với pyzmq)# RESET - Reset hệ thống---

import zmq

{

context = zmq.Context()

socket = context.socket(zmq.REQ)    "command": "RESET"  Nếu cần mình thêm preset “macro” các lệnh trong tester (ví dụ `demo` chạy cả chuỗi ready → set → get → start) thì đã có luôn: `python tester.py demo --num 15 --text "J 15" ...`.

socket.connect("tcp://localhost:5555")

}```

# Send request

socket.send_json({

    "command": "GET_JOB",# GET_READY_STATUS - Kiểm tra trạng thái Ready

    "payload": {"JobNumber": 5}{

})    "command": "GET_READY_STATUS"

}

# Wait for response````

response = socket.recv_json()

print(response)## 📁 Cấu trúc file

`````

- **`controller.py`**: Main controller với ZeroMQ server
- **`logger_setup.py`**: Seq logging configuration
- **`device_config.json`**: Device và timeout configuration
- **`job_store.json`**: Job storage (auto-generated)
- **`requirements.txt`**: Python dependencies

## 🔧 CLI Arguments

```bash
python controller.py [OPTIONS]

Options:
  --dry-run              Enable dry run for both devices
  --dry-run-relay        Enable dry run for BOARD_RELAY only
  --dry-run-command      Enable dry run for SOFTWARE_COMMAND only
  --log-level LEVEL      Set log level (off|error|warn|info|debug)
  --seq-url URL          Override Seq server URL
  --config FILE          Use custom config file
  --help                 Show help message
```

## 🔧 Cấu hình nâng cao

### Timeout Settings

```json
{
  "timeouts": {
    "sc_complete_ms": 5000,
    "ui_op_timeout_ms": 20000,
    "get_job_ms": 4000
  }
}
```

### Relay Actions

```json
{
  "relay_actions": {
    "on_send_pulse": { "relay": 1, "pulse_ms": 1000 },
    "hold_during_op": { "relay": 2, "on": true },
    "on_complete_pulse": { "relay": 3, "pulse_ms": 200 }
  }
}
```

## 🐛 Troubleshooting

### Lỗi COM port

- Kiểm tra device manager để xác định COM port đúng
- Chạy với quyền Administrator nếu cần
- Sử dụng `--dry-run` để test logic không cần phần cứng

### Lỗi Seq logging

- Kiểm tra network connection tới Seq server
- Logger tự động fallback console nếu Seq không khả dụng
- Sử dụng `--log-level debug` để xem chi tiết

### Lỗi ZeroMQ

- Kiểm tra port 5555 không bị chiếm bởi process khác
- Test bằng: `telnet localhost 5555`

## 📞 Hỗ trợ

- Repository: [VM2030 Controller](https://github.com/D1nhkh01/controller)
- Issues: Sử dụng GitHub Issues cho bug reports
- Logs: Kiểm tra Seq logs tại https://seq-lab.digitalfactory.vn

## 🎯 Examples

```bash
# Development với dry run
python controller.py --dry-run --log-level debug

# Production với hardware
python controller.py --log-level info

# Test chỉ relay hardware
python controller.py --dry-run-command

# Test chỉ VM2030 hardware
python controller.py --dry-run-relay
```
