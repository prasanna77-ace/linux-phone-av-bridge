#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

# 1. Ensure kernel modules and virtual devices exist
sudo modprobe snd-aloop 2>/dev/null || true
sudo modprobe v4l2loopback video_nr=10 card_label="PhoneWebcam" exclusive_caps=1 2>/dev/null || true
sudo chmod 666 /dev/video10 2>/dev/null || true

# 2. Configure Virtual Mic Nodes
pactl list short modules | grep -E "VirtualMic|Phone_Mic|PhoneMic" | awk '{print $1}' | xargs -r -n1 pactl unload-module 2>/dev/null || true
pactl load-module module-null-sink sink_name=PhoneMicEngine sink_properties='device.description="PhoneMicEngine_Internal" media.class="Audio/Sink"' >/dev/null
pactl load-module module-remap-source master=PhoneMicEngine.monitor source_name=Phone_Microphone source_properties='device.description="Phone_Microphone" device.class="sound" device.form_factor="microphone" device.icon_name="audio-input-microphone" device.intended_roles="input" node.description="Phone_Microphone"' >/dev/null
pactl set-sink-mute PhoneMicEngine 0 2>/dev/null || true
pactl set-source-mute Phone_Microphone 0 2>/dev/null || true
pactl set-default-source Phone_Microphone 2>/dev/null || true

# 3. Generate self-signed SSL certificate if missing
if [ ! -f "cert.pem" ] || [ ! -f "key.pem" ]; then
    echo "[*] Generating local self-signed SSL certificates..."
    openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=PhoneBridge" 2>/dev/null
fi

# 4. Launch Bridge Server
./venv/bin/python3 server.py
