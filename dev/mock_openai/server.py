"""Small OpenAI-compatible test server embedded in simulated Kubernetes pods.

Env:
  NODE_NAME       which logical fleet node to impersonate (required)
  MOCK_LISTENERS  JSON list of {"port": 8000, "model": "model-id"} (required)
  MOCK_TTFT_MS    simulated time-to-first-token, default 30
  MOCK_TPS        simulated decode tokens/sec, default 200

Failure injection (for gateway health-check demos):
  POST /mock/fail {"seconds": N}  -> /health returns 500 for N seconds
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NODE = os.environ["NODE_NAME"]
TTFT_S = int(os.environ.get("MOCK_TTFT_MS", "30")) / 1000.0
TPS = float(os.environ.get("MOCK_TPS", "200"))

TTFT_BUCKETS = [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
TPOT_BUCKETS = [0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5]
E2E_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]


def approx_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


class Histogram:
    def __init__(self, buckets):
        self.buckets = buckets
        self.counts = [0] * len(buckets)
        self.total = 0
        self.sum = 0.0
        self.lock = threading.Lock()

    def observe(self, value: float):
        with self.lock:
            self.total += 1
            self.sum += value
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    self.counts[i] += 1

    def render(self, name: str, labels: str) -> list[str]:
        out = []
        cumulative = 0
        with self.lock:
            for bound, count in zip(self.buckets, self.counts):
                cumulative += count
                out.append(f'{name}_bucket{{{labels},le="{bound}"}} {cumulative}')
            out.append(f'{name}_bucket{{{labels},le="+Inf"}} {self.total}')
            out.append(f"{name}_sum{{{labels}}} {self.sum:.6f}")
            out.append(f"{name}_count{{{labels}}} {self.total}")
        return out


class ModelState:
    """Per-listener counters, rendered as vLLM-style metrics."""

    def __init__(self, model: str):
        self.model = model
        self.lock = threading.Lock()
        self.prompt_tokens = 0
        self.generation_tokens = 0
        self.requests = 0
        self.running = 0
        self.ttft = Histogram(TTFT_BUCKETS)
        self.tpot = Histogram(TPOT_BUCKETS)
        self.e2e = Histogram(E2E_BUCKETS)

    def metrics(self) -> str:
        labels = f'model_name="{self.model}"'
        lines = [
            "# HELP vllm:num_requests_running Number of requests currently running.",
            "# TYPE vllm:num_requests_running gauge",
            f"vllm:num_requests_running{{{labels}}} {self.running}",
            "# TYPE vllm:num_requests_waiting gauge",
            f"vllm:num_requests_waiting{{{labels}}} 0",
            "# TYPE vllm:kv_cache_usage_perc gauge",
            f"vllm:kv_cache_usage_perc{{{labels}}} 0.0",
            "# TYPE vllm:prompt_tokens_total counter",
            f"vllm:prompt_tokens_total{{{labels}}} {self.prompt_tokens}",
            "# TYPE vllm:generation_tokens_total counter",
            f"vllm:generation_tokens_total{{{labels}}} {self.generation_tokens}",
            "# TYPE vllm:request_success_total counter",
            f'vllm:request_success_total{{{labels},finished_reason="stop"}} {self.requests}',
            "# TYPE vllm:time_to_first_token_seconds histogram",
            *self.ttft.render("vllm:time_to_first_token_seconds", labels),
            "# TYPE vllm:inter_token_latency_seconds histogram",
            *self.tpot.render("vllm:inter_token_latency_seconds", labels),
            "# TYPE vllm:e2e_request_latency_seconds histogram",
            *self.e2e.render("vllm:e2e_request_latency_seconds", labels),
        ]
        return "\n".join(lines) + "\n"


FAIL_UNTIL = {"ts": 0.0}


def make_handler(state: ModelState, port: int):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write(f"[{NODE}:{port}] {fmt % args}\n")

        def _send(self, code: int, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, code: int, obj):
            self._send(code, json.dumps(obj).encode())

        @property
        def route(self) -> str:
            """Path without query string; APISIX's ai transport appends '?'."""
            return self.path.split("?", 1)[0]

        def do_GET(self):
            if self.route == "/health":
                if time.time() < FAIL_UNTIL["ts"]:
                    self._send_json(500, {"status": "failing (mock/fail active)"})
                else:
                    self._send_json(200, {"status": "ok"})
            elif self.route == "/v1/models":
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": state.model,
                                "object": "model",
                                "owned_by": "spark-fleet-mock",
                            }
                        ],
                    },
                )
            elif self.route == "/metrics":
                self._send(200, state.metrics().encode(), "text/plain; version=0.0.4")
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                self._send_json(400, {"object": "error", "message": "invalid JSON"})
                return

            if self.route == "/mock/fail":
                FAIL_UNTIL["ts"] = time.time() + float(body.get("seconds", 30))
                self._send_json(200, {"failing_for": body.get("seconds", 30)})
                return
            if self.route not in ("/v1/chat/completions", "/v1/completions", "/v1/embeddings"):
                self._send_json(404, {"error": "not found"})
                return

            requested = body.get("model", "")
            if requested != state.model:
                sys.stderr.write(
                    f"[{NODE}:{port}] model mismatch: body carries {requested!r}, "
                    f"this listener serves {state.model!r}\n"
                )
                # Mirrors vLLM's unknown-model behavior.
                self._send_json(
                    404,
                    {
                        "object": "error",
                        "message": f"The model `{requested}` does not exist.",
                        "type": "NotFoundError",
                        "code": 404,
                    },
                )
                return

            started = time.time()
            with state.lock:
                state.running += 1
            try:
                if self.route == "/v1/embeddings":
                    self._embed(body, started)
                else:
                    self._complete(body, started)
            finally:
                with state.lock:
                    state.running -= 1

        def _complete(self, body, started):
            messages = body.get("messages") or [{"content": body.get("prompt", "")}]
            prompt_text = " ".join(str(m.get("content", "")) for m in messages)
            prompt_tokens = approx_tokens(prompt_text)
            completion_tokens = min(int(body.get("max_tokens") or 48), 256)

            words = [
                f"mock({state.model}@{NODE}:{port})"
            ] + [f"tok{i}" for i in range(1, completion_tokens)]
            content = " ".join(words)

            time.sleep(TTFT_S)
            decode_time = completion_tokens / TPS
            time.sleep(decode_time)

            now = time.time()
            state.ttft.observe(TTFT_S)
            state.tpot.observe(decode_time / max(1, completion_tokens))
            state.e2e.observe(now - started)
            with state.lock:
                state.prompt_tokens += prompt_tokens
                state.generation_tokens += completion_tokens
                state.requests += 1

            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
            rid = f"chatcmpl-mock-{uuid.uuid4().hex[:20]}"
            fingerprint = f"{NODE}:{port}"

            if body.get("stream"):
                self._stream(rid, fingerprint, words, usage, body)
                return

            if self.route == "/v1/completions":
                self._send_json(
                    200,
                    {
                        "id": rid.replace("chatcmpl-", "cmpl-"),
                        "object": "text_completion",
                        "created": int(started),
                        "model": state.model,
                        "system_fingerprint": fingerprint,
                        "choices": [
                            {"index": 0, "text": content, "logprobs": None,
                             "finish_reason": "stop"}
                        ],
                        "usage": usage,
                    },
                )
            else:
                self._send_json(
                    200,
                    {
                        "id": rid,
                        "object": "chat.completion",
                        "created": int(started),
                        "model": state.model,
                        "system_fingerprint": fingerprint,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": usage,
                    },
                )

        def _embed(self, body, started):
            inputs = body.get("input", "")
            if not isinstance(inputs, list):
                inputs = [inputs]
            data = []
            prompt_tokens = 0
            for index, item in enumerate(inputs):
                text = str(item)
                prompt_tokens += approx_tokens(text)
                digest = hashlib.sha256(f"{state.model}\0{text}".encode()).digest()
                vector = [round((byte - 127.5) / 127.5, 6) for byte in digest[:16]]
                data.append({"object": "embedding", "embedding": vector, "index": index})
            with state.lock:
                state.prompt_tokens += prompt_tokens
                state.requests += 1
            state.e2e.observe(time.time() - started)
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": data,
                    "model": state.model,
                    "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
                },
            )

        def _stream(self, rid, fingerprint, words, usage, body):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def chunk(obj):
                data = f"data: {json.dumps(obj)}\n\n".encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

            base = {
                "id": rid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": state.model,
                "system_fingerprint": fingerprint,
            }
            chunk({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
            for word in words:
                chunk({**base, "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]})
            chunk({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            include_usage = (body.get("stream_options") or {}).get("include_usage")
            if include_usage:
                chunk({**base, "choices": [], "usage": usage})
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):x}\r\n".encode() + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

    return Handler


def listeners() -> list[dict]:
    """Read the listeners rendered into the pod by render_kubernetes.py."""
    raw = os.environ.get("MOCK_LISTENERS")
    if not raw:
        raise SystemExit("MOCK_LISTENERS is required")
    configured = json.loads(raw)
    if not isinstance(configured, list):
        raise SystemExit("MOCK_LISTENERS must be a JSON list")
    return configured


def main():
    servers = []
    for container in listeners():
        state = ModelState(container["model"])
        server = ThreadingHTTPServer(
            ("0.0.0.0", container["port"]), make_handler(state, container["port"])
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        print(
            f"mock node {NODE}: serving {container['model']} on :{container['port']}",
            flush=True,
        )

    if not servers:
        print(f"mock node {NODE}: no containers planned; idling", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
