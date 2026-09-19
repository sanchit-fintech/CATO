"""Detection and lifecycle management for an owned local Cato API process."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from client.api_client import APIError, HTTPAgentClient


@dataclass(frozen=True)
class ServiceStatus:
    state: str
    message: str
    health: dict[str, Any] | None = None
    owned: bool = False

    @property
    def ready(self) -> bool:
        return self.state == "ready"


class LocalAPIManager:
    def __init__(
        self,
        client: HTTPAgentClient,
        *,
        process_factory: Callable[..., Any] = subprocess.Popen,
        startup_timeout: float = 10,
    ) -> None:
        self.client = client
        self.process_factory = process_factory
        self.startup_timeout = startup_timeout
        self._process = None

    def probe(self) -> ServiceStatus:
        try:
            health = self.client.health()
        except APIError as error:
            if error.code == "connection_refused":
                return ServiceStatus("absent", str(error))
            if error.code == "malformed_response":
                return ServiceStatus("non_cato", str(error))
            return ServiceStatus(error.code, str(error))
        if health.get("service") != "cato":
            return ServiceStatus(
                "incompatible_service",
                "The port has a stale Cato API or another HTTP service.",
                health,
            )
        if health.get("ready") is not True:
            return ServiceStatus(
                "configuration_required",
                "Cato is running but GEMINI_API_KEY is not configured for the service.",
                health,
            )
        return ServiceStatus(
            "ready", "Cato API is ready.", health, self._process is not None
        )

    def ensure(self, *, can_start: bool = True) -> ServiceStatus:
        status = self.probe()
        if status.ready or not can_start:
            return status
        if status.state in {
            "incompatible_service",
            "non_cato",
            "api_http_error",
            "api_timeout",
            "connection_failed",
        }:
            self._use_free_loopback_port()
            status = self.probe()
        elif status.state == "configuration_required":
            return status
        if status.state != "absent":
            return status
        if self._port_in_use():
            return ServiceStatus(
                "port_occupied",
                "The API port is already in use by another process.",
            )
        parsed = urlsplit(self.client.base_url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        host = parsed.hostname or "127.0.0.1"
        if parsed.scheme != "http" or host not in {"127.0.0.1", "localhost", "::1"}:
            return ServiceStatus(
                "wrong_url", "Automatic startup requires a local HTTP URL."
            )
        project_root = Path(__file__).resolve().parents[1]
        try:
            self._process = self.process_factory(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "core.api:create_app",
                    "--factory",
                    "--host",
                    host,
                    "--port",
                    str(port),
                ],
                cwd=project_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
        except (FileNotFoundError, OSError):
            return ServiceStatus(
                "startup_failed",
                "Cato could not start its API. Install the optional voice "
                "dependencies.",
            )
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._process = None
                return ServiceStatus(
                    "startup_failed", "The Cato API process exited during startup."
                )
            status = self.probe()
            if status.state != "absent":
                return status
            time.sleep(0.1)
        self.stop()
        return ServiceStatus("startup_timeout", "Cato API startup timed out.")

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def _port_in_use(self) -> bool:
        parsed = urlsplit(self.client.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            return False

    def _use_free_loopback_port(self) -> None:
        parsed = urlsplit(self.client.base_url)
        host = parsed.hostname or "127.0.0.1"
        family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            listener.bind((host, 0))
            port = listener.getsockname()[1]
        display_host = f"[{host}]" if ":" in host else host
        self.client.base_url = f"http://{display_host}:{port}"
