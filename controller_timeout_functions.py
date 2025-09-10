# controller_timeout_functions.py
"""
Các hàm timeout từ controller.py - Documentation và Usage
"""

import time
import threading
from typing import Dict, Any, Tuple

# =============================================================================
# CÁC HÀM TIMEOUT HIỆN TẠI TRONG CONTROLLER
# =============================================================================

def sc_wait_complete(timeout_ms: int) -> Dict[str, Any]:
    """
    Đợi completion signal (0x1F) từ VM2030 trong thời gian timeout
    
    Args:
        timeout_ms: Timeout in milliseconds
        
    Returns:
        {"ok": True, "code": 0x1F} nếu thành công
        {"ok": False, "code": last_code} nếu timeout
        
    Usage:
        result = sc_wait_complete(20000)  # Đợi 20 giây
        if result["ok"]:
            print("Command completed successfully")
        else:
            print(f"Timeout! Last code: {result['code']}")
    """
    end = time.time() + (timeout_ms/1000.0)
    last = None
    while time.time() < end:
        # with sc_rx_lock:
        #     last = _last_status_code["code"]
        # if last == 0x1F:
        #     return {"ok": True, "code": last}
        time.sleep(0.02)
    return {"ok": False, "code": last}

def sc_read_until_complete_collect(timeout_ms: int) -> bytes:
    """
    Thu thập toàn bộ data từ VM2030 cho đến khi nhận completion signal hoặc timeout
    
    Args:
        timeout_ms: Timeout in milliseconds
        
    Returns:
        bytes: Data đã nhận (không bao gồm 0x1F completion byte)
        
    Usage:
        data = sc_read_until_complete_collect(15000)  # Đợi 15 giây
        if data:
            print(f"Received {len(data)} bytes: {data.hex()}")
    """
    end = time.time() + (timeout_ms/1000.0)
    # with sc_rx_lock:
    #     if _last_status_code["code"] == 0x1F:
    #         data = bytes(_sc_rx_buffer)
    #         _sc_rx_buffer.clear()
    #         return data
    #     while True:
    #         remaining = end - time.time()
    #         if remaining <= 0:
    #             data = bytes(_sc_rx_buffer)
    #             _sc_rx_buffer.clear()
    #             return data
    #         sc_rx_cv.wait(timeout=remaining)
    #         if _last_status_code["code"] == 0x1F:
    #             data = bytes(_sc_rx_buffer)
    #             _sc_rx_buffer.clear()
    return b""  # Placeholder

def sc_read_two_segments_for_get_job(total_timeout_ms: int) -> Tuple[bytes, bytes]:
    """
    Đọc 2 segments cho GET_JOB command với timeout phân chia thông minh
    
    Args:
        total_timeout_ms: Tổng timeout cho cả 2 segments
        
    Returns:
        (header_bytes, body_bytes): Tuple chứa 2 segments
        
    Usage:
        header, body = sc_read_two_segments_for_get_job(20000)
        if header and body:
            print(f"Header: {len(header)} bytes, Body: {len(body)} bytes")
    """
    # Phân chia timeout: 40% cho segment 1, 60% cho segment 2
    t1 = max(200, int(total_timeout_ms * 0.4))  # Min 200ms cho segment 1
    t2 = total_timeout_ms - t1
    
    # segment1 = sc_read_until_complete_collect(t1)
    # segment2 = sc_read_until_complete_collect(t2)
    
    return (b"", b"")  # Placeholder

def exec_sc_operation(op_id: str, command: str, raw: bytes, source: str, 
                     meta: Dict = None, wait: bool = True) -> Dict[str, Any]:
    """
    Thực thi VM2030 operation với timeout và error handling
    
    Args:
        op_id: Operation ID để tracking
        command: Command name (SET_JOB, START_JOB, etc.)
        raw: Raw command bytes
        source: Source của command ("ui", "input_edge", etc.)
        meta: Metadata dictionary
        wait: Có đợi completion không
        
    Returns:
        {"ok": True, "code": completion_code} nếu thành công
        {"ok": False, "isTimeout": True, ...} nếu timeout
        
    Usage:
        result = exec_sc_operation("op-123", "SET_JOB", raw_bytes, "ui", {"job_no": 15})
        if result["ok"]:
            print(f"Operation completed with code: {result['code']}")
        else:
            print(f"Operation failed: timeout={result.get('isTimeout', False)}")
    """
    meta = meta or {}
    
    # Gửi command
    # send_raw_to_software_command(raw)
    
    if not wait:
        return {"ok": True, "code": None, "note": "queued"}
    
    # Lấy timeout từ config
    # tout = int(config.get("timeouts",{}).get("ui_op_timeout_ms", 20000))
    tout = 20000  # Default 20 seconds
    
    # Đợi completion
    res = sc_wait_complete(tout)
    
    if res.get("ok"):
        return {"ok": True, "code": res["code"], "timeoutMs": tout}
    else:
        return {"ok": False, "isTimeout": True, "lastCode": res.get("code"), "timeoutMs": tout}

# =============================================================================
# TIMEOUT CONFIGURATION
# =============================================================================

def get_default_timeout_config() -> Dict[str, Any]:
    """
    Lấy default timeout configuration từ controller
    
    Returns:
        Dict chứa timeout settings
    """
    return {
        "timeouts": {
            "ui_op_timeout_ms": 20000,      # 20 giây cho UI operations
            "relay_timeout_ms": 5000,       # 5 giây cho relay operations
            "serial_read_timeout_s": 1,     # 1 giây cho serial read
            "queue_get_timeout_s": 0.2,     # 200ms cho queue get
        },
        "zeromq": {
            "rcv_timeout_ms": 1000,         # 1 giây cho ZMQ receive
            "snd_timeout_ms": 1000,         # 1 giây cho ZMQ send
        }
    }

# =============================================================================
# TIMEOUT PATTERNS VÀ BEST PRACTICES
# =============================================================================

def timeout_patterns_examples():
    """
    Các patterns timeout thường gặp trong controller
    """
    
    # Pattern 1: Simple wait với timeout
    def simple_wait_pattern(timeout_ms: int):
        """Đợi condition với timeout đơn giản"""
        end_time = time.time() + (timeout_ms / 1000.0)
        while time.time() < end_time:
            # Check condition
            # if condition_met():
            #     return True
            time.sleep(0.02)  # 20ms intervals
        return False
    
    # Pattern 2: Wait với condition variable
    def condition_wait_pattern(timeout_ms: int, cv: threading.Condition):
        """Đợi với condition variable và timeout"""
        end_time = time.time() + (timeout_ms / 1000.0)
        with cv:
            while time.time() < end_time:
                remaining = end_time - time.time()
                if remaining <= 0:
                    break
                cv.wait(timeout=remaining)
                # Check condition after wakeup
                # if condition_met():
                #     return True
        return False
    
    # Pattern 3: Progressive timeout
    def progressive_timeout_pattern(base_timeout_ms: int, retries: int):
        """Timeout tăng dần qua các lần retry"""
        for attempt in range(retries):
            timeout = base_timeout_ms * (2 ** attempt)  # Exponential backoff
            success = simple_wait_pattern(timeout)
            if success:
                return True
        return False
    
    # Pattern 4: Segment-based timeout
    def segmented_timeout_pattern(total_timeout_ms: int, segments: list):
        """Chia timeout cho nhiều segments"""
        remaining = total_timeout_ms
        results = []
        
        for i, segment_weight in enumerate(segments):
            if i == len(segments) - 1:  # Last segment gets all remaining time
                segment_timeout = remaining
            else:
                segment_timeout = int(total_timeout_ms * segment_weight)
                remaining -= segment_timeout
            
            # Execute segment with timeout
            success = simple_wait_pattern(segment_timeout)
            results.append(success)
            
            if not success:
                break
                
        return all(results)

# =============================================================================
# USAGE EXAMPLES
# =============================================================================

def example_timeout_usage():
    """
    Ví dụ sử dụng các hàm timeout trong thực tế
    """
    
    print("=== TIMEOUT FUNCTION EXAMPLES ===")
    
    # 1. Basic operation với timeout
    print("\n1. Basic VM2030 Operation:")
    result = exec_sc_operation(
        op_id="example-001",
        command="HOME", 
        raw=b"%H\n\r",
        source="ui",
        wait=True
    )
    print(f"Result: {result}")
    
    # 2. GET_JOB với segmented timeout
    print("\n2. GET_JOB with Segmented Timeout:")
    header, body = sc_read_two_segments_for_get_job(15000)
    print(f"Header size: {len(header)}, Body size: {len(body)}")
    
    # 3. Wait completion với custom timeout
    print("\n3. Wait Completion:")
    completion = sc_wait_complete(10000)  # 10 seconds
    if completion["ok"]:
        print(f"Completed with code: 0x{completion['code']:02X}")
    else:
        print(f"Timeout! Last code: {completion['code']}")
    
    # 4. Timeout configuration
    print("\n4. Timeout Configuration:")
    config = get_default_timeout_config()
    ui_timeout = config["timeouts"]["ui_op_timeout_ms"]
    print(f"UI Operation Timeout: {ui_timeout}ms ({ui_timeout/1000}s)")

# =============================================================================
# TIMEOUT MONITORING VÀ DEBUGGING
# =============================================================================

def create_timeout_monitor(operation_name: str, timeout_ms: int):
    """
    Tạo timeout monitor để debug timeout issues
    
    Returns:
        Dict chứa monitoring functions
    """
    start_time = time.time()
    checkpoints = []
    
    def add_checkpoint(name: str, data: Any = None):
        """Thêm checkpoint với timestamp"""
        elapsed = (time.time() - start_time) * 1000
        checkpoints.append({
            "name": name,
            "elapsed_ms": elapsed,
            "data": data,
            "remaining_ms": timeout_ms - elapsed
        })
    
    def get_summary():
        """Lấy summary của timeout monitoring"""
        total_elapsed = (time.time() - start_time) * 1000
        return {
            "operation": operation_name,
            "timeout_ms": timeout_ms,
            "total_elapsed_ms": total_elapsed,
            "is_timeout": total_elapsed >= timeout_ms,
            "checkpoints": checkpoints,
            "checkpoint_count": len(checkpoints)
        }
    
    # Add initial checkpoint
    add_checkpoint("start", {"timeout_ms": timeout_ms})
    
    return {
        "add_checkpoint": add_checkpoint,
        "get_summary": get_summary,
        "start_time": start_time
    }

if __name__ == "__main__":
    example_timeout_usage()
