#!/bin/bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "======================================================"
echo "    PHONE-TO-PC AV BRIDGE — ENHANCED LAUNCHER"
echo "======================================================"

# 1. Allow port 8443 in ufw if active
if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow 8443/tcp 2>/dev/null || true
fi

# 2. Setup virtual video loopback on Linux if not present
if [ "$(uname)" = "Linux" ]; then
    if ! lsmod | grep -q v4l2loopback; then
        echo "[*] Initializing virtual camera module (v4l2loopback)..."
        sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="PhoneWebcam" exclusive_caps=1 2>/dev/null || true
    fi
    # Setup virtual audio sink for microphone routing
    pactl load-module module-null-sink sink_name=PhoneMicEngine sink_properties=device.description="PhoneMicEngine" 2>/dev/null || true
fi

# 3. Auto-generate SSL certificates if missing
if [ ! -f cert.pem ] || [ ! -f key.pem ]; then
    echo "[*] Creating local secure SSL certificate..."
    openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=PhoneBridge" 2>/dev/null
fi

# 4. Create virtualenv and install dependencies automatically
if [ ! -d "venv" ]; then
    echo "[*] Setting up Python environment..."
    python3 -m venv venv
    ./venv/bin/pip install --upgrade pip --quiet
    ./venv/bin/pip install aiohttp aiortc pyvirtualcam opencv-python av qrcode pyperclip pynput numpy --quiet
fi

# 5. Launch engine
echo "[✓] Starting server..."
./venv/bin/python3 server.py
