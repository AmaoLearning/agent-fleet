"""Loopback-only relay that fixes DeepSeek benchmark sampling parameters."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HOST = "127.0.0.1"
PORT = int(os.environ.get("DSH_SAMPLING_RELAY_PORT", "18100"))
UPSTREAM = os.environ["DSH_SAMPLING_UPSTREAM_BASE_URL"].rstrip("/")
RECEIPT = Path(
    os.environ.get(
        "DSH_SAMPLING_RECEIPT_PATH", "/logs/agent/sampling-relay.jsonl"
    )
)
FIXED_TEMPERATURE = 1.0
FIXED_TOP_P = 0.95
FIXED_REASONING_EFFORT = "max"
HOP_BY_HOP = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _upstream_url(path: str) -> str:
    base = urlsplit(UPSTREAM)
    base_path = base.path.rstrip("/")
    incoming = urlsplit(path)
    suffix = incoming.path
    if base_path and (suffix == base_path or suffix.startswith(base_path + "/")):
        merged = suffix
    else:
        merged = f"{base_path}/{suffix.lstrip('/')}"
    return urlunsplit((base.scheme, base.netloc, merged, incoming.query, ""))


def _append_receipt(record: dict[str, object]) -> None:
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    with RECEIPT.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
        handle.write("\n")


class Relay(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"sampling-relay: {format % args}", flush=True)

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/healthz":
            self.send_error(404)
            return
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        started = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            original_body = self.rfile.read(length)
            payload = json.loads(original_body)
            if not isinstance(payload, dict):
                raise TypeError("request JSON must be an object")
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self.send_error(400, str(error))
            return

        original = {
            "temperature": payload.get("temperature"),
            "top_p": payload.get("top_p"),
            "reasoning_effort": payload.get("reasoning_effort"),
        }
        payload["temperature"] = FIXED_TEMPERATURE
        payload["top_p"] = FIXED_TOP_P
        payload["reasoning_effort"] = FIXED_REASONING_EFFORT
        forwarded_body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP
        }
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            _upstream_url(self.path),
            data=forwarded_body,
            headers=headers,
            method="POST",
        )
        response = None
        status = 502
        response_bytes = 0
        request_id = None
        try:
            response = urllib.request.urlopen(request, timeout=3600)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError) as error:
            body = json.dumps(
                {"error": {"message": f"sampling relay upstream failure: {error}"}}
            ).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            response_bytes = len(body)
        if response is not None:
            status = int(response.status)
            request_id = response.headers.get("x-request-id") or response.headers.get(
                "x-yicloud-request-id"
            )
            streaming = bool(payload.get("stream"))
            self.send_response(status)
            for key, value in response.headers.items():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            if streaming:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while chunk := response.read(8192):
                    response_bytes += len(chunk)
                    self.wfile.write(f"{len(chunk):X}\r\n".encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            else:
                body = response.read()
                response_bytes = len(body)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            response.close()

        _append_receipt(
            {
                "effective": {
                    "reasoning_effort": FIXED_REASONING_EFFORT,
                    "temperature": FIXED_TEMPERATURE,
                    "top_p": FIXED_TOP_P,
                },
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "message_count": len(payload.get("messages") or []),
                "model": payload.get("model"),
                "original": original,
                "request_bytes": len(forwarded_body),
                "request_sha256": hashlib.sha256(forwarded_body).hexdigest(),
                "response_bytes": response_bytes,
                "status": status,
                "stream": bool(payload.get("stream")),
                "tool_count": len(payload.get("tools") or []),
                "upstream_request_id": request_id,
            }
        )


def main() -> None:
    server = http.server.ThreadingHTTPServer((HOST, PORT), Relay)
    print(f"sampling-relay: listening on {HOST}:{PORT} upstream={UPSTREAM}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
