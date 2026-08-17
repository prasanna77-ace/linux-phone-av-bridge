#!/bin/bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "===================================================================="
echo "    PHONE-TO-PC MASTER ECOSYSTEM — ZERO-FAILURE ENGINE"
echo "===================================================================="

# 1. Unblock Firewall for Port 8443
if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow 8443/tcp 2>/dev/null || true
fi

# 2. Linux Kernel Modules, Virtual Devices & Audio Sinks
if [ "$(uname)" = "Linux" ]; then
    # Virtual Camera Loopback Module
    if ! lsmod | grep -q v4l2loopback; then
        echo "[*] Loading v4l2loopback module..."
        sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="PhoneWebcam" exclusive_caps=1 2>/dev/null || true
    fi

    # PulseAudio / PipeWire Virtual Microphone Sink
    if command -v pactl >/dev/null 2>&1; then
        if ! pactl list sinks short | grep -q "PhoneMicEngine"; then
            echo "[*] Initializing PhoneMicEngine virtual audio sink..."
            pactl load-module module-null-sink sink_name=PhoneMicEngine sink_properties=device.description="PhoneMicEngine" 2>/dev/null || true
        fi
    fi

    # Low-latency Kernel uinput permissions
    if [ -e /dev/uinput ]; then
        sudo chmod 666 /dev/uinput 2>/dev/null || true
    fi
fi

# 3. Create SSL Certificates if missing
if [ ! -f cert.pem ] || [ ! -f key.pem ]; then
    echo "[*] Generating local SSL certificates..."
    openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=PhoneBridgeMaster" 2>/dev/null
fi

# 4. Auto-setup virtualenv & dependencies
if [ ! -d "venv" ]; then
    echo "[*] Setting up dedicated Python virtual environment..."
    python3 -m venv venv
    ./venv/bin/pip install --upgrade pip --quiet
    ./venv/bin/pip install aiohttp aiortc pyvirtualcam opencv-python av qrcode pyperclip pynput numpy evdev --quiet
fi

# 5. Launch Master Server
echo "[✓] Launching Master Controller Engine..."
./venv/bin/python3 server.py
