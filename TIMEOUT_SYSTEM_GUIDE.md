# VM2030 Controller - Timeout System Documentation

## 📋 Tổng quan

VM2030 Controller sử dụng hệ thống timeout nhiều tầng để đảm bảo reliability và responsiveness. Tài liệu này mô tả các hàm timeout và cách sử dụng.

## ⏱️ Các Hàm Timeout Chính

### 1. `sc_wait_complete(timeout_ms: int)`

**Mục đích**: Đợi completion signal (0x1F) từ VM2030

```python
# Sử dụng cơ bản
result = sc_wait_complete(20000)  # Đợi 20 giây
if result["ok"]:
    print(f"Completed with code: 0x{result['code']:02X}")
else:
    print("Timeout occurred")

# Response format
{
    "ok": True/False,
    "code": 0x1F  # Completion code
}
```

**Đặc điểm**:

- ✅ Polling mỗi 20ms để kiểm tra status
- ✅ Thread-safe với `sc_rx_lock`
- ✅ Trả về last status code khi timeout

### 2. `sc_read_until_complete_collect(timeout_ms: int)`

**Mục đích**: Thu thập data từ VM2030 cho đến completion

```python
# Đọc response data
data = sc_read_until_complete_collect(15000)
if data:
    print(f"Received {len(data)} bytes: {data.hex()}")

# Sử dụng với condition variable
with sc_rx_lock:
    data = sc_read_until_complete_collect(10000)
```

**Đặc điểm**:

- ✅ Sử dụng condition variable cho efficiency
- ✅ Auto-clear buffer khi hoàn thành
- ✅ Trả về partial data nếu timeout

### 3. `exec_sc_operation(...)`

**Mục đích**: Thực thi operation với full timeout handling

```python
# Operation với wait=True
result = exec_sc_operation(
    op_id="op-123",
    command="SET_JOB",
    raw=command_bytes,
    source="ui",
    meta={"job_number": 15},
    wait=True
)

# Response format
{
    "ok": True/False,
    "code": 0x1F,              # Success case
    "isTimeout": True,         # Timeout case
    "lastCode": 0x...,         # Last received code
    "timeoutMs": 20000,        # Actual timeout used
    "relay_errors": [...]      # Relay error info
}
```

**Đặc điểm**:

- ✅ Integrated relay error handling
- ✅ Configurable timeout từ config
- ✅ Comprehensive response với error details

## ⚙️ Timeout Configuration

### Default Settings

```python
{
    "timeouts": {
        "ui_op_timeout_ms": 20000,    # UI operations (20s)
        "relay_timeout_ms": 5000,     # Relay operations (5s)
        "serial_read_timeout_s": 1,   # Serial read (1s)
        "queue_get_timeout_s": 0.2,   # Queue operations (200ms)
    },
    "zeromq": {
        "rcv_timeout_ms": 1000,       # ZMQ receive (1s)
        "snd_timeout_ms": 1000,       # ZMQ send (1s)
    }
}
```

### Dynamic Timeout Calculation

```python
def calculate_dynamic_timeout(command: str, payload: dict = None) -> int:
    """Tính timeout dựa trên command type và payload"""

    base_timeouts = {
        "HOME": 5000,           # Quick operations
        "PING": 2000,
        "GET_STATUS": 3000,
        "SET_JOB": 8000,        # Medium operations
        "GET_JOB": 10000,
        "START_JOB": 15000,     # Slow operations
        "START_SEQUENCE": 30000,
        "UPLOAD_ALL": 25000,
    }

    # Adjust based on payload complexity
    timeout = base_timeouts.get(command, 20000)

    if command == "SET_JOB" and payload:
        char_string = payload.get("characterString", "")
        timeout += len(char_string) * 100  # 100ms per character

    return min(timeout, 60000)  # Max 60s
```

## 🔄 Timeout Patterns

### 1. Simple Wait Pattern

```python
def simple_wait(timeout_ms: int, check_condition: callable) -> bool:
    """Đợi condition với polling"""
    end_time = time.time() + (timeout_ms / 1000.0)

    while time.time() < end_time:
        if check_condition():
            return True
        time.sleep(0.02)  # 20ms intervals

    return False
```

### 2. Condition Variable Pattern

```python
def condition_wait(timeout_ms: int, cv: threading.Condition) -> bool:
    """Đợi với condition variable - hiệu quả hơn polling"""
    end_time = time.time() + (timeout_ms / 1000.0)

    with cv:
        while time.time() < end_time:
            remaining = end_time - time.time()
            if remaining <= 0:
                break
            cv.wait(timeout=remaining)
            # Check condition after wakeup
            if condition_met():
                return True
    return False
```

### 3. Segmented Timeout Pattern

```python
def segmented_operation(total_timeout_ms: int) -> tuple:
    """Chia timeout cho multiple phases"""

    # Phase 1: 40% timeout cho header
    t1 = max(200, int(total_timeout_ms * 0.4))
    header = read_header_with_timeout(t1)

    # Phase 2: 60% timeout cho body
    t2 = total_timeout_ms - t1
    body = read_body_with_timeout(t2)

    return (header, body)
```

## 🎯 Command-Specific Timeouts

### Quick Commands (< 5s)

```python
quick_commands = {
    "HOME": 5000,       # Machine homing
    "PING": 2000,       # Connectivity test
    "GET_STATUS": 3000, # Status query
    "TOGGLE_ECHO": 3000 # Echo on/off
}
```

### Medium Commands (5-15s)

```python
medium_commands = {
    "SET_JOB": 8000,    # Job upload
    "GET_JOB": 10000,   # Job download
    "START_JOB": 15000  # Job execution
}
```

### Heavy Commands (15s+)

```python
heavy_commands = {
    "START_SEQUENCE": 30000,  # Multiple job sequence
    "UPLOAD_ALL": 25000,      # Full memory dump
    "FACTORY_RESET": 45000    # System reset
}
```

## 🛠️ Timeout Monitoring

### Timeout Tracker

```python
# Tạo tracker cho operation
tracker = create_timeout_tracker()

# Thêm checkpoints
tracker["add_checkpoint"]("command_sent")
# ... perform operation ...
tracker["add_checkpoint"]("response_received")

# Lấy summary
summary = tracker["get_summary"]()
print(f"Operation took {summary['total_elapsed_ms']}ms")
```

### Timeout Analytics

```python
def analyze_timeout_performance(operation_logs: list):
    """Phân tích performance timeout"""

    timeouts = [log for log in operation_logs if log.get("is_timeout")]
    successes = [log for log in operation_logs if not log.get("is_timeout")]

    return {
        "timeout_rate": len(timeouts) / len(operation_logs),
        "avg_success_time": sum(s["elapsed_ms"] for s in successes) / len(successes),
        "avg_timeout_time": sum(t["timeout_ms"] for t in timeouts) / len(timeouts),
        "slowest_commands": sorted(successes, key=lambda x: x["elapsed_ms"], reverse=True)[:5]
    }
```

## 🐛 Troubleshooting Timeouts

### Common Timeout Issues

1. **Serial Communication**

   ```
   Issue: Serial read timeout
   Solution: Check cable connection, baud rate, parity settings
   ```

2. **VM2030 Response Delay**

   ```
   Issue: Command sent but no 0x1F completion
   Solution: Check VM2030 status, increase timeout for complex operations
   ```

3. **Relay Operation Timeout**
   ```
   Issue: Modbus relay không response
   Solution: Check Modbus slave ID, COM port, power supply
   ```

### Debug Commands

```python
# Enable timeout debugging
config["logging"]["debug_timeouts"] = True

# Monitor timeout events
def log_timeout_event(command: str, elapsed_ms: int, timeout_ms: int):
    print(f"TIMEOUT: {command} took {elapsed_ms}ms (limit: {timeout_ms}ms)")

# Check current operation timeouts
def check_current_timeouts():
    active_ops = get_active_operations()
    for op in active_ops:
        elapsed = op["elapsed_ms"]
        timeout = op["timeout_ms"]
        remaining = timeout - elapsed
        print(f"Op {op['id']}: {remaining}ms remaining")
```

## 📊 Performance Recommendations

### Timeout Tuning Guidelines

1. **Conservative Settings**: Start với timeouts cao, giảm dần based on actual performance
2. **Command-Specific**: Sử dụng dynamic timeout calculation
3. **Network Conditions**: Adjust timeouts cho slow/unreliable connections
4. **Error Recovery**: Implement exponential backoff cho retry scenarios

### Optimal Timeout Values

```python
# Production recommendations
PRODUCTION_TIMEOUTS = {
    "HOME": 3000,           # Usually completes in 1-2s
    "SET_JOB": 6000,        # Depends on job complexity
    "START_JOB": 12000,     # Depends on marking time
    "GET_JOB": 8000,        # Depends on job size
    "START_SEQUENCE": 25000, # Depends on sequence length
    "RELAY_OPERATION": 3000, # Usually quick
    "ZMQ_OPERATION": 5000   # Network dependent
}
```

## 🔍 Advanced Usage

### Custom Timeout Manager

```python
class TimeoutManager:
    def __init__(self, default_timeout_ms: int = 20000):
        self.default_timeout = default_timeout_ms
        self.command_timeouts = {}
        self.active_operations = {}

    def set_command_timeout(self, command: str, timeout_ms: int):
        """Set specific timeout cho command"""
        self.command_timeouts[command] = timeout_ms

    def start_operation(self, op_id: str, command: str) -> dict:
        """Start tracking operation với timeout"""
        timeout = self.command_timeouts.get(command, self.default_timeout)
        tracker = create_timeout_tracker()

        self.active_operations[op_id] = {
            "command": command,
            "timeout_ms": timeout,
            "tracker": tracker,
            "start_time": time.time()
        }

        return tracker

    def check_timeout(self, op_id: str) -> bool:
        """Check nếu operation đã timeout"""
        if op_id not in self.active_operations:
            return False

        op = self.active_operations[op_id]
        elapsed = (time.time() - op["start_time"]) * 1000
        return elapsed >= op["timeout_ms"]

    def complete_operation(self, op_id: str) -> dict:
        """Mark operation complete và return summary"""
        if op_id not in self.active_operations:
            return {}

        op = self.active_operations.pop(op_id)
        elapsed = (time.time() - op["start_time"]) * 1000

        return {
            "op_id": op_id,
            "command": op["command"],
            "elapsed_ms": elapsed,
            "timeout_ms": op["timeout_ms"],
            "is_timeout": elapsed >= op["timeout_ms"]
        }

# Usage
timeout_mgr = TimeoutManager()
timeout_mgr.set_command_timeout("START_SEQUENCE", 45000)

tracker = timeout_mgr.start_operation("op-123", "START_SEQUENCE")
# ... perform operation ...
summary = timeout_mgr.complete_operation("op-123")
```

Hệ thống timeout này đảm bảo VM2030 Controller hoạt động reliable và có khả năng handle các scenarios khác nhau một cách graceful. 🎯
