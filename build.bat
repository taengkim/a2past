@echo off
REM Net Tuner 빌드 스크립트
REM PyInstaller 필요: pip install pyinstaller

pyinstaller --onefile --windowed --uac-admin --name NetTuner net_tuner.py

echo.
echo 빌드 완료: dist\NetTuner.exe
pause
