#!/usr/bin/env python3
"""Exercise a real Codex binary, isolated home, and two clients with a local mock model.

Usage: python3 scripts/verify_endpoint_recovery.py /absolute/path/to/codex
No credentials, external model calls, or production sockets are used.
"""

import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time


class Client:
    """Minimal uncompressed WebSocket JSON-RPC client for the local test fixture."""

    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(15)
        self.socket.connect(str(path))
        key = base64.b64encode(os.urandom(16)).decode()
        self.socket.sendall((
            "GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            header += self.exact(1)
            assert len(header) < 16384, "oversized handshake"
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest())
        assert header.startswith(b"HTTP/1.1 101 ") and expected in header
        self.sequence = 0
        self.events = []
        self.rpc("initialize", {
            "clientInfo": {"name": "endpoint_recovery_smoke", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        self.send({"method": "initialized"})

    def exact(self, count):
        data = b""
        while len(data) < count:
            chunk = self.socket.recv(count - len(data))
            if not chunk:
                raise EOFError("server disconnected")
            data += chunk
        return data

    def frame(self, payload, opcode=1):
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            header = bytes([0x80 | opcode, 0x80 | size])
        elif size < 65536:
            header = bytes([0x80 | opcode, 0xFE]) + struct.pack("!H", size)
        else:
            header = bytes([0x80 | opcode, 0xFF]) + struct.pack("!Q", size)
        self.socket.sendall(header + mask + bytes(
            byte ^ mask[i % 4] for i, byte in enumerate(payload)
        ))

    def send(self, value):
        self.frame(json.dumps(value).encode())

    def receive(self):
        fragments = b""
        while True:
            first, second = self.exact(2)
            assert not first & 0x70 and not second & 0x80
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self.exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.exact(8))[0]
            assert size < 32 * 1024 * 1024
            payload = self.exact(size)
            opcode = first & 15
            if opcode == 8:
                raise EOFError("server closed websocket")
            if opcode == 9:
                self.frame(payload, 10)
                continue
            if opcode == 10:
                continue
            assert opcode in (0, 1)
            fragments += payload
            if first & 0x80:
                return json.loads(fragments)

    def rpc(self, method, params):
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        while True:
            result = self.receive()
            if result.get("id") == self.sequence:
                assert "error" not in result, (method, result.get("error"))
                return result["result"]
            self.events.append(result)

    def completed(self, turn_id):
        while True:
            for i, event in enumerate(self.events):
                if event.get("method") == "turn/completed" and event["params"]["turn"]["id"] == turn_id:
                    self.events.pop(i)
                    assert event["params"]["turn"]["status"] == "completed", event
                    return
            self.events.append(self.receive())

    def close(self):
        self.socket.close()


started = threading.Event()
release = threading.Event()


class Model(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.event({"type": "response.created", "response": {"id": "fixture-response"}})
        started.set()
        assert release.wait(90), "fixture completion was never released"
        self.event({"type": "response.output_item.done", "item": {
            "type": "message", "role": "assistant", "id": "fixture-message",
            "content": [{"type": "output_text", "text": "Endpoint recovery verified."}],
        }})
        self.event({"type": "response.completed", "response": {
            "id": "fixture-response", "usage": {
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "input_tokens_details": None, "output_tokens_details": None,
            },
        }})

    def event(self, value):
        self.wfile.write(f"event: {value['type']}\ndata: {json.dumps(value)}\n\n".encode())
        self.wfile.flush()


def connect_when_ready(path, process):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        assert process.poll() is None, "app-server exited during startup"
        try:
            return Client(path)
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.05)
    raise TimeoutError("app-server endpoint did not become reachable")


def main():
    binary = Path(sys.argv[1]).resolve(strict=True)
    model = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="codex-endpoint-smoke-") as temporary:
        home = Path(temporary)
        (home / "config.toml").write_text(f'''
model = "mock-model"
model_provider = "mock_provider"
approval_policy = "never"
sandbox_mode = "read-only"
[model_providers.mock_provider]
name = "Local test fixture"
base_url = "http://127.0.0.1:{model.server_port}/v1"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
''')
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CODEX_", "OPENAI_"))}
        env["CODEX_HOME"] = str(home)
        env["CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED"] = "1"
        command = [str(binary), "app-server", "--listen", "unix://"]
        log = tempfile.TemporaryFile()
        process = subprocess.Popen(command, env=env, cwd=home, stdout=log, stderr=log)
        clients = []
        try:
            path = home / "app-server-control/app-server-control.sock"
            first = connect_when_ready(path, process)
            clients.append(first)
            second = Client(path)
            clients.append(second)
            thread = first.rpc("thread/start", {"cwd": str(home)})["thread"]["id"]
            turn = first.rpc("turn/start", {
                "threadId": thread, "input": [{"type": "text", "text": "fixture turn", "text_elements": []}],
            })["turn"]["id"]
            assert started.wait(15), "mock model did not receive the turn"
            assert second.rpc("thread/resume", {"threadId": thread})["thread"]["id"] == thread
            print("two_clients_same_thread=PASS", flush=True)
            physical = path.resolve(strict=True)
            before = physical.stat().st_ino
            # Only this fixture's exact paths are removed.
            path.unlink()
            physical.unlink()
            repair_start = time.monotonic()
            third = connect_when_ready(path, process)
            clients.append(third)
            assert third.rpc("thread/resume", {"threadId": thread})["thread"]["id"] == thread
            assert physical.stat().st_ino != before
            assert physical.stat().st_mode & 0o777 == 0o600
            print(f"endpoint_recovered_ms={int((time.monotonic() - repair_start) * 1000)}", flush=True)

            duplicate = subprocess.run(command, env=env, cwd=home, capture_output=True, timeout=30)
            assert duplicate.returncode != 0 and b"already owns this Codex home" in duplicate.stderr
            print("second_writer_rejected=PASS", flush=True)

            release.set()
            for client in clients:
                client.completed(turn)
                assert client.rpc("thread/read", {"threadId": thread})["thread"]["id"] == thread
            print("active_turn_survived_and_all_three_clients_received_completion=PASS", flush=True)

            first.close()
            clients.remove(first)
            mobile_turn = third.rpc("turn/start", {
                "threadId": thread, "input": [{"type": "text", "text": "continue from second device", "text_elements": []}],
            })["turn"]["id"]
            third.completed(mobile_turn)
            second.completed(mobile_turn)
            returning = Client(path)
            clients.append(returning)
            resumed = returning.rpc("thread/resume", {"threadId": thread})
            assert resumed["thread"]["id"] == thread
            print("device_handoff_and_original_client_reconnect=PASS", flush=True)

            for client in clients:
                client.close()
            clients.clear()
            process.send_signal(signal.SIGHUP)
            assert process.wait(timeout=20) == 0
            process = subprocess.Popen(command, env=env, cwd=home, stdout=log, stderr=log)
            after_restart = connect_when_ready(path, process)
            clients.append(after_restart)
            assert after_restart.rpc("thread/resume", {"threadId": thread})["thread"]["id"] == thread
            print("graceful_restart_same_thread=PASS", flush=True)
        finally:
            release.set()
            for client in clients:
                client.close()
            if process.poll() is None:
                process.send_signal(signal.SIGHUP)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if sys.exc_info()[0] is not None:
                log.seek(0)
                print(log.read().decode(errors="replace")[-12000:], file=sys.stderr)
            log.close()
    model.shutdown()


if __name__ == "__main__":
    main()
