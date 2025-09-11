# README_EXE.md - Hướng dẫn sử dụng file .exe

# VM2030 Controller - File .exe

## Tạo file .exe

### Cách 1: Sử dụng script tự động

```bash
python build_exe.py
```

### Cách 2: Sử dụng PyInstaller trực tiếp

```bash
# Cài đặt PyInstaller nếu chưa có
pip install pyinstaller

# Tạo file .exe
pyinstaller --onefile --name "VM2030Controller" --add-data "device_config.json;." --add-data "job_store.json;." controller.py
```

## Chạy file .exe

### Cách 1: Sử dụng batch file

```bash
run_exe.bat
```

### Cách 2: Chạy trực tiếp

```bash
cd dist
VM2030Controller.exe
```

## Các file quan trọng

- `VM2030Controller.exe` - File chính để chạy controller
- `device_config.json` - Cấu hình thiết bị (được embed trong .exe)
- `job_store.json` - Lưu trữ job (được embed trong .exe)

## Yêu cầu hệ thống

- Windows 7/8/10/11 (64-bit)
- Không cần cài đặt Python
- Không cần virtual environment
- Port COM khả dụng cho kết nối thiết bị

## Tính năng

✅ Hoàn toàn standalone - không cần cài đặt thêm gì
✅ Tích hợp đầy đủ VM2030 Controller
✅ Seq logging tự động
✅ ZMQ server tích hợp
✅ Modbus RTU communication
✅ Relay control

## Khắc phục sự cố

### File .exe không chạy được

1. Kiểm tra Windows Defender/Antivirus có block không
2. Chạy với quyền Administrator
3. Kiểm tra file có bị corrupt không (tải lại)

### Lỗi kết nối COM port

1. Kiểm tra device có được kết nối không
2. Kiểm tra driver COM port
3. Chạy Device Manager để xem port khả dụng

### Lỗi ZMQ connection

1. Kiểm tra port 5555 có bị chiếm không
2. Kiểm tra firewall settings
3. Test với telnet: `telnet localhost 5555`

## Cấu hình

Các biến môi trường có thể set trước khi chạy:

- `SEQ_URL` - URL của Seq server
- `LOG_LEVEL` - Mức độ log (DEBUG, INFO, WARNING, ERROR)
- `APP_VERSION` - Phiên bản ứng dụng

Ví dụ:

```cmd
set SEQ_URL=https://your-seq-server.com
set LOG_LEVEL=DEBUG
VM2030Controller.exe
```
