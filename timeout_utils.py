# timeout_utils.py
"""
Timeout utilities cho VM2030 Controller
Các hàm tính toán và quản lý timeout khi gửi lệnh và chờ hoàn thành
"""

import time
from typing import Dict, Any, Optional, Callable

def get_timeout_config(config: Dict[str, Any]) -> Dict[str, int]:
    """
    Lấy cấu hình timeout từ config
    
    Returns:
        Dict chứa các timeout values (milliseconds)
    """
    return {
        "ui_op_timeout_ms": config.get("timeouts", {}).get("ui_op_timeout_ms", 20000),
        "zmq_rcv_timeout_ms": config.get("zeromq", {}).get("rcv_timeout_ms", 1000),
        "zmq_snd_timeout_ms": config.get("zeromq", {}).get("snd_timeout_ms", 1000),
        "serial_timeout_ms": 1000,  # Serial timeout cố định
        "get_job_timeout_ms": config.get("timeouts", {}).get("ui_op_timeout_ms", 20000),
        "sequence_timeout_ms": config.get("timeouts", {}).get("ui_op_timeout_ms", 20000) * 2  # Sequence cần thời gian dài hơn
    }

def calculate_dynamic_timeout(command: str, payload: Dict[str, Any] = None) -> int:
    """
    Tính timeout động dựa trên loại command
    
    Args:
        command: Tên command (SET_JOB, START_JOB, etc.)
        payload: Payload của command
        
    Returns:
        Timeout in milliseconds
    """
    payload = payload or {}
    
    # Base timeouts cho từng loại command
    base_timeouts = {
        "HOME": 5000,           # HOME nhanh
        "PING": 2000,           # PING rất nhanh
        "GET_STATUS": 3000,     # Status check nhanh
        "SET_JOB": 8000,        # SET_JOB trung bình
        "GET_JOB": 10000,       # GET_JOB có thể chậm do đọc data
        "START_JOB": 15000,     # START_JOB cần thời gian marking
        "START_SEQUENCE": 30000, # SEQUENCE có thể rất lâu
        "UPLOAD_ALL": 25000,    # UPLOAD_ALL đọc nhiều data
        "TOGGLE_ECHO": 3000,    # Echo setting nhanh
    }
    
    base_timeout = base_timeouts.get(command, 20000)  # Default 20s
    
    # Điều chỉnh timeout dựa trên payload
    if command == "SET_JOB" and payload:
        # Timeout tăng theo độ dài character string
        char_string = payload.get("characterString", "")
        char_count = len(char_string)
        # Thêm 100ms per character
        base_timeout += char_count * 100
        
    elif command == "START_SEQUENCE" and payload:
        # Timeout tăng theo số lượng jobs trong sequence
        # Giả sử có thông tin về sequence complexity
        sequence_jobs = payload.get("estimated_jobs", 1)
        base_timeout = max(30000, sequence_jobs * 5000)
        
    elif command == "GET_JOB" and payload:
        # GET_JOB có thể chậm với job có nhiều data
        job_index = payload.get("index", 1)
        # Jobs cao hơn có thể có nhiều data hơn
        if job_index > 50:
            base_timeout += 2000
            
    return min(base_timeout, 60000)  # Max 60s

def create_timeout_tracker():
    """
    Tạo timeout tracker để theo dõi thời gian thực thi
    
    Returns:
        Dict chứa timeout tracking functions
    """
    start_time = time.time()
    
    def get_elapsed_ms() -> int:
        """Lấy thời gian đã trôi qua (ms)"""
        return int((time.time() - start_time) * 1000)
    
    def get_remaining_ms(timeout_ms: int) -> int:
        """Lấy thời gian còn lại (ms)"""
        elapsed = get_elapsed_ms()
        remaining = timeout_ms - elapsed
        return max(0, remaining)
    
    def is_timeout(timeout_ms: int) -> bool:
        """Kiểm tra có timeout không"""
        return get_elapsed_ms() >= timeout_ms
    
    def wait_with_callback(timeout_ms: int, callback: Callable[[], bool], 
                          check_interval_ms: int = 50) -> bool:
        """
        Đợi với callback check
        
        Args:
            timeout_ms: Timeout milliseconds
            callback: Function return True nếu condition met
            check_interval_ms: Interval giữa các lần check
            
        Returns:
            True nếu callback return True trước timeout
        """
        check_interval_s = check_interval_ms / 1000.0
        
        while not is_timeout(timeout_ms):
            if callback():
                return True
            time.sleep(check_interval_s)
            
        return False
    
    return {
        "get_elapsed_ms": get_elapsed_ms,
        "get_remaining_ms": get_remaining_ms, 
        "is_timeout": is_timeout,
        "wait_with_callback": wait_with_callback,
        "start_time": start_time
    }

def format_timeout_info(elapsed_ms: int, timeout_ms: int, command: str) -> Dict[str, Any]:
    """
    Format timeout information cho logging
    
    Args:
        elapsed_ms: Thời gian đã trôi qua
        timeout_ms: Timeout setting
        command: Command name
        
    Returns:
        Dict chứa timeout info
    """
    return {
        "command": command,
        "elapsed_ms": elapsed_ms,
        "timeout_ms": timeout_ms,
        "remaining_ms": max(0, timeout_ms - elapsed_ms),
        "is_timeout": elapsed_ms >= timeout_ms,
        "elapsed_percent": round((elapsed_ms / timeout_ms) * 100, 1) if timeout_ms > 0 else 100
    }

def adaptive_timeout_for_sequence(sequence_commands: list, base_timeout_ms: int = 30000) -> int:
    """
    Tính timeout thích ứng cho sequence dựa trên các commands trong sequence
    
    Args:
        sequence_commands: List các commands trong sequence
        base_timeout_ms: Base timeout
        
    Returns:
        Adaptive timeout in milliseconds
    """
    if not sequence_commands:
        return base_timeout_ms
    
    # Estimate timeout dựa trên loại commands
    estimated_time = 0
    
    for cmd in sequence_commands:
        if isinstance(cmd, str):
            cmd_name = cmd
            cmd_payload = {}
        elif isinstance(cmd, dict):
            cmd_name = cmd.get("command", "UNKNOWN")
            cmd_payload = cmd.get("payload", {})
        else:
            continue
            
        cmd_timeout = calculate_dynamic_timeout(cmd_name, cmd_payload)
        estimated_time += cmd_timeout
    
    # Thêm buffer 20% cho sequence overhead
    adaptive_timeout = int(estimated_time * 1.2)
    
    # Giới hạn min/max
    adaptive_timeout = max(base_timeout_ms, adaptive_timeout)
    adaptive_timeout = min(120000, adaptive_timeout)  # Max 2 phút
    
    return adaptive_timeout

# Example usage functions
def example_usage():
    """Ví dụ sử dụng timeout utilities"""
    
    # 1. Lấy timeout config
    config = {
        "timeouts": {"ui_op_timeout_ms": 25000},
        "zeromq": {"rcv_timeout_ms": 1500, "snd_timeout_ms": 1500}
    }
    timeouts = get_timeout_config(config)
    print("Timeout config:", timeouts)
    
    # 2. Tính dynamic timeout
    set_job_timeout = calculate_dynamic_timeout("SET_JOB", {
        "characterString": "Hello World Test Message"
    })
    print(f"SET_JOB timeout: {set_job_timeout}ms")
    
    # 3. Sử dụng timeout tracker
    tracker = create_timeout_tracker()
    
    # Simulate some work
    time.sleep(0.1)
    
    elapsed = tracker["get_elapsed_ms"]()
    print(f"Elapsed: {elapsed}ms")
    
    # 4. Format timeout info
    info = format_timeout_info(elapsed, 5000, "TEST_COMMAND")
    print("Timeout info:", info)
    
    # 5. Adaptive sequence timeout
    sequence = [
        {"command": "SET_JOB", "payload": {"characterString": "Test"}},
        {"command": "START_JOB", "payload": {"index": 1}},
        {"command": "HOME", "payload": {}}
    ]
    seq_timeout = adaptive_timeout_for_sequence(sequence)
    print(f"Sequence timeout: {seq_timeout}ms")

if __name__ == "__main__":
    example_usage()
