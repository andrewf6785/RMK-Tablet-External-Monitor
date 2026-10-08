@echo off
setlocal
cd /d "%~dp0"
where x86_64-w64-mingw32-gcc >nul 2>nul
if errorlevel 1 (
  echo Install an x64 MinGW-w64 toolchain and add its bin directory to PATH.
  exit /b 1
)
x86_64-w64-mingw32-windres launcher.rc -O coff -o launcher-res.o
if errorlevel 1 exit /b 1
x86_64-w64-mingw32-gcc -std=c11 -O2 -Wall -Wextra -Werror -municode -mwindows -static-libgcc -Wl,--nxcompat,--dynamicbase,--high-entropy-va launcher.c launcher-res.o -lshell32 -luser32 -lgdi32 -o ..\RemarkableMonitor.exe
if errorlevel 1 exit /b 1
del launcher-res.o
echo Built ..\RemarkableMonitor.exe
