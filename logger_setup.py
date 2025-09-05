# logger_setup.py
import logging, os, socket, uuid, sys

try:
    import seqlog
    _HAS_SEQ = True
except Exception:
    _HAS_SEQ = False

APP_NAME     = os.getenv("APP_NAME", "VM2030Controller")
APP_VERSION  = os.getenv("APP_VERSION", "1.0.0")
SEQ_URL      = os.getenv("SEQ_URL", "https://seq-lab.digitalfactory.vn")
SEQ_API_KEY  = os.getenv("SEQ_API_KEY", "")  # API key de trong theo admin
LOG_LEVEL    = os.getenv("LOG_LEVEL", "INFO").upper()
SIGNAL_TYPE  = os.getenv("SIGNAL_TYPE", "vm2030_controller")

class _ContextFilter(logging.Filter):
    def __init__(self, service: str, version: str, session_id: str, signal_type: str):
        super().__init__()
        self.service = service
        self.version = version
        self.session = session_id
        self.signal_type = signal_type
        self.host    = socket.gethostname()
        self.pid     = os.getpid()
    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self.service
        record.version = self.version
        record.session = self.session
        record.signal_type = self.signal_type
        record.Signal = self.signal_type  # Property chính cho Seq Signal filtering
        record.host    = self.host
        record.pid     = self.pid
        record.Environment = "Development"  # Môi trường
        record.Component = "VM2030Controller"  # Component
        record.Application = "Controller"  # Application group
        record.DeviceType = "VM2030LaserMarker"  # Device type
        return True

def _setup_console(level: str, ctx_filter: logging.Filter):
    root = logging.getLogger()
    root.setLevel(level)
    h = logging.StreamHandler(sys.stdout)
    
    # Set encoding to UTF-8 to handle Vietnamese characters
    if hasattr(h.stream, 'reconfigure'):
        h.stream.reconfigure(encoding='utf-8')
    
    fmt = "[%(asctime)s] %(levelname)s %(service)s/%(version)s " \
          "Signal=%(Signal)s App=%(Application)s Device=%(DeviceType)s session=%(session)s :: %(message)s"
    h.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    h.addFilter(ctx_filter)
    root.addHandler(h)

def _setup_seq(level: str, ctx_filter: logging.Filter):
    # Gắn seqlog vào root logger
    try:
        print(f"DEBUG: Setting up Seq with URL: {SEQ_URL}")
        
        # Sử dụng seqlog.log_to_seq với cách đúng
        seqlog.log_to_seq(
            server_url=SEQ_URL,
            api_key=SEQ_API_KEY if SEQ_API_KEY else None,
            level=getattr(logging, level.upper()),
            batch_size=10,
            auto_flush_timeout=1,
            override_root_logger=True
        )
        
        # Thêm global properties theo Seq Signals best practices
        # Signal là identifier chính để group và filter logs theo hướng dẫn Seq
        seqlog.set_global_log_properties(
            # Core Signal properties - chính để filtering trong Seq
            Signal=SIGNAL_TYPE,  # Primary signal: vm2030_controller
            Application="IndustrialController",  # Application group
            Component="VM2030Controller",  # Specific component
            
            # Infrastructure properties
            Host=socket.gethostname(),
            Environment="Development",  # Production/Staging/Development
            Version=APP_VERSION,
            
            # Domain-specific properties cho VM2030 controller
            DeviceType="VM2030LaserMarker",  # Loại thiết bị điều khiển
            Protocol="ModbusRTU+ASCII",  # Giao thức sử dụng
            ConnectionType="Serial+ZeroMQ",  # Phương thức kết nối
            ProcessType="Controller"  # Loại process
        )
        
        print(f"DEBUG: Seq setup completed with Signal={SIGNAL_TYPE}")
        print(f"DEBUG: Use 'Signal == \"{SIGNAL_TYPE}\"' in Seq queries to filter VM2030 Controller logs")
        print(f"DEBUG: Available filters: Application='IndustrialController', Component='VM2030Controller', DeviceType='VM2030LaserMarker'")
    except Exception as e:
        print(f"DEBUG: Seq setup failed: {e}")
        raise e
    # Thêm context filter vào mọi handler hiện có
    for h in logging.getLogger().handlers:
        h.addFilter(ctx_filter)

def setup_logging(level: str | None = None, session_id: str | None = None) -> logging.Logger:
    """
    Gọi thật sớm trong __main__. Trả về logger app.
    """
    level = (level or LOG_LEVEL).upper()
    session_id = session_id or uuid.uuid4().hex[:12]
    ctx_filter = _ContextFilter(APP_NAME, APP_VERSION, session_id, SIGNAL_TYPE)

    if _HAS_SEQ:
        try:
            _setup_seq(level, ctx_filter)
        except Exception:
            _setup_console(level, ctx_filter)
    else:
        _setup_console(level, ctx_filter)

    # Bắt uncaught exception → log lên Seq/console
    app_logger = logging.getLogger(APP_NAME)
    def _excepthook(exc_type, exc, tb):
        app_logger.exception("Uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = _excepthook

    # Serial/ZMQ exception hook cho controller
    app_logger.setLevel(level)
    
    # Kiểm tra thực tế Seq có hoạt động không
    seq_enabled = _HAS_SEQ and any(isinstance(h, seqlog.SeqLogHandler) for h in logging.getLogger().handlers)
    
    app_logger.info("VM2030 Controller logging initialized - SEQ: %s, URL: %s, Level: %s, Session: %s, Signal: %s", 
                    seq_enabled, SEQ_URL, level, session_id, SIGNAL_TYPE)
    app_logger.info("Signal filter configured for Seq: Signal=%s (use this in Seq filtering)", SIGNAL_TYPE)
    return app_logger

def get_logger(name: str | None = None) -> logging.Logger:
    """
    Lấy logger theo module. Gọi sau khi setup_logging().
    """
    return logging.getLogger(name or APP_NAME)

# Helper functions để log structured data cho VM2030 operations
def log_vm2030_command(logger: logging.Logger, command: str, job_number: int = None, **kwargs):
    """Log VM2030 command với structured data"""
    logger.info("VM2030 command executed", extra={
        "VM2030Command": command,
        "JobNumber": job_number,
        "CommandType": "VM2030Operation",
        **kwargs
    })

def log_relay_operation(logger: logging.Logger, relay_id: int, state: str, **kwargs):
    """Log relay operation với structured data"""
    logger.info("Relay operation", extra={
        "RelayID": relay_id,
        "RelayState": state,
        "OperationType": "RelayControl",
        **kwargs
    })

def log_zmq_request(logger: logging.Logger, command: str, message_id: str, payload_size: int = None, **kwargs):
    """Log ZMQ request với structured data"""
    logger.info("ZMQ request received", extra={
        "ZMQCommand": command,
        "MessageID": message_id,
        "PayloadSize": payload_size,
        "RequestType": "ZMQOperation",
        **kwargs
    })

def log_serial_error(logger: logging.Logger, device: str, error_type: str, error_msg: str, **kwargs):
    """Log serial communication errors với structured data"""
    logger.error("Serial communication error", extra={
        "Device": device,
        "ErrorType": error_type,
        "ErrorMessage": error_msg,
        "OperationType": "SerialError",
        **kwargs
    })
