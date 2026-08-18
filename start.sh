#!/bin/bash
set -e

# 1. Allow port 8443 through firewall automatically
sudo ufw allow 8443/tcp >/dev/null 2>&1 || true

# 2. Initialize virtual camera device (/dev/video10)
echo "[*] Setting up virtual camera (/dev/video10)..."
sudo modprobe -r v4l2loopback 2>/dev/null || true
sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="PhoneWebcam" exclusive_caps=1

# 3. Setup virtual audio input source
echo "[*] Setting up Phone HD Microphone..."
pactl unload-module module-remap-source 2>/dev/null || true
pactl unload-module module-null-sink 2>/dev/null || true
pactl load-module module-null-sink sink_name=PhoneMicEngine sink_properties=device.description="PhoneMicEngine_Sink" >/dev/null
pactl load-module module-remap-source source_name=PhoneMic master=PhoneMicEngine.monitor source_properties=device.description="Phone_HD_Microphone" >/dev/null

# 4. Auto-open local dashboard in default browser after 1 second
(sleep 1.2 && xdg-open "https://localhost:8443/" 2>/dev/null || true) &

# 5. Run PhoneBridge server
echo "[*] Starting server..."
./venv/bin/python3 server.py
