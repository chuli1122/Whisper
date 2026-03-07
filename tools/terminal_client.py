"""
Terminal Client — 常驻后台连接 VPS，让模型通过 Telegram 远程操作本地电脑。

用法:
    python tools/terminal_client.py
    python tools/terminal_client.py --url wss://your-vps:8002/ws/terminal

需要安装: pip install websockets
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import subprocess
import sys
import threading

# Add NVIDIA CUDA libs to PATH so ctranslate2/faster-whisper can find cublas
_nvidia_libs = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
if os.path.isdir(_nvidia_libs):
    for _sub in os.listdir(_nvidia_libs):
        _bin = os.path.join(_nvidia_libs, _sub, "bin")
        if os.path.isdir(_bin):
            os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")

# Force flush on every print
print = functools.partial(print, flush=True)
import io
import urllib.request
import urllib.error

# Windows UTF-8 + unbuffered
if os.name == "nt":
    os.system("")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")

DEFAULT_API = os.environ.get("TERMINAL_API", "http://43.156.11.234:8002")
DEFAULT_PASSWORD = "chuli2026bendanachengbendanahuai"

RESET = "\033[0m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


# ── Auth ──

def get_token() -> str:
    password = os.environ.get("WHISPER_PASSWORD") or DEFAULT_PASSWORD
    url = f"{DEFAULT_API}/api/auth/verify"
    data = json.dumps({"password": password}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())["token"]
    except Exception as e:
        print(f"{RED}登录失败: {e}{RESET}")
        sys.exit(1)


# ── Tool execution ──

def execute_tool(name: str, arguments: dict) -> dict:
    if name == "run_bash":
        return run_bash(arguments.get("command", ""))
    elif name == "read_file":
        return read_file(arguments.get("path", ""))
    elif name == "write_file":
        return write_file(arguments.get("path", ""), arguments.get("content", ""))
    elif name == "screenshot":
        return screenshot()
    elif name == "mouse_click":
        return mouse_click(arguments.get("x", 0), arguments.get("y", 0), arguments.get("button", "left"))
    elif name == "keyboard_type":
        return keyboard_type(arguments.get("text", ""))
    elif name == "hotkey":
        return hotkey(arguments.get("keys", []))
    elif name == "scroll":
        return scroll_screen(arguments.get("clicks", -3), arguments.get("x"), arguments.get("y"))
    elif name == "transcribe":
        return transcribe_audio(arguments.get("audio_base64", ""), arguments.get("file_name", "voice.ogg"))
    elif name == "toy_control":
        return toy_control(
            arguments.get("action", ""),
            arguments.get("intensity", 0.5),
            arguments.get("index"),
            arguments.get("pattern"),
            arguments.get("duration", 10),
        )
    return {"error": f"Unknown tool: {name}"}


def run_bash(command: str) -> dict:
    if not command:
        return {"error": "empty command"}
    print(f"  {DIM}$ {command}{RESET}")
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=60,
            cwd=os.path.expanduser("~"),
            encoding="utf-8", errors="replace",
        )
        output = result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        if len(output) > 8000:
            output = output[:4000] + f"\n... (truncated {len(output) - 8000} chars) ...\n" + output[-4000:]
        print(f"  {DIM}{output.rstrip()}{RESET}")
        return {"exit_code": result.returncode, "output": output}
    except subprocess.TimeoutExpired:
        return {"error": "command timed out (60s)"}
    except Exception as e:
        return {"error": str(e)}


def read_file(path: str) -> dict:
    if not path:
        return {"error": "empty path"}
    path = os.path.expanduser(path)
    print(f"  {DIM}[读取] {path}{RESET}")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > 16000:
            content = content[:8000] + f"\n... (truncated, total {len(content)} chars) ...\n" + content[-8000:]
        return {"path": path, "content": content}
    except FileNotFoundError:
        return {"error": f"file not found: {path}"}
    except Exception as e:
        return {"error": str(e)}


def write_file(path: str, content: str) -> dict:
    if not path:
        return {"error": "empty path"}
    path = os.path.expanduser(path)
    print(f"  {DIM}[写入] {path} ({len(content)} chars){RESET}")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"path": path, "status": "ok", "bytes_written": len(content.encode("utf-8"))}
    except Exception as e:
        return {"error": str(e)}


# ── Screen & input tools ──

def screenshot() -> dict:
    print(f"  {DIM}[截屏]{RESET}")
    try:
        import mss
        import mss.tools
        from PIL import Image
        import base64

        with mss.mss() as sct:
            monitor = sct.monitors[1]  # primary monitor
            img = sct.grab(monitor)
            # Convert to PIL for resize + compress
            pil_img = Image.frombytes("RGB", img.size, img.rgb)

        # Resize to max 1280 width
        w, h = pil_img.size
        if w > 1280:
            ratio = 1280 / w
            pil_img = pil_img.resize((1280, int(h * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=50)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        size_kb = len(buf.getvalue()) / 1024
        print(f"  {DIM}截屏 {pil_img.size[0]}x{pil_img.size[1]}, {size_kb:.0f}KB{RESET}")
        return {"image": f"data:image/jpeg;base64,{b64}", "width": pil_img.size[0], "height": pil_img.size[1]}
    except ImportError as e:
        return {"error": f"缺少依赖: {e}. 请运行: pip install mss Pillow"}
    except Exception as e:
        return {"error": str(e)}


def mouse_click(x: int, y: int, button: str = "left") -> dict:
    print(f"  {DIM}[点击] ({x}, {y}) {button}{RESET}")
    try:
        import pyautogui
        pyautogui.click(x=int(x), y=int(y), button=button)
        return {"status": "ok", "x": x, "y": y, "button": button}
    except ImportError:
        return {"error": "缺少依赖: pip install pyautogui"}
    except Exception as e:
        return {"error": str(e)}


def keyboard_type(text: str) -> dict:
    if not text:
        return {"error": "empty text"}
    print(f"  {DIM}[输入] {text[:50]}{'...' if len(text) > 50 else ''}{RESET}")
    try:
        import pyautogui
        pyautogui.typewrite(text, interval=0.02) if text.isascii() else _type_unicode(text)
        return {"status": "ok", "length": len(text)}
    except ImportError:
        return {"error": "缺少依赖: pip install pyautogui"}
    except Exception as e:
        return {"error": str(e)}


def _type_unicode(text: str):
    """Type unicode text (pyautogui.typewrite only handles ASCII)."""
    import pyautogui
    import pyperclip
    old = pyperclip.paste()
    pyperclip.copy(text)
    pyautogui.hotkey("ctrl", "v")
    import time
    time.sleep(0.1)
    pyperclip.copy(old)


def hotkey(keys: list) -> dict:
    if not keys:
        return {"error": "empty keys"}
    print(f"  {DIM}[快捷键] {'+'.join(keys)}{RESET}")
    try:
        import pyautogui
        pyautogui.hotkey(*keys)
        return {"status": "ok", "keys": keys}
    except ImportError:
        return {"error": "缺少依赖: pip install pyautogui"}
    except Exception as e:
        return {"error": str(e)}


def scroll_screen(clicks: int = -3, x: int | None = None, y: int | None = None) -> dict:
    print(f"  {DIM}[滚动] {clicks} clicks at ({x}, {y}){RESET}")
    try:
        import pyautogui
        pyautogui.scroll(int(clicks), x=int(x) if x is not None else None, y=int(y) if y is not None else None)
        return {"status": "ok", "clicks": clicks}
    except ImportError:
        return {"error": "缺少依赖: pip install pyautogui"}
    except Exception as e:
        return {"error": str(e)}


# ── Transcribe (local faster-whisper) ──

_whisper_model = None

def transcribe_audio(audio_base64: str, file_name: str = "voice.ogg") -> dict:
    """Transcribe audio using local faster-whisper model."""
    global _whisper_model
    import base64
    import tempfile
    import time

    if not audio_base64:
        return {"error": "empty audio_base64"}

    print(f"  {DIM}[转写] {file_name}{RESET}")
    start = time.time()

    try:
        audio_bytes = base64.b64decode(audio_base64)
    except Exception as e:
        return {"error": f"base64 decode failed: {e}"}

    # Write to temp file
    ext = file_name.rsplit(".", 1)[-1] if "." in file_name else "ogg"
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
    except Exception as e:
        return {"error": f"temp file write failed: {e}"}

    try:
        # Lazy-load model
        if _whisper_model is None:
            print(f"  {DIM}加载 whisper 模型...{RESET}")
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel("deepdml/faster-whisper-large-v3-turbo-ct2", device="cuda", compute_type="float16")
            print(f"  {DIM}模型加载完成{RESET}")

        segments, info = _whisper_model.transcribe(tmp_path, language="zh")
        text = "".join(seg.text for seg in segments).strip()
        duration = time.time() - start
        print(f"  {DIM}转写完成 ({duration:.1f}s): {text[:60]}{RESET}")
        return {"text": text, "duration": round(duration, 2)}
    except ImportError:
        return {"error": "缺少依赖: pip install faster-whisper"}
    except Exception as e:
        return {"error": f"transcribe failed: {e}"}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Toy control (Buttplug.io / Intiface Central) ──

_bp_client = None
_bp_connected = False
_bp_loop = None
_bp_thread = None


def _get_bp_loop():
    """Get or create a dedicated event loop for buttplug async operations."""
    global _bp_loop, _bp_thread
    if _bp_loop is None or not _bp_loop.is_running():
        _bp_loop = asyncio.new_event_loop()
        _bp_thread = threading.Thread(target=_bp_loop.run_forever, daemon=True)
        _bp_thread.start()
    return _bp_loop


def _describe_device(idx, device):
    """Return a dict describing a device and its actuators."""
    actuators = []
    try:
        for i, act in enumerate(device.actuators):
            actuators.append({
                "index": i,
                "type": type(act).__name__,
                "description": str(act),
                "step_count": getattr(act, "step_count", None),
            })
    except Exception:
        pass
    return {"device_index": idx, "name": device.name, "actuators": actuators}


async def _async_toy_control(action, intensity, index, pattern, duration):
    global _bp_client, _bp_connected

    if action == "connect":
        try:
            from buttplug import Client, WebsocketConnector
        except ImportError:
            return {"error": "缺少依赖: pip install buttplug-py"}

        # Disconnect existing client if any
        if _bp_client and _bp_connected:
            try:
                await _bp_client.disconnect()
            except Exception:
                pass
            _bp_connected = False

        _bp_client = Client("Acheng")
        connector = WebsocketConnector("ws://127.0.0.1:12345")
        try:
            await _bp_client.connect(connector)
        except Exception as e:
            return {"error": f"连接 Intiface Central 失败 (确保已启动): {e}"}

        await _bp_client.start_scanning()
        await asyncio.sleep(3)
        await _bp_client.stop_scanning()
        _bp_connected = True

        devices = [_describe_device(i, d) for i, d in _bp_client.devices.items()]
        print(f"  {GREEN}已连接 Intiface Central, {len(devices)} 个设备{RESET}")
        return {"status": "connected", "devices": devices}

    if not _bp_client or not _bp_connected:
        return {"error": "未连接，请先 connect"}

    if action == "list":
        devices = [_describe_device(i, d) for i, d in _bp_client.devices.items()]
        return {"status": "ok", "devices": devices}

    if action == "vibrate":
        if not _bp_client.devices:
            return {"error": "没有已连接的设备"}
        intensity = max(0.0, min(1.0, float(intensity)))
        controlled = []
        for dev_idx, device in _bp_client.devices.items():
            try:
                if index is not None:
                    # Control specific actuator by index
                    idx = int(index)
                    if idx < len(device.actuators):
                        await device.actuators[idx].command(intensity)
                        controlled.append(f"{device.name}[{idx}]")
                else:
                    # Control all actuators
                    for i, act in enumerate(device.actuators):
                        await act.command(intensity)
                    controlled.append(device.name)
            except Exception as e:
                controlled.append(f"{device.name}: error {e}")
        print(f"  {DIM}震动 {intensity:.0%}: {', '.join(controlled)}{RESET}")
        return {"status": "vibrating", "intensity": intensity, "devices": controlled}

    if action == "stop":
        for device in _bp_client.devices.values():
            try:
                await device.stop()
            except Exception:
                pass
        print(f"  {DIM}已停止所有设备{RESET}")
        return {"status": "stopped"}

    if action == "pattern":
        if not _bp_client.devices:
            return {"error": "没有已连接的设备"}
        duration = max(1, min(120, int(duration)))
        pattern = pattern or "wave"

        async def _run_pattern():
            import math
            elapsed = 0.0
            step = 0.2  # update interval
            while elapsed < duration:
                t = elapsed / duration
                if pattern == "pulse":
                    val = 1.0 if int(elapsed / 0.5) % 2 == 0 else 0.0
                elif pattern == "wave":
                    val = (math.sin(t * math.pi * 4) + 1) / 2
                elif pattern == "escalate":
                    val = min(1.0, t * 1.2)
                else:
                    val = 0.5
                for device in _bp_client.devices.values():
                    try:
                        if index is not None:
                            idx = int(index)
                            if idx < len(device.actuators):
                                await device.actuators[idx].command(val)
                        else:
                            for act in device.actuators:
                                await act.command(val)
                    except Exception:
                        pass
                await asyncio.sleep(step)
                elapsed += step
            # Stop after pattern
            for device in _bp_client.devices.values():
                try:
                    await device.stop()
                except Exception:
                    pass

        await _run_pattern()
        print(f"  {DIM}模式 {pattern} 完成 ({duration}s){RESET}")
        return {"status": "pattern_done", "pattern": pattern, "duration": duration}

    if action == "disconnect":
        try:
            await _bp_client.disconnect()
        except Exception:
            pass
        _bp_connected = False
        print(f"  {DIM}已断开 Intiface Central{RESET}")
        return {"status": "disconnected"}

    return {"error": f"未知 action: {action}"}


def toy_control(action, intensity=0.5, index=None, pattern=None, duration=10):
    """Control Bluetooth toy via Intiface Central / Buttplug.io."""
    print(f"  {DIM}[玩具] action={action} intensity={intensity} index={index}{RESET}")
    try:
        loop = _get_bp_loop()
        future = asyncio.run_coroutine_threadsafe(
            _async_toy_control(action, intensity, index, pattern, duration), loop
        )
        return future.result(timeout=max(30, duration + 5) if action == "pattern" else 30)
    except TimeoutError:
        return {"error": "操作超时"}
    except Exception as e:
        return {"error": str(e)}


# ── WebSocket client ──

async def connect_loop():
    try:
        import websockets
    except ImportError:
        print(f"{RED}需要安装 websockets: pip install websockets{RESET}")
        sys.exit(1)

    token = get_token()
    print(f"{GREEN}已登录{RESET}")

    # Build WS URL
    api_base = DEFAULT_API
    if api_base.startswith("https://"):
        ws_base = "wss://" + api_base[8:]
    elif api_base.startswith("http://"):
        ws_base = "ws://" + api_base[7:]
    else:
        ws_base = "ws://" + api_base
    ws_url = f"{ws_base}/ws/terminal?token={token}"

    while True:
        try:
            print(f"{CYAN}连接 {ws_base}/ws/terminal ...{RESET}")
            async with websockets.connect(ws_url, ping_interval=10, ping_timeout=10) as ws:
                print(f"{GREEN}已连接，等待命令...{RESET}")
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    req_id = data.get("id")
                    tool = data.get("tool")
                    arguments = data.get("arguments", {})

                    if not req_id or not tool:
                        continue

                    print(f"\n{YELLOW}[{tool}]{RESET} id={req_id[:8]}...")
                    # Run tool in thread so it doesn't block WebSocket ping/pong
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(None, execute_tool, tool, arguments)
                    response = json.dumps({"id": req_id, "result": result})
                    await ws.send(response)
                    print(f"{GREEN}  -> 结果已发送{RESET}")

        except Exception as e:
            print(f"{RED}连接断开: {e}{RESET}")

        print(f"{DIM}5秒后重连...{RESET}")
        await asyncio.sleep(5)
        # Re-auth in case token expired
        try:
            token = get_token()
            ws_url = f"{ws_base}/ws/terminal?token={token}"
        except Exception:
            pass


def main():
    global DEFAULT_API
    # Parse --url argument
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--url" and i < len(sys.argv):
            # Extract base from ws URL
            url = sys.argv[i + 1]
            if "/ws/" in url:
                url = url[:url.index("/ws/")]
            url = url.replace("wss://", "https://").replace("ws://", "http://")
            DEFAULT_API = url
        elif arg.startswith("--api="):
            DEFAULT_API = arg.split("=", 1)[1]

    print(f"{CYAN}Terminal Client{RESET}")
    print(f"  API: {DEFAULT_API}")
    print(f"  按 Ctrl+C 退出\n")
    try:
        asyncio.run(connect_loop())
    except KeyboardInterrupt:
        print(f"\n{DIM}已退出{RESET}")


if __name__ == "__main__":
    main()
