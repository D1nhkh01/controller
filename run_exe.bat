REM run_exe.bat - Script de chay VM2030Controller.exe
@echo off
chcp 65001 >nul
echo ========================================
echo    VM2030 Controller Launcher
echo ========================================
echo.
echo Chon phien ban de chay:
echo   1. VM2030Controller.exe (An console)
echo   2. VM2030Controller_Console.exe (Hien thi console)
echo.
choice /c 12 /m "Nhap lua chon (1 hoac 2)"

if %errorlevel%==1 (
    set "EXE_FILE=VM2030Controller.exe"
    echo Khoi dong VM2030Controller ^(an console^)...
) else (
    set "EXE_FILE=VM2030Controller_Console.exe"
    echo Khoi dong VM2030Controller ^(hien thi console^)...
)

REM Kiem tra file .exe co ton tai khong
if not exist "dist\%EXE_FILE%" (
    echo Khong tim thay %EXE_FILE%
    echo    Hay chay build_exe.py truoc de tao file .exe
    pause
    exit /b 1
)

REM Chuyen den thu muc dist va chay
cd dist
echo.
%EXE_FILE%

REM Xử lý lỗi nếu có
if %ERRORLEVEL% neq 0 (
    echo.
    echo ❌ Controller đã dừng với lỗi: %ERRORLEVEL%
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ✅ Controller đã dừng bình thường
pause
