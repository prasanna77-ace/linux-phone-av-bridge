import ssl, asyncio, socket, subprocess, os, fractions, cv2, json, time, platform, webbrowser, glob
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web, WSMsgType
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer, AudioStreamTrack
import pyvirtualcam
import numpy as np
import av
import qrcode
import pyperclip

IS_WINDOWS = platform.system() == "Windows"

if not IS_WINDOWS:
    os.environ.setdefault("DISPLAY", ":0")

uinput_device = None
if not IS_WINDOWS:
    try:
        import evdev
        from evdev import UInput, ecodes as e
        cap = {
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
            e.EV_KEY: [
                e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE,
                e.KEY_PLAYPAUSE, e.KEY_NEXTSONG, e.KEY_PREVIOUSSONG,
                e.KEY_VOLUMEUP, e.KEY_VOLUMEDOWN, e.KEY_MUTE,
                e.KEY_LEFTCTRL, e.KEY_LEFTALT, e.KEY_TAB, e.KEY_Z, e.KEY_C, e.KEY_V,
                e.KEY_LEFTMETA, e.KEY_F5, e.KEY_ESC, e.KEY_LEFT, e.KEY_RIGHT, e.KEY_UP, e.KEY_DOWN
            ]
        }
        uinput_device = UInput(cap, name="PhoneBridge-Master-Device")
    except Exception:
        uinput_device = None

from pynput.mouse import Controller as MouseController, Button
from pynput.keyboard import Controller as KeyboardController, Key
try:
    pynput_mouse = MouseController()
    pynput_keyboard = KeyboardController()
except Exception:
    pynput_mouse, pynput_keyboard = None, None

executor = ThreadPoolExecutor(max_workers=4)
pcs = set()
vcam = None
vcam_lock = asyncio.Lock()
active_tasks = set()
ws_clients = set()

mobile_telemetry = {
    "battery": "100%", "charging": False, "device": "Mobile",
    "mode": "av", "cam_active": False, "mic_active": False
}

TRANSFER_DIR = os.path.expanduser("~/Downloads/PhoneBridge_Transfers")
os.makedirs(TRANSFER_DIR, exist_ok=True)

VCAM_WIDTH = 1280
VCAM_HEIGHT = 720
VCAM_FPS = 30

def get_all_lan_ips():
    """Extracts all physical and wireless IPv4 addresses."""
    ip_list = []
    try:
        # Standard UDP socket probe
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('1.1.1.1', 80))
        primary = s.getsockname()[0]
        ip_list.append(primary)
        s.close()
    except Exception:
        pass

    try:
        # Linux hostname query
        out = subprocess.check_output("hostname -I", shell=True).decode().split()
        for ip in out:
            if ip not in ip_list and not ip.startswith("127.") and not ip.startswith("172.17.") and not ip.startswith("docker"):
                ip_list.append(ip)
    except Exception:
        pass

    return ip_list if ip_list else ["127.0.0.1"]

ALL_IPS = get_all_lan_ips()
PRIMARY_IP = ALL_IPS[0]

def get_sys_clipboard():
    try: return pyperclip.paste()
    except Exception: return ""

def set_sys_clipboard(text):
    try: pyperclip.copy(text)
    except Exception: pass

def make_standby_frame(w=VCAM_WIDTH, h=VCAM_HEIGHT, text="PhoneBridge Standby"):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (10, 15, 29)
    cv2.putText(img, text, (int(w*0.24), int(h*0.48)), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (56, 189, 248), 2, cv2.LINE_AA)
    cv2.putText(img, "Ready for WebRTC AV Transmission", (int(w*0.30), int(h*0.56)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (148, 163, 184), 1, cv2.LINE_AA)
    return img

class NonBlockingDesktopAudio(AudioStreamTrack):
    def __init__(self):
        super().__init__()
        self.rate = 48000
        self.channels = 2
        self.samples_per_frame = 960
        self.bytes_per_frame = self.samples_per_frame * self.channels * 2
        self._pts = 0
        self.proc = None
        self.reader = None
        self.pyaudio_stream = None
        self.p_instance = None

    async def init_process(self):
        if not IS_WINDOWS:
            cmd = [
                "parec", "--format=s16le", "--rate=48000", "--channels=2",
                "--raw", "--latency-msec=10", "--process-time-msec=5", "-d", "@DEFAULT_MONITOR@"
            ]
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
                )
                self.reader = self.proc.stdout
            except Exception:
                self.proc = None
        else:
            try:
                import pyaudiowpatch as pyaudio
                self.p_instance = pyaudio.PyAudio()
                wasapi_info = self.p_instance.get_host_api_info_by_type(pyaudio.paWASAPI)
                default_speakers = self.p_instance.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
                if not default_speakers["isLoopbackDevice"]:
                    for loopback in self.p_instance.get_loopback_device_info_generator():
                        if default_speakers["name"] in loopback["name"]:
                            default_speakers = loopback
                            break
                self.pyaudio_stream = self.p_instance.open(
                    format=pyaudio.paInt16,
                    channels=2,
                    rate=48000,
                    input=True,
                    input_device_index=default_speakers["index"],
                    frames_per_buffer=self.samples_per_frame
                )
            except Exception as e:
                print(f"[Audio Monitor Notice] {e}")

    async def recv(self):
        if self.proc is None and self.pyaudio_stream is None:
            await self.init_process()

        raw_data = b'\x00' * self.bytes_per_frame
        try:
            if not IS_WINDOWS and self.reader:
                raw_data = await self.reader.readexactly(self.bytes_per_frame)
            elif IS_WINDOWS and self.pyaudio_stream:
                loop = asyncio.get_running_loop()
                raw_data = await loop.run_in_executor(executor, self.pyaudio_stream.read, self.samples_per_frame, False)
        except Exception:
            raw_data = b'\x00' * self.bytes_per_frame

        frame = av.AudioFrame(format='s16', layout='stereo', samples=self.samples_per_frame)
        frame.planes[0].update(raw_data)
        frame.sample_rate = self.rate
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, self.rate)
        self._pts += self.samples_per_frame
        return frame

    def stop(self):
        super().stop()
        if self.proc:
            try: self.proc.terminate()
            except Exception: pass
        if self.pyaudio_stream:
            try:
                self.pyaudio_stream.stop_stream()
                self.pyaudio_stream.close()
            except Exception: pass
        if self.p_instance:
            try: self.p_instance.terminate()
            except Exception: pass

async def handle_index(request):
    if os.path.exists("index.html"): return web.FileResponse("index.html")
    return web.Response(text="PhoneBridge Core Running")

async def handle_phone(request):
    if os.path.exists("phone.html"): return web.FileResponse("phone.html")
    return web.Response(text="PhoneBridge Controller Ready")

async def handle_manifest(request):
    if os.path.exists("manifest.json"): return web.FileResponse("manifest.json")
    return web.Response(status=404)

async def handle_sw(request):
    if os.path.exists("sw.js"): return web.FileResponse("sw.js", headers={"Content-Type": "application/javascript"})
    return web.Response(status=404)

async def get_status(request):
    files = [{"name": f, "size": os.path.getsize(os.path.join(TRANSFER_DIR, f))} for f in os.listdir(TRANSFER_DIR) if os.path.isfile(os.path.join(TRANSFER_DIR, f))]
    return web.json_response({
        "status": "ready",
        "lan_ip": PRIMARY_IP,
        "all_ips": ALL_IPS,
        "host": f"{PRIMARY_IP}:8443",
        "active_connections": len(pcs) + len(ws_clients),
        "files": files,
        "save_path": TRANSFER_DIR,
        "telemetry": mobile_telemetry,
        "clipboard": get_sys_clipboard(),
        "platform": platform.system(),
        "input_backend": "kernel_uinput" if uinput_device else "pynput"
    })

def inject_mouse_rel(dx, dy):
    if uinput_device:
        from evdev import ecodes as e
        uinput_device.write(e.EV_REL, e.REL_X, int(dx))
        uinput_device.write(e.EV_REL, e.REL_Y, int(dy))
        uinput_device.syn()
    elif pynput_mouse:
        pynput_mouse.move(dx, dy)

def inject_mouse_click(btn_str):
    if uinput_device:
        from evdev import ecodes as e
        btn = e.BTN_LEFT if btn_str == "l" else (e.BTN_RIGHT if btn_str == "r" else e.BTN_MIDDLE)
        uinput_device.write(e.EV_KEY, btn, 1)
        uinput_device.syn()
        uinput_device.write(e.EV_KEY, btn, 0)
        uinput_device.syn()
    elif pynput_mouse:
        btn = Button.left if btn_str == "l" else (Button.right if btn_str == "r" else Button.middle)
        pynput_mouse.click(btn)

def inject_hotkey(combo):
    if pynput_keyboard:
        if combo == "ctrl_c":
            pynput_keyboard.press(Key.ctrl); pynput_keyboard.press('c'); pynput_keyboard.release('c'); pynput_keyboard.release(Key.ctrl)
        elif combo == "ctrl_v":
            pynput_keyboard.press(Key.ctrl); pynput_keyboard.press('v'); pynput_keyboard.release('v'); pynput_keyboard.release(Key.ctrl)
        elif combo == "ctrl_z":
            pynput_keyboard.press(Key.ctrl); pynput_keyboard.press('z'); pynput_keyboard.release('z'); pynput_keyboard.release(Key.ctrl)
        elif combo == "alt_tab":
            pynput_keyboard.press(Key.alt); pynput_keyboard.press(Key.tab); pynput_keyboard.release(Key.tab); pynput_keyboard.release(Key.alt)
        elif combo == "super":
            pynput_keyboard.press(Key.cmd); pynput_keyboard.release(Key.cmd)
        elif combo == "f5":
            pynput_keyboard.press(Key.f5); pynput_keyboard.release(Key.f5)
        elif combo == "esc":
            pynput_keyboard.press(Key.esc); pynput_keyboard.release(Key.esc)
        elif combo == "left":
            pynput_keyboard.press(Key.left); pynput_keyboard.release(Key.left)
        elif combo == "right":
            pynput_keyboard.press(Key.right); pynput_keyboard.release(Key.right)

async def websocket_input_handler(request):
    global mobile_telemetry
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.add(ws)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                act = data.get("a")

                if act == "mm":
                    sens = data.get("sens", 1.5)
                    inject_mouse_rel(data.get("dx", 0) * sens, data.get("dy", 0) * sens)
                elif act == "gyro":
                    sens = data.get("sens", 2.2)
                    inject_mouse_rel(data.get("gx", 0) * sens, data.get("gy", 0) * sens)
                elif act == "c":
                    inject_mouse_click(data.get("b", "l"))
                elif act == "sc":
                    dy = int(data.get("dy", 0))
                    if uinput_device:
                        from evdev import ecodes as e
                        uinput_device.write(e.EV_REL, e.REL_WHEEL, dy)
                        uinput_device.syn()
                    elif pynput_mouse:
                        pynput_mouse.scroll(0, dy)
                elif act == "drag_start" and pynput_mouse:
                    pynput_mouse.press(Button.left)
                elif act == "drag_end" and pynput_mouse:
                    pynput_mouse.release(Button.left)

                elif act == "hotkey":
                    inject_hotkey(data.get("k"))
                elif act == "t" and pynput_keyboard:
                    pynput_keyboard.type(data.get("txt", ""))
                elif act == "macro":
                    m = data.get("cmd")
                    if m == "play_pause" and pynput_keyboard: pynput_keyboard.press(Key.media_play_pause); pynput_keyboard.release(Key.media_play_pause)
                    elif m == "next" and pynput_keyboard: pynput_keyboard.press(Key.media_next); pynput_keyboard.release(Key.media_next)
                    elif m == "prev" and pynput_keyboard: pynput_keyboard.press(Key.media_previous); pynput_keyboard.release(Key.media_previous)
                    elif m == "vol_up" and pynput_keyboard: pynput_keyboard.press(Key.media_volume_up); pynput_keyboard.release(Key.media_volume_up)
                    elif m == "vol_down" and pynput_keyboard: pynput_keyboard.press(Key.media_volume_down); pynput_keyboard.release(Key.media_volume_down)
                    elif m == "lock":
                        if IS_WINDOWS: subprocess.Popen("rundll32.exe user32.dll,LockWorkStation", shell=True)
                        else: subprocess.Popen("loginctl lock-session 2>/dev/null || xflock4 2>/dev/null || true", shell=True)

                elif act == "set_clip":
                    set_sys_clipboard(data.get("text", ""))
                elif act == "get_clip":
                    await ws.send_json({"a": "clip_data", "text": get_sys_clipboard()})

                elif act == "phone_control":
                    for client in ws_clients:
                        if client != ws and not client.closed:
                            await client.send_json(data)

                elif act == "telemetry":
                    mobile_telemetry.update({
                        "battery": f"{data.get('battery', 100)}%",
                        "charging": data.get("charging", False),
                        "device": data.get("device", "Mobile"),
                        "mode": data.get("mode", "av"),
                        "cam_active": data.get("cam_active", False),
                        "mic_active": data.get("mic_active", False)
                    })
                    for client in ws_clients:
                        if client != ws and not client.closed:
                            await client.send_json({"a": "telemetry_update", **data})
    finally:
        ws_clients.discard(ws)
    return ws

async def upload_file(request):
    reader = await request.multipart()
    uploaded = []
    while True:
        part = await reader.next()
        if part is None: break
        if part.filename:
            safe_name = os.path.basename(part.filename)
            filepath = os.path.abspath(os.path.join(TRANSFER_DIR, safe_name))
            if not filepath.startswith(os.path.abspath(TRANSFER_DIR)): continue
            with open(filepath, 'wb', buffering=4*1024*1024) as f:
                while True:
                    chunk = await part.read_chunk(size=1024*1024)
                    if not chunk: break
                    f.write(chunk)
            uploaded.append(safe_name)
    return web.json_response({"status": "uploaded", "files": uploaded, "dir": TRANSFER_DIR})

async def list_files(request):
    files = [{"name": f, "size": os.path.getsize(os.path.join(TRANSFER_DIR, f))} for f in os.listdir(TRANSFER_DIR) if os.path.isfile(os.path.join(TRANSFER_DIR, f))]
    return web.json_response(files)

async def download_file(request):
    safe_name = os.path.basename(request.match_info.get('filename', ''))
    filepath = os.path.abspath(os.path.join(TRANSFER_DIR, safe_name))
    if filepath.startswith(os.path.abspath(TRANSFER_DIR)) and os.path.isfile(filepath):
        return web.FileResponse(filepath)
    return web.Response(status=404)

async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    enable_pc_to_phone = params.get("enable_pc_to_phone", True)

    ice_servers = [RTCIceServer(urls=["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"])]
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
    pcs.add(pc)

    audio_track = None
    if enable_pc_to_phone:
        audio_track = NonBlockingDesktopAudio()
        pc.addTrack(audio_track)

    @pc.on("track")
    def on_track(track):
        if track.kind == "video":
            task = asyncio.create_task(handle_video(track, pc))
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)
        elif track.kind == "audio":
            task = asyncio.create_task(handle_mic(track, pc))
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

    @pc.on("connectionstatechange")
    async def on_state_change():
        if pc.connectionState in ["failed", "closed"]:
            if audio_track: audio_track.stop()
            await pc.close()
            pcs.discard(pc)
            async with vcam_lock:
                if vcam: vcam.send(make_standby_frame())

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    for _ in range(25):
        if pc.iceGatheringState == "complete": break
        await asyncio.sleep(0.04)

    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})

def process_frame(frame, w_target, h_target):
    img = frame.to_ndarray(format="bgr24")
    h, w, _ = img.shape
    if w != w_target or h != h_target:
        img = cv2.resize(img, (w_target, h_target), interpolation=cv2.INTER_LINEAR)
    return img

async def handle_video(track, pc):
    global vcam
    loop = asyncio.get_running_loop()

    while pc.connectionState not in ["failed", "closed"]:
        try:
            frame = await track.recv()
            img = await loop.run_in_executor(executor, process_frame, frame, VCAM_WIDTH, VCAM_HEIGHT)
            async with vcam_lock:
                if vcam is not None: vcam.send(img)
        except Exception: break

async def handle_mic(track, pc):
    resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
    loop = asyncio.get_running_loop()
    
    if not IS_WINDOWS:
        cmd = ["pacat", "--playback", "-d", "PhoneMicEngine", "--rate=48000", "--channels=1", "--format=s16le", "--raw", "--latency-msec=10", "--process-time-msec=5"]
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            while pc.connectionState not in ["failed", "closed"]:
                frame = await track.recv()
                for resampled in resampler.resample(frame):
                    raw_bytes = resampled.to_ndarray().tobytes()
                    try:
                        proc.stdin.write(raw_bytes)
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError): break
        except Exception: pass
        finally:
            try: proc.terminate()
            except Exception: pass
    else:
        import pyaudio
        p = pyaudio.PyAudio()
        stream = p.open(format=pyaudio.paInt16, channels=1, rate=48000, output=True)
        try:
            while pc.connectionState not in ["failed", "closed"]:
                frame = await track.recv()
                for resampled in resampler.resample(frame):
                    raw_bytes = resampled.to_ndarray().tobytes()
                    await loop.run_in_executor(executor, stream.write, raw_bytes)
        except Exception: pass
        finally:
            stream.stop_stream(); stream.close(); p.terminate()

def find_v4l2_device():
    devices = sorted(glob.glob('/dev/video*'))
    if '/dev/video10' in devices: return '/dev/video10'
    for d in devices:
        if d != '/dev/video0': return d
    return '/dev/video0' if devices else '/dev/video10'

async def init_virtual_camera():
    global vcam
    device_path = find_v4l2_device()
    try:
        if IS_WINDOWS:
            vcam = pyvirtualcam.Camera(width=VCAM_WIDTH, height=VCAM_HEIGHT, fps=VCAM_FPS, backend='obs', fmt=pyvirtualcam.PixelFormat.BGR)
        else:
            vcam = pyvirtualcam.Camera(width=VCAM_WIDTH, height=VCAM_HEIGHT, fps=VCAM_FPS, device=device_path, fmt=pyvirtualcam.PixelFormat.BGR)
        vcam.send(make_standby_frame())
    except Exception as e:
        print(f"[VirtualCam Init Notice] Camera bypassed ({e})")

@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS": response = web.Response()
    else: response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

app = web.Application(client_max_size=1024**3 * 10, middlewares=[cors_middleware])
app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response())
app.router.add_get("/", handle_index)
app.router.add_get("/phone.html", handle_phone)
app.router.add_get("/manifest.json", handle_manifest)
app.router.add_get("/sw.js", handle_sw)
app.router.add_get("/api/status", get_status)
app.router.add_get("/ws/input", websocket_input_handler)
app.router.add_post("/api/upload", upload_file)
app.router.add_get("/api/files", list_files)
app.router.add_get("/api/files/{filename}", download_file)
app.router.add_post("/offer", offer)

if __name__ == "__main__":
    local_dashboard = f"https://{PRIMARY_IP}:8443/"
    phone_url = f"https://{PRIMARY_IP}:8443/phone.html"

    try: pyperclip.copy(local_dashboard)
    except Exception: pass

    qr = qrcode.QRCode()
    qr.add_data(phone_url)
    qr.make()

    print("\n" + "═"*74)
    print("      PHONE-TO-PC MASTER RELAY — ACTIVE INTERFACES")
    print("═"*74)
    for idx, ip in enumerate(ALL_IPS):
        print(f" [{idx+1}] Interface IP: https://{ip}:8443/phone.html")
    print(f"\n👉 Primary Phone URL:\n   {phone_url}\n")
    print("👉 Terminal QR Code for Primary IP:")
    qr.print_ascii(invert=True)
    print("═"*74 + "\n")

    try: webbrowser.open(local_dashboard)
    except Exception: pass

    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_virtual_camera())

    ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_ctx.load_cert_chain(certfile="cert.pem", keyfile="key.pem")
    web.run_app(app, host="0.0.0.0", port=8443, ssl_context=ssl_ctx)
