#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Small, authenticated, single-model relay; the upstream key never reaches the VM."""
import hmac
import json
import math
import os
import re
import ssl
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit


RATE_HEADERS = (
    "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
    "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens",
    "x-ratelimit-limit-tokens-prompt", "x-ratelimit-limit-tokens-cache-adjusted-prompt",
    "x-ratelimit-limit-tokens-generated", "fireworks-prompt-tokens",
    "fireworks-cached-prompt-tokens",
)


def safe_rate_headers(headers):
    """Only numeric quota values and a normalized, bounded retry delay may leave the relay."""
    result = {}
    for name in RATE_HEADERS:
        value = headers.get(name, "").strip()
        if re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,6})?", value):
            result[name] = value
    delay = None
    raw = headers.get("Retry-After", "").strip()
    if re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,6})?", raw):
        delay = math.ceil(float(raw))
    elif raw and len(raw) <= 64:
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is not None:
                delay = max(0, math.ceil((when - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass
    if delay is None:
        milliseconds = headers.get("retry-after-ms", "").strip()
        if re.fullmatch(r"[0-9]{1,8}", milliseconds):
            delay = math.ceil(int(milliseconds) / 1000)
    if delay is not None and 0 <= delay <= 86400:
        result["Retry-After"] = str(delay)
    return result


def provider_rate_error(error):
    """Classify bounded provider text into fixed labels; never return its text, URLs or keys."""
    headers = safe_rate_headers(error.headers)
    try:
        detail = json.loads(error.read(8192))
        detail = detail.get("error", detail) if isinstance(detail, dict) else None
        if isinstance(detail, dict):
            text = " ".join(str(detail.get(name, "")) for name in ("code", "type", "message"))
        else:
            text = detail if isinstance(detail, str) else ""
    except (ValueError, OSError):
        text = ""
    tokens = bool(re.search(r"tokens?[ _-]*(?:per[ _-]*minute|rate[ _-]*limit)|\btpm\b|prompt[ _-]*tokens|generated[ _-]*tokens", text, re.I))
    requests = bool(re.search(r"requests?[ _-]*(?:per[ _-]*minute|rate[ _-]*limit)|\brpm\b", text, re.I))
    dimension = "tokens and requests" if tokens and requests else "tokens" if tokens else "requests" if requests else "unspecified"
    message = "Inference provider rate limit reached (" + dimension + ")"
    diagnostics = {"source": "upstream", "httpStatus": 429, "reportedLimit": dimension,
                   "rateHeaders": headers}
    if "Retry-After" in headers:
        seconds = int(headers["Retry-After"])
        diagnostics["retryAfterSeconds"] = seconds
        message += "; retry after " + str(seconds) + " seconds"
    return {"error": message, "diagnostics": diagnostics}, headers


class Broker(ThreadingHTTPServer):
    def __init__(self, address, *, endpoint, model, upstream_key, client_key,
                 auth_header="Authorization", max_requests=1000, max_tokens=4096):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("The author must supply an HTTPS inference base URL without credentials or query parameters")
        if not model or not upstream_key or not client_key:
            raise ValueError("Inference model and credentials are required")
        if auth_header not in ("Authorization", "api-key"):
            raise ValueError("Unsupported upstream authentication header")
        self.endpoint, self.model = endpoint.rstrip("/"), model
        self.upstream_key, self.client_key = upstream_key, client_key
        self.auth_header = auth_header
        self.max_requests, self.max_tokens = max_requests, max_tokens
        self.requests_used = 0
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # No prompt, credential, URL or upstream error logging.

    def send_json(self, status, value, headers=None):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return hmac.compare_digest(self.headers.get("Authorization", ""),
                                   "Bearer " + self.server.client_key)

    def do_GET(self):
        if self.path == "/healthz":
            return self.send_json(200, {"status": "ready"})
        if not self.authorized():
            return self.send_json(401, {"error": "Unauthorized"})
        if self.path == "/v1/models":
            return self.send_json(200, {"object": "list", "data": [
                {"id": self.server.model, "object": "model", "owned_by": "lab-provider"}]})
        return self.send_json(404, {"error": "Unsupported route"})

    def do_POST(self):
        if not self.authorized():
            return self.send_json(401, {"error": "Unauthorized"})
        if self.path != "/v1/chat/completions":
            return self.send_json(404, {"error": "Unsupported route"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1048576:
                return self.send_json(413, {"error": "Request too large or empty"})
            self.connection.settimeout(30)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or body.get("model") != self.server.model:
                return self.send_json(400, {"error": "Only the assigned model is available"})
            if not isinstance(body.get("messages"), list) or not body["messages"]:
                return self.send_json(400, {"error": "Messages are required"})
            field = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
            limit = body.get(field, self.server.max_tokens)
            if type(limit) is not int or not 1 <= limit <= self.server.max_tokens:
                return self.send_json(400, {"error": "Output token limit exceeded"})
            # Reject a second conflicting limit; do not silently increase the cap.
            for name in ("max_tokens", "max_completion_tokens"):
                if name in body and (type(body[name]) is not int or not 1 <= body[name] <= self.server.max_tokens):
                    return self.send_json(400, {"error": "Output token limit exceeded"})
            body[field] = limit
            if not self.server.slots.acquire(blocking=False):
                return self.send_json(429, {"error": "Too many concurrent requests"}, {"Retry-After": "1"})
            try:
                with self.server.lock:
                    if self.server.requests_used >= self.server.max_requests:
                        return self.send_json(429, {"error": "Lab request allowance exhausted"})
                    self.server.requests_used += 1
                credential = self.server.upstream_key
                if self.server.auth_header == "Authorization":
                    credential = "Bearer " + credential
                request = urllib.request.Request(self.server.endpoint + "/chat/completions",
                    data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
                    self.server.auth_header: credential}, method="POST")
                # Redirects must not send a provider credential to a different host.
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *_args, **_kwargs):
                        return None
                opener = urllib.request.build_opener(NoRedirect())
                try:
                    with opener.open(request, timeout=180) as response:
                        if body.get("stream") is True:
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Cache-Control", "no-cache")
                            self.send_header("Connection", "close")
                            for name, value in safe_rate_headers(response.headers).items():
                                self.send_header(name, value)
                            self.end_headers()
                            self.close_connection = True
                            while chunk := response.read1(8192):
                                self.wfile.write(chunk)
                                self.wfile.flush()
                        else:
                            result = response.read(4194305)
                            if len(result) > 4194304:
                                return self.send_json(502, {"error": "Provider response too large"})
                            self.send_json(200, json.loads(result), safe_rate_headers(response.headers))
                except urllib.error.HTTPError as error:
                    with error:
                        if error.code == 429:
                            value, headers = provider_rate_error(error)
                            return self.send_json(429, value, headers)
                        return self.send_json(502, {"error": "Inference provider rejected the request"})
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                    return self.send_json(502, {"error": "Inference provider unavailable"})
            finally:
                self.server.slots.release()
        except (ValueError, TypeError, TimeoutError):
            return self.send_json(400, {"error": "Invalid JSON request"})
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    server = Broker(("0.0.0.0", 8000), endpoint=os.environ["INFERENCE_ENDPOINT"],
        model=os.environ["INFERENCE_MODEL"],
        upstream_key=Path("/var/run/upstream/api-key").read_text().strip(),
        client_key=Path("/var/run/broker-auth/token").read_text().strip(),
        auth_header=os.environ.get("INFERENCE_AUTH_HEADER", "Authorization"),
        max_requests=int(os.environ.get("MAX_REQUESTS", "1000")))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/var/run/lab-tls/tls.crt", "/var/run/lab-tls/tls.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
