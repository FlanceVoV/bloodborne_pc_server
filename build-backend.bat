@echo off
setlocal
cd /d "%~dp0"
where git >nul 2>nul || goto tools
where cmake >nul 2>nul || goto tools
if not exist "third_party\shadnet-p2p\.git" git clone --recursive https://github.com/Wozzardman/shadnet-p2p.git third_party\shadnet-p2p
if errorlevel 1 goto fail
git -C third_party\shadnet-p2p checkout 5a849a192fd4450593c57b10697316ac324c856e
if errorlevel 1 goto fail
git -C third_party\shadnet-p2p submodule update --init --recursive
if errorlevel 1 goto fail
cmake -S third_party\shadnet-p2p -B third_party\shadnet-p2p\build -DCMAKE_BUILD_TYPE=Release
if errorlevel 1 goto fail
cmake --build third_party\shadnet-p2p\build --config Release --parallel
if errorlevel 1 goto fail
echo Build complete. Set shadnet.executable and shadnet.enabled in config.json.
exit /b 0
:tools
echo Git and CMake are required. Install Qt 6 Core, Network, SQL, Concurrent and HttpServer first.
:fail
echo shadNet build failed. See README.md for prerequisites.
pause
exit /b 1
