cat << 'EOF' > README.md
# ⚡ Linux Phone AV Bridge

> Turn your Android phone into a high-definition Linux workstation companion over local Wi-Fi: HD Virtual Webcam & DSP Microphone (with reverse PC audio streaming), Touchpad Mouse & Keyboard input relay, and high-speed bidirectional File Drop vault.

---

## 🌟 Core Features

- **📷 Studio AV Relay:** Stream phone camera directly to `/dev/video10` (V4L2) with resolution control (1080p, 720p, 480p) and front/back lens switching. Includes hardware Acoustic Echo Cancellation (AEC) for the microphone and real-time reverse PC audio to the phone speaker.
- **🖱️ Mouse & ⌨️ Keyboard:** 120Hz precision relative touchpad with left/right click, smooth scrolling, and live keyboard typing relay directly into the focused Linux window.
- **📁 High-Speed File Vault:** Peer-to-peer browser-based file transfer between phone and PC with instant download and file deletion support.
- **📊 Master Control Dashboards:** Dedicated dashboards on both PC (`/`) and Android (`/phone.html`) for real-time hardware status, latency (RTT), and diagnostics.

---

## 🚀 Quick Start

### 1. Prerequisites (Ubuntu / Debian / Linux Mint)
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv v4l2loopback-dkms pulseaudio-utils openssl
