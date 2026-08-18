#!/bin/bash
set -e

echo "[*] Initializing virtual camera device (/dev/video10)..."
sudo modprobe -r v4l2loopback 2>/dev/null || true
sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="PhoneWebcam" exclusive_caps=1

echo "[*] Setting up isolated microphone input (PhoneMic)..."
pactl unload-module module-remap-source 2>/dev/null || true
pactl unload-module module-null-sink 2>/dev/null || true
pactl load-module module-null-sink sink_name=PhoneMicEngine sink_properties=device.description="PhoneMicEngine_Sink" >/dev/null
pactl load-module module-remap-source source_name=PhoneMic master=PhoneMicEngine.monitor source_properties=device.description="Phone_HD_Microphone" >/dev/null

echo "[*] Launching PhoneBridge Core Server..."
./venv/bin/python3 server.py
