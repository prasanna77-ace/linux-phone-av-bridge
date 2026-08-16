import ssl, asyncio, socket, subprocess, os, fractions, cv2
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, AudioStreamTrack
import pyvirtualcam
import numpy as np
import av
import qrcode

pcs = set()
vcam = None
vcam_lock = asyncio.Lock()
active_tasks = set()

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
MOBILE_URL = f"https://{LAN_IP}:8443/phone"
DASHBOARD_URL = f"https://{LAN_IP}:8443/"

def make_standby_frame(w=1280, h=720, text="PhoneWebcam (Active)"):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (15, 23, 42)
    cv2.putText(img, text, (int(w*0.28), int(h*0.48)), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (56, 189, 248), 2, cv2.LINE_AA)
    cv2.putText(img, "Standby: Connect phone to stream video", (int(w*0.27), int(h*0.56)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (148, 163, 184), 1, cv2.LINE_AA)
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

    async def init_process(self):
        cmd = [
            "parec",
            "--format=s16le",
            "--rate=48000",
            "--channels=2",
            "--raw",
            "--latency-msec=10",
            "--process-time-msec=5",
            "-d", "@DEFAULT_MONITOR@"
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        self.reader = self.proc.stdout

    async def recv(self):
        if self.proc is None:
            await self.init_process()

        try:
            raw_data = await self.reader.readexactly(self.bytes_per_frame)
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
            try:
                self.proc.terminate()
            except Exception:
                pass

async def handle_dashboard(request):
    return web.FileResponse('./static/dashboard.html')

async def handle_phone(request):
    return web.FileResponse('./static/phone.html')

async def get_status(request):
    return web.json_response({
        "lan_ip": LAN_IP,
        "mobile_url": MOBILE_URL,
        "active_connections": len(pcs),
        "video_device": "/dev/video10",
        "audio_sink": "Phone_Microphone",
        "monitor_source": "@DEFAULT_MONITOR@"
    })

async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    enable_pc_to_phone = params.get("enable_pc_to_phone", True)

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
            if audio_track:
                audio_track.stop()
            await pc.close()
            pcs.discard(pc)
            async with vcam_lock:
                if vcam:
                    vcam.send(make_standby_frame())

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    for _ in range(20):
        if pc.iceGatheringState == "complete":
            break
        await asyncio.sleep(0.05)

    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})

async def handle_video(track, pc):
    global vcam
    TARGET_W, TARGET_H = 1280, 720
    while pc.connectionState not in ["failed", "closed"]:
        try:
            frame = await track.recv()
            img = frame.to_ndarray(format="bgr24")
            h, w, _ = img.shape

            if w != TARGET_W or h != TARGET_H:
                img = cv2.resize(img, (TARGET_W, TARGET_H), interpolation=cv2.INTER_LINEAR)

            async with vcam_lock:
                if vcam is not None:
                    vcam.send(img)
        except Exception:
            break

async def handle_mic(track, pc):
    cmd = [
        "pacat",
        "--playback",
        "-d", "PhoneMicEngine",
        "--rate=48000",
        "--channels=1",
        "--format=s16le",
        "--raw",
        "--latency-msec=10",
        "--process-time-msec=5"
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL
    )
    resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)

    try:
        while pc.connectionState not in ["failed", "closed"]:
            frame = await track.recv()
            for resampled in resampler.resample(frame):
                raw_bytes = resampled.to_ndarray().tobytes()
                proc.stdin.write(raw_bytes)
                await proc.stdin.drain()
    except Exception:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

async def init_virtual_camera():
    global vcam
    try:
        vcam = pyvirtualcam.Camera(width=1280, height=720, fps=30, device='/dev/video10', fmt=pyvirtualcam.PixelFormat.BGR)
        vcam.send(make_standby_frame())
    except Exception as e:
        print(f"V4L2 Init: {e}")

app = web.Application()
app.router.add_get("/", handle_dashboard)
app.router.add_get("/phone", handle_phone)
app.router.add_get("/api/status", get_status)
app.router.add_post("/offer", offer)
app.router.add_static("/static", path="./static", name="static")

if __name__ == "__main__":
    qr = qrcode.QRCode()
    qr.add_data(MOBILE_URL)
    qr.make()

    print("\n" + "="*60)
    print("       LINUX AV STREAM HUB (HARDWARE MIC READY)")
    print("="*60)
    print(f" PC Dashboard:   {DASHBOARD_URL}")
    print(f" Phone Access:   {MOBILE_URL}")
    print(f" Default Mic:    Phone_Microphone (Recognized by all sites)")
    print("\nScan QR Code on Phone:")
    qr.print_ascii(invert=True)
    print("="*60 + "\n")

    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_virtual_camera())

    ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_ctx.load_cert_chain(certfile="cert.pem", keyfile="key.pem")
    web.run_app(app, host="0.0.0.0", port=8443, ssl_context=ssl_ctx)
