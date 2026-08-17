import ssl, asyncio, socket, subprocess, os, fractions, cv2, json, time, platform
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web, WSMsgType
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, AudioStreamTrack
import pyvirtualcam
import numpy as np
import av
import pyperclip
from pynput.mouse import Controller as MouseController, Button
from pynput.keyboard import Controller as KeyboardController, Key

IS_WINDOWS = platform.system() == "Windows"

if not IS_WINDOWS:
    os.environ.setdefault("DISPLAY", ":0")

try:
    mouse = MouseController()
    keyboard = KeyboardController()
except Exception:
    mouse, keyboard = None, None

executor = ThreadPoolExecutor(max_workers=4)
pcs = set()
vcam = None
vcam_lock = asyncio.Lock()
active_tasks = set()
ws_clients = set()
mobile_telemetry = {"battery": "100%", "charging": False, "device": "Mobile"}

TRANSFER_DIR = os.path.expanduser("~/Downloads/PhoneBridge_Transfers")
os.makedirs(TRANSFER_DIR, exist_ok=True)

VCAM_DEVICE = os.environ.get("VIRTUAL_CAM_DEV", "/dev/video10")
VCAM_WIDTH = 1280
VCAM_HEIGHT = 720
VCAM_FPS = 30

def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('1.1.1.1', 80))
        ip = s.getsockname()[0]
    except Exception:
        try:
            s.connect(('192.168.1.1', 80))
            ip = s.getsockname()[0]
        except Exception:
            ip = '127.0.0.1'
    finally:
        s.close()
    return ip

LAN_IP = get_lan_ip()

def get_sys_clipboard():
    try:
        return pyperclip.paste()
    except Exception:
        return ""

def set_sys_clipboard(text):
    try:
        pyperclip.copy(text)
    except Exception:
        pass

def make_standby_frame(w=VCAM_WIDTH, h=VCAM_HEIGHT, text="PhoneWebcam (Standby)"):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (15, 23, 42)
    cv2.putText(img, text, (int(w*0.25), int(h*0.48)), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (56, 189, 248), 2, cv2.LINE_AA)
    cv2.putText(img, "Ready for connection via WebRTC", (int(w*0.28), int(h*0.56)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (148, 163, 184), 1, cv2.LINE_AA)
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
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            self.reader = self.proc.stdout
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
                print(f"[Windows Audio Warning] WASAPI Loopback init failed: {e}")

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

async def get_status(request):
    files = [f for f in os.listdir(TRANSFER_DIR) if os.path.isfile(os.path.join(TRANSFER_DIR, f))]
    return web.json_response({
        "status": "ready",
        "lan_ip": LAN_IP,
        "active_connections": len(pcs) + len(ws_clients),
        "files_count": len(files),
        "save_path": TRANSFER_DIR,
        "telemetry": mobile_telemetry,
        "clipboard": get_sys_clipboard(),
        "platform": platform.system()
    })

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

                if act == "mm" and mouse:
                    sens = data.get("sens", 1.5)
                    mouse.move(data.get("dx", 0) * sens, data.get("dy", 0) * sens)
                elif act == "gyro" and mouse:
                    dx = data.get("gx", 0) * data.get("sens", 2.2)
                    dy = data.get("gy", 0) * data.get("sens", 2.2)
                    mouse.move(dx, dy)
                elif act == "c" and mouse:
                    btn = Button.left if data.get("b") == "l" else (Button.right if data.get("b") == "r" else Button.middle)
                    mouse.click(btn)
                elif act == "sc" and mouse:
                    mouse.scroll(0, data.get("dy", 0))

                elif act == "t" and keyboard:
                    keyboard.type(data.get("txt", ""))
                elif act == "k" and keyboard:
                    k = data.get("k")
                    key_map = {
                        "Backspace": Key.backspace, "Enter": Key.enter, "Space": Key.space,
                        "Tab": Key.tab, "Escape": Key.esc, "Up": Key.up, "Down": Key.down,
                        "Left": Key.left, "Right": Key.right
                    }
                    if k in key_map:
                        keyboard.press(key_map[k])
                        keyboard.release(key_map[k])

                elif act == "macro":
                    m = data.get("cmd")
                    if m == "play_pause": keyboard.press(Key.media_play_pause); keyboard.release(Key.media_play_pause)
                    elif m == "next": keyboard.press(Key.media_next); keyboard.release(Key.media_next)
                    elif m == "prev": keyboard.press(Key.media_previous); keyboard.release(Key.media_previous)
                    elif m == "vol_up": keyboard.press(Key.media_volume_up); keyboard.release(Key.media_volume_up)
                    elif m == "vol_down": keyboard.press(Key.media_volume_down); keyboard.release(Key.media_volume_down)
                    elif m == "mute": keyboard.press(Key.media_volume_mute); keyboard.release(Key.media_volume_mute)
                    elif m == "lock":
                        if IS_WINDOWS: subprocess.Popen("rundll32.exe user32.dll,LockWorkStation", shell=True)
                        else: subprocess.Popen("loginctl lock-session 2>/dev/null || xflock4 2>/dev/null || true", shell=True)
                    elif m == "screenshot":
                        from PIL import ImageGrab
                        ss = ImageGrab.grab()
                        ss.save(os.path.join(TRANSFER_DIR, f"screenshot_{int(time.time())}.png"))
                    elif m == "terminal":
                        if IS_WINDOWS: subprocess.Popen("start cmd.exe", shell=True)
                        else: subprocess.Popen("x-terminal-emulator 2>/dev/null || xterm 2>/dev/null || true", shell=True)

                elif act == "set_clip":
                    set_sys_clipboard(data.get("text", ""))
                elif act == "get_clip":
                    await ws.send_json({"a": "clip_data", "text": get_sys_clipboard()})

                elif act == "phone_control":
                    for client in ws_clients:
                        if client != ws and not client.closed:
                            await client.send_json(data)

                elif act == "telemetry":
                    mobile_telemetry = {
                        "battery": f"{data.get('battery', 100)}%",
                        "charging": data.get("charging", False),
                        "device": data.get("device", "Mobile")
                    }
    finally:
        ws_clients.discard(ws)
    return ws

async def upload_file(request):
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None: break
        if part.filename:
            safe_name = os.path.basename(part.filename)
            filepath = os.path.abspath(os.path.join(TRANSFER_DIR, safe_name))
            if not filepath.startswith(os.path.abspath(TRANSFER_DIR)): 
                continue
            with open(filepath, 'wb', buffering=4*1024*1024) as f:
                while True:
                    chunk = await part.read_chunk(size=1024*1024)
                    if not chunk: break
                    f.write(chunk)
    return web.json_response({"status": "uploaded", "dir": TRANSFER_DIR})

async def list_files(request):
    files = [
        {"name": f, "size": os.path.getsize(os.path.join(TRANSFER_DIR, f))} 
        for f in os.listdir(TRANSFER_DIR) 
        if os.path.isfile(os.path.join(TRANSFER_DIR, f))
    ]
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
    enable_pc_to_phone = params.get("enable_pc_to_phone", False)

    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
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
                if vcam is not None: 
                    vcam.send(img)
        except Exception: 
            break

async def handle_mic(track, pc):
    resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
    loop = asyncio.get_running_loop()
    
    if not IS_WINDOWS:
        cmd = [
            "pacat", "--playback", "-d", "PhoneMicEngine", 
            "--rate=48000", "--channels=1", "--format=s16le", 
            "--raw", "--latency-msec=10", "--process-time-msec=5"
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            while pc.connectionState not in ["failed", "closed"]:
                frame = await track.recv()
                for resampled in resampler.resample(frame):
                    raw_bytes = resampled.to_ndarray().tobytes()
                    try:
                        proc.stdin.write(raw_bytes)
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError): 
                        break
        except Exception: 
            pass
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
        except Exception: 
            pass
        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()

async def init_virtual_camera():
    global vcam
    try:
        if IS_WINDOWS:
            vcam = pyvirtualcam.Camera(width=VCAM_WIDTH, height=VCAM_HEIGHT, fps=VCAM_FPS, backend='obs', fmt=pyvirtualcam.PixelFormat.BGR)
        else:
            vcam = pyvirtualcam.Camera(width=VCAM_WIDTH, height=VCAM_HEIGHT, fps=VCAM_FPS, device=VCAM_DEVICE, fmt=pyvirtualcam.PixelFormat.BGR)
        vcam.send(make_standby_frame())
    except Exception as e:
        print(f"[VirtualCam Init Notice] Camera node initialization bypassed: {e}")

@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

app = web.Application(client_max_size=1024**3 * 10, middlewares=[cors_middleware])
app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response())
app.router.add_get("/api/status", get_status)
app.router.add_get("/ws/input", websocket_input_handler)
app.router.add_post("/api/upload", upload_file)
app.router.add_get("/api/files", list_files)
app.router.add_get("/api/files/{filename}", download_file)
app.router.add_post("/offer", offer)

if __name__ == "__main__":
    print("\n" + "="*65)
    print(f"   PHONE-TO-PC HEADLESS ENGINE ({platform.system().upper()})")
    print("="*65)
    print(f" Local Engine IP: https://{LAN_IP}:8443")
    print(f" File Storage:    {TRANSFER_DIR}")
    print(f" Virtual Cam:     {VCAM_DEVICE if not IS_WINDOWS else 'OBS Virtual Cam'}")
    print("="*65 + "\n")

    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_virtual_camera())

    ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_ctx.load_cert_chain(certfile="cert.pem", keyfile="key.pem")
    web.run_app(app, host="0.0.0.0", port=8443, ssl_context=ssl_ctx)
