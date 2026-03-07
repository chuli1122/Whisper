#!/usr/bin/env python3
"""
阿澄的终端 — chuli_home CLI 聊天入口
用法: python tools/cli_chat.py

支持本地工具：阿澄可以在你的电脑上执行命令、读写文件。
"""

import base64
import io
import json
import mimetypes
import os
import subprocess
import sys
import urllib.error
import urllib.request

# Windows 终端 UTF-8 支持
if os.name == "nt":
    os.system("")  # enable ANSI/VT
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")

API_URL = os.environ.get("CHULI_API_URL", "https://chat.chuli.win")
DEFAULT_PASSWORD = "chuli2026bendanachengbendanahuai"
ASSISTANT_ID = 2  # 阿澄

# ANSI colors
DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
WHITE = "\033[97m"


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _separator() -> str:
    return f"{DIM}{'─' * _term_width()}{RESET}"


def api(method: str, path: str, body: dict | None = None, token: str | None = None,
        stream: bool = False) -> dict | urllib.request.http.client.HTTPResponse:
    url = f"{API_URL}/api{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    resp = urllib.request.urlopen(req, timeout=300)
    if stream:
        return resp
    return json.loads(resp.read().decode())


def login() -> str:
    password = os.environ.get("WHISPER_PASSWORD") or DEFAULT_PASSWORD
    try:
        result = api("POST", "/auth/verify", {"password": password})
    except urllib.error.HTTPError as e:
        print(f"登录失败: {e.code}")
        sys.exit(1)
    return result["token"]


def list_sessions(token: str) -> list[dict]:
    result = api("GET", f"/sessions?assistant_id={ASSISTANT_ID}", token=token)
    return result.get("sessions", [])


def create_session(token: str, title: str = "") -> dict:
    return api("POST", "/sessions", {
        "assistant_id": ASSISTANT_ID,
        "title": title or "终端聊天",
    }, token=token)


def pick_session(token: str) -> int:
    sessions = list_sessions(token)
    if sessions:
        print("\n已有会话:")
        for i, s in enumerate(sessions[:10]):
            tag = " (最近)" if i == 0 else ""
            print(f"  [{i}] #{s['id']} {s.get('title', '')}{tag}")
        print(f"  [n] 新建会话")
        choice = input("\n选择 (直接回车选最近): ").strip().lower()
        if choice == "n":
            s = create_session(token)
            print(f"  新建会话 #{s['id']}")
            return s["id"]
        if choice == "":
            return sessions[0]["id"]
        try:
            idx = int(choice)
            return sessions[idx]["id"]
        except (ValueError, IndexError):
            return sessions[0]["id"]
    else:
        s = create_session(token)
        print(f"  新建会话 #{s['id']}")
        return s["id"]


# ─── Local tool execution ───


def execute_local_tool(name: str, arguments: dict) -> str:
    """Execute a local tool and return the result as a JSON string."""
    if name == "run_bash":
        return _run_bash(arguments.get("command", ""))
    if name == "read_file":
        return _read_file(arguments.get("path", ""))
    if name == "write_file":
        return _write_file(arguments.get("path", ""), arguments.get("content", ""))
    return json.dumps({"error": f"Unknown tool: {name}"})


def _run_bash(command: str) -> str:
    if not command:
        return json.dumps({"error": "empty command"})
    sys.stdout.write(f"\n{DIM}$ {command}{RESET}\n")
    sys.stdout.flush()
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=60,
            cwd=os.getcwd(),
        )
        output = result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        # Truncate very long output
        if len(output) > 8000:
            output = output[:4000] + f"\n... (truncated {len(output) - 8000} chars) ...\n" + output[-4000:]
        sys.stdout.write(f"{DIM}{output.rstrip()}{RESET}\n")
        sys.stdout.flush()
        return json.dumps({"exit_code": result.returncode, "output": output})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "command timed out (60s)"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _read_file(path: str) -> str:
    if not path:
        return json.dumps({"error": "empty path"})
    path = os.path.expanduser(path)
    sys.stdout.write(f"\n{DIM}[读取] {path}{RESET}\n")
    sys.stdout.flush()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > 16000:
            content = content[:8000] + f"\n... (truncated, total {len(content)} chars) ...\n" + content[-8000:]
        return json.dumps({"path": path, "content": content})
    except FileNotFoundError:
        return json.dumps({"error": f"file not found: {path}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _write_file(path: str, content: str) -> str:
    if not path:
        return json.dumps({"error": "empty path"})
    path = os.path.expanduser(path)
    sys.stdout.write(f"\n{DIM}[写入] {path} ({len(content)} chars){RESET}\n")
    sys.stdout.flush()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return json.dumps({"path": path, "status": "ok", "bytes_written": len(content.encode("utf-8"))})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ─── Chat with tool loop ───


def stream_chat(token: str, session_id: int, message: str | list | None,
                tool_results: list[dict] | None = None) -> None:
    """Send a message (or tool results) and stream the response.
    Handles tool_call events by executing locally and sending results back."""
    body: dict = {"session_id": session_id, "stream": True, "source": "terminal"}
    if tool_results:
        body["tool_results"] = tool_results
        body["message"] = None
    elif message is not None:
        body["message"] = message

    try:
        resp = api("POST", "/chat/completions", body, token=token, stream=True)
    except urllib.error.HTTPError as e:
        print(f"\n[错误] {e.code}: {e.read().decode()[:200]}")
        return

    pending_tool_calls: list[dict] = []
    has_content = False

    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            break
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue

        if "content" in data:
            if not has_content:
                # Clear typing indicator line, write name prefix
                sys.stdout.write(f"\r\033[2K{BOLD}阿澄{RESET}  ")
                has_content = True
            sys.stdout.write(data["content"])
            sys.stdout.flush()

        if "tool_call" in data:
            pending_tool_calls.append(data["tool_call"])

        if "error" in data:
            sys.stdout.write(f"\r\033[2K[错误] {data['error']}")

    if has_content:
        sys.stdout.write("\n\n")
        sys.stdout.flush()

    # If there are pending tool calls, execute them locally and send results back
    if pending_tool_calls:
        # Clear typing indicator if no content was shown
        if not has_content:
            sys.stdout.write(f"\r\033[2K")
        sys.stdout.write(f"{CYAN}[工具调用] {len(pending_tool_calls)} 个{RESET}\n")
        sys.stdout.flush()

        results = []
        for tc in pending_tool_calls:
            tc_id = tc.get("id", "")
            tc_name = tc.get("name", "")
            tc_args = tc.get("arguments", {})

            sys.stdout.write(f"{YELLOW}> {tc_name}({json.dumps(tc_args, ensure_ascii=False)[:80]}){RESET}\n")
            sys.stdout.flush()

            result_content = execute_local_tool(tc_name, tc_args)
            results.append({
                "tool_call_id": tc_id,
                "name": tc_name,
                "content": result_content,
            })

        # Send results back — this may trigger another tool round
        stream_chat(token, session_id, None, tool_results=results)


def build_image_message(text: str, image_path: str) -> list[dict]:
    image_path = image_path.strip().strip('"').strip("'")
    if not os.path.isfile(image_path):
        print(f"  文件不存在: {image_path}")
        return []
    mime, _ = mimetypes.guess_type(image_path)
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    parts = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.append({
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{b64}"},
    })
    return parts


def main():
    token = login()
    session_id = pick_session(token)

    # Clear screen and print header at top
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()
    print(f"{DIM}阿澄的终端 | 会话 #{session_id} | /quit 退出 | /img <路径> 发图片{RESET}")
    print(f"{DIM}本地工具: run_bash, read_file, write_file{RESET}")
    print()

    while True:
        try:
            print(_separator())
            user_input = input(f"{BOLD}❯{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n再见 ✨")
            break

        if not user_input:
            # Erase top sep + empty input line
            sys.stdout.write("\033[A\033[2K\033[A\033[2K")
            sys.stdout.flush()
            continue
        if user_input.lower() == "/quit":
            print("再见 ✨")
            break

        # Erase input area (sep + input line), show as sent message with name
        sys.stdout.write("\033[A\033[2K\033[A\033[2K")
        sys.stdout.flush()
        print(f"{BOLD}初礼{RESET}  {user_input}")
        print()

        # Show typing indicator
        sys.stdout.write(f"{BOLD}阿澄{RESET}  {DIM}...{RESET}")
        sys.stdout.flush()

        # /img 命令
        if user_input.lower().startswith("/img "):
            rest = user_input[5:].strip()
            parts_split = rest.split(" ", 1)
            image_path = parts_split[0]
            caption = parts_split[1] if len(parts_split) > 1 else ""
            message = build_image_message(caption, image_path)
            if not message:
                # Clear typing indicator
                sys.stdout.write("\r\033[2K")
                sys.stdout.flush()
                continue
            stream_chat(token, session_id, message)
            continue

        # 普通文字消息
        stream_chat(token, session_id, user_input)


if __name__ == "__main__":
    main()
