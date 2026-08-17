#!/bin/bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "===================================================================="
echo "    PHONE-TO-PC AV BRIDGE — FULL THROTTLE SUITE (BI-DIRECTIONAL)"
echo "===================================================================="

# 1. Allow port 8443 in ufw firewall
if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow 8443/tcp 2>/dev/null || true
fi

# 2. Linux kernel loopback & virtual audio nodes
if [ "$(uname)" = "Linux" ]; then
    if ! lsmod | grep -q v4l2loopback; then
        echo "[*] Loading v4l2loopback virtual camera module..."
        sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="PhoneWebcam" exclusive_caps=1 2>/dev/null || true
    fi
    # Ensure PulseAudio/PipeWire PhoneMicEngine sink exists for phone mic routing
    pactl load-module module-null-sink sink_name=PhoneMicEngine sink_properties=device.description="PhoneMicEngine" 2>/dev/null || true
fi

# 3. Create SSL Certificates if missing
if [ ! -f cert.pem ] || [ ! -f key.pem ]; then
    echo "[*] Generating local zero-trust SSL certificates..."
    openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=PhoneBridge" 2>/dev/null
fi

# 4. Auto-setup virtualenv & dependencies
if [ ! -d "venv" ]; then
    echo "[*] Initializing dedicated Python virtual environment..."
    python3 -m venv venv
    ./venv/bin/pip install --upgrade pip --quiet
    ./venv/bin/pip install aiohttp aiortc pyvirtualcam opencv-python av qrcode pyperclip pynput numpy --quiet
fi

# 5. Run headless server
echo "[✓] Launching core engine..."
./venv/bin/python3 server.py
