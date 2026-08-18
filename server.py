import ssl, asyncio, socket, subprocess, os, fractions, cv2, json, time, platform, glob
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

TOTAL_SCREEN_W, TOTAL_SCREEN_H = 1920, 1080
if not IS_WINDOWS:
    try:
        xrandr_out = subprocess.check_output("xrandr --current 2>/dev/null | grep 'current' | head -n1", shell=True).decode()
        parts = xrandr_out.split("current")[1].split(",")[0].strip().split("x")
        TOTAL_SCREEN_W, TOTAL_SCREEN_H = int(parts[0].strip()), int(parts[1].strip())
    except Exception:
        TOTAL_SCREEN_W, TOTAL_SCREEN_H = 1920, 1080

uinput_mouse = None
if not IS_WINDOWS:
    try:
        import evdev
        from evdev import UInput, ecodes as e
        cap_mouse = {
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL],
            e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE]
        }
        uinput_mouse = UInput(cap_mouse, name="PhoneBridge-Mouse")
    except Exception:
        uinput_mouse = None

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

TRANSFER_DIR = os.path.expanduser("~/Downloads/PhoneBridge_Transfers")
os.makedirs(TRANSFER_DIR, exist_ok=True)

VCAM_WIDTH, VCAM_HEIGHT, VCAM_FPS = 1280, 720, 30

telemetry_state = {
    "cam_active": False,
    "mic_active": False,
    "pc_audio_active": False,
    "resolution": "1280x720",
    "rtt": 0,
    "battery": 100
}

def get_default_monitor_source():
    if IS_WINDOWS: return ""
    try:
        sink = subprocess.check_output("pactl get-default-sink 2>/dev/null || true", shell=True).decode().strip()
        if sink: return f"{sink}.monitor"
    except Exception: pass
    return "@DEFAULT_MONITOR@"

def get_all_lan_ips():
    ip_list = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('1.1.1.1', 80))
        primary = s.getsockname()[0]
        ip_list.append(primary)
        s.close()
    except Exception: pass

    try:
        out = subprocess.check_output("hostname -I 2>/dev/null", shell=True).decode().split()
        for ip in out:
            if ip not in ip_list and not ip.startswith("127.") and not ip.startswith("172.17."):
                ip_list.append(ip)
    except Exception: pass
    return ip_list if ip_list else ["127.0.0.1"]

ALL_IPS = get_all_lan_ips()
PRIMARY_IP = ALL_IPS[0]

def make_standby_frame(w=VCAM_WIDTH, h=VCAM_HEIGHT):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (20, 30, 48)
    cv2.putText(img, "PhoneBridge Ready", (int(w*0.30), int(h*0.48)), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (56, 189, 248), 2, cv2.LINE_AA)
    return img

class NonBlockingDesktopAudio(AudioStreamTrack):
    def __init__(self):
        super().__init__()
        self.rate = 48000
        self.samples_per_frame = 960
        self.bytes_per_frame = self.samples_per_frame * 2 * 2
        self._pts = 0
        self.proc = None

    async def init_process(self):
        if not IS_WINDOWS:
            source = get_default_monitor_source()
            cmd = ["parec", "--format=s16le", "--rate=48000", "--channels=2", "--raw", "--latency-msec=10", "-d", source]
            try:
                self.proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                telemetry_state["pc_audio_active"] = True
            except Exception:
                self.proc = None
                telemetry_state["pc_audio_active"] = False

    async def recv(self):
        if self.proc is None and not IS_WINDOWS:
            await self.init_process()

        raw_data = b'\x00' * self.bytes_per_frame
        try:
            if self.proc and self.proc.stdout:
                raw_data = await self.proc.stdout.readexactly(self.bytes_per_frame)
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
        telemetry_state["pc_audio_active"] = False
        if self.proc:
            try: self.proc.terminate()
            except Exception: pass

def inject_mouse_rel(dx, dy):
    if uinput_mouse:
        from evdev import ecodes as e
        uinput_mouse.write(e.EV_REL, e.REL_X, int(dx))
        uinput_mouse.write(e.EV_REL, e.REL_Y, int(dy))
        uinput_mouse.syn()
    elif pynput_mouse:
        pynput_mouse.move(dx, dy)

def inject_mouse_scroll(dy):
    if uinput_mouse:
        from evdev import ecodes as e
        uinput_mouse.write(e.EV_REL, e.REL_WHEEL, int(dy))
        uinput_mouse.syn()
    elif pynput_mouse:
        pynput_mouse.scroll(0, dy)

def inject_mouse_click(btn_str):
    if uinput_mouse:
        from evdev import ecodes as e
        btn = e.BTN_LEFT if btn_str == "l" else e.BTN_RIGHT
        uinput_mouse.write(e.EV_KEY, btn, 1); uinput_mouse.syn()
        uinput_mouse.write(e.EV_KEY, btn, 0); uinput_mouse.syn()
    elif pynput_mouse:
        pynput_mouse.click(Button.left if btn_str == "l" else Button.right)

def inject_keyboard_text(text):
    if pynput_keyboard and text:
        pynput_keyboard.type(text)

def inject_keyboard_key(key_code):
    if not pynput_keyboard: return
    key_map = {
        "enter": Key.enter, "backspace": Key.backspace, "tab": Key.tab,
        "esc": Key.esc, "space": Key.space, "up": Key.up, "down": Key.down,
        "left": Key.left, "right": Key.right
    }
    if key_code in key_map:
        pynput_keyboard.press(key_map[key_code])
        pynput_keyboard.release(key_map[key_code])
    elif key_code == "ctrl_c":
        pynput_keyboard.press(Key.ctrl); pynput_keyboard.press('c'); pynput_keyboard.release('c'); pynput_keyboard.release(Key.ctrl)
    elif key_code == "ctrl_v":
        pynput_keyboard.press(Key.ctrl); pynput_keyboard.press('v'); pynput_keyboard.release('v'); pynput_keyboard.release(Key.ctrl)

async def websocket_input_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.add(ws)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                act = data.get("a")

                if act == "ping":
                    await ws.send_json({"a": "pong", "ts": data.get("ts", 0)})
                elif act == "mm":
                    inject_mouse_rel(data.get("dx", 0) * 1.5, data.get("dy", 0) * 1.5)
                elif act == "sc":
                    inject_mouse_scroll(data.get("dy", 0))
                elif act == "c":
                    inject_mouse_click(data.get("b", "l"))
                elif act == "type":
                    inject_keyboard_text(data.get("text", ""))
                elif act == "key":
                    inject_keyboard_key(data.get("k", ""))
                elif act == "telemetry":
                    telemetry_state["cam_active"] = data.get("cam", False)
                    telemetry_state["mic_active"] = data.get("mic", False)
                    telemetry_state["resolution"] = data.get("res", "1280x720")
                    telemetry_state["battery"] = data.get("batt", 100)
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
            with open(filepath, 'wb') as f:
                while True:
                    chunk = await part.read_chunk(size=1024*1024)
                    if not chunk: break
                    f.write(chunk)
            uploaded.append(safe_name)
    return web.json_response({"status": "uploaded", "files": uploaded})

async def list_files(request):
    files = []
    for f in os.listdir(TRANSFER_DIR):
        p = os.path.join(TRANSFER_DIR, f)
        if os.path.isfile(p):
            files.append({"name": f, "size": os.path.getsize(p), "url": f"/api/download/{f}"})
    return web.json_response({"files": files, "count": len(files)})

async def download_file(request):
    filename = os.path.basename(request.match_info['filename'])
    filepath = os.path.join(TRANSFER_DIR, filename)
    if os.path.exists(filepath):
        return web.FileResponse(filepath, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    return web.Response(status=404, text="File not found")

async def delete_file(request):
    filename = os.path.basename(request.match_info['filename'])
    filepath = os.path.join(TRANSFER_DIR, filename)
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
            return web.json_response({"status": "deleted", "name": filename})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)
    return web.Response(status=404, text="File not found")

async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    ice_servers = [RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
    pcs.add(pc)

    audio_track = NonBlockingDesktopAudio()
    pc.addTrack(audio_track)

    @pc.on("track")
    def on_track(track):
        if track.kind == "video":
            task = asyncio.create_task(handle_video_stream(track, pc))
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)
        elif track.kind == "audio":
            task = asyncio.create_task(handle_mic(track, pc))
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

    @pc.on("connectionstatechange")
    async def on_state_change():
        if pc.connectionState in ["failed", "closed"]:
            audio_track.stop()
            await pc.close()
            pcs.discard(pc)
            async with vcam_lock:
                if vcam: vcam.send(make_standby_frame())

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})

def process_and_send_frame(frame, target_w, target_h):
    global vcam
    try:
        src_img = frame.to_ndarray(format="bgr24")
        src_h, src_w, _ = src_img.shape

        if src_w != target_w or src_h != target_h:
            scale = min(target_w / src_w, target_h / src_h)
            new_w, new_h = int(src_w * scale), int(src_h * scale)
            resized = cv2.resize(src_img, (new_w, new_h), interpolation=cv2.INTER_AREA)

            canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
            y_off = (target_h - new_h) // 2
            x_off = (target_w - new_w) // 2
            canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
            output_bgr = np.ascontiguousarray(canvas)
        else:
            output_bgr = np.ascontiguousarray(src_img)

        if vcam is not None:
            vcam.send(output_bgr)
    except Exception:
        pass

async def handle_video_stream(track, pc):
    telemetry_state["cam_active"] = True
    loop = asyncio.get_running_loop()
    while pc.connectionState not in ["failed", "closed"]:
        try:
            frame = await track.recv()
            await loop.run_in_executor(executor, process_and_send_frame, frame, VCAM_WIDTH, VCAM_HEIGHT)
        except Exception:
            break
    telemetry_state["cam_active"] = False
    if vcam: vcam.send(make_standby_frame())

async def handle_mic(track, pc):
    telemetry_state["mic_active"] = True
    resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
    if not IS_WINDOWS:
        cmd = ["pacat", "--playback", "-d", "PhoneMicEngine", "--rate=48000", "--channels=1", "--format=s16le", "--raw", "--latency-msec=10"]
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            while pc.connectionState not in ["failed", "closed"]:
                frame = await track.recv()
                for resampled in resampler.resample(frame):
                    proc.stdin.write(resampled.to_ndarray().tobytes())
                    await proc.stdin.drain()
        except Exception: pass
        finally:
            if proc:
                try: proc.terminate()
                except Exception: pass
    telemetry_state["mic_active"] = False

def init_virtual_camera():
    global vcam
    device_path = '/dev/video10'
    try:
        vcam = pyvirtualcam.Camera(
            width=VCAM_WIDTH, height=VCAM_HEIGHT, fps=VCAM_FPS,
            device=device_path, fmt=pyvirtualcam.PixelFormat.BGR
        )
        vcam.send(make_standby_frame())
    except Exception as e:
        print(f"[VirtualCam Init Notice] {e}")

app = web.Application()
app.router.add_get("/", lambda r: web.FileResponse("index.html") if os.path.exists("index.html") else web.Response(text="PhoneBridge Core Running"))
app.router.add_get("/phone.html", lambda r: web.FileResponse("phone.html"))
app.router.add_get("/manifest.json", lambda r: web.FileResponse("manifest.json") if os.path.exists("manifest.json") else web.Response(status=404))
app.router.add_get("/api/status", lambda r: web.json_response({"status": "ready", "lan_ip": PRIMARY_IP, "all_ips": ALL_IPS, "telemetry": telemetry_state}))
app.router.add_get("/api/files", list_files)
app.router.add_get("/api/download/{filename}", download_file)
app.router.add_delete("/api/files/{filename}", delete_file)
app.router.add_post("/api/upload", upload_file)
app.router.add_get("/ws/input", websocket_input_handler)
app.router.add_post("/offer", offer)

if __name__ == "__main__":
    init_virtual_camera()
    phone_url = f"https://{PRIMARY_IP}:8443/phone.html"
    
    print("\n" + "═"*65)
    print("   ⚡ PHONEBRIDGE (AV • INPUT • FILE VAULT)")
    print("═"*65)
    print(f"👉 Mobile URL: {phone_url}")
    print(f"👉 PC Hub:    https://{PRIMARY_IP}:8443/\n")
    
    qr = qrcode.QRCode()
    qr.add_data(phone_url)
    qr.make()
    qr.print_ascii(invert=True)
    print("═"*65 + "\n")

    ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_ctx.load_cert_chain(certfile="cert.pem", keyfile="key.pem")

    try:
        web.run_app(app, host="0.0.0.0", port=8443, ssl_context=ssl_ctx)
    finally:
        if uinput_mouse: uinput_mouse.close()
