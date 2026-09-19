"""Low-level native macOS operations with injectable process execution."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.tool_types import ToolResult
from tools.filesystem_policy import (
    PathOutsideApprovedRoots,
    is_sensitive_path,
    resolve_approved_path,
)

Runner = Callable[..., subprocess.CompletedProcess[bytes]]
APP_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+_-]{0,79}$")
ALIASES = {"vscode": "Visual Studio Code", "vs code": "Visual Studio Code"}
PROTECTED_APPS = {
    "Finder",
    "System Settings",
    "System Preferences",
    "Dock",
    "loginwindow",
}


def is_macos(system: str | None = None) -> bool:
    return (system or platform.system()) == "Darwin"


@dataclass
class MacOSController:
    approved_roots: tuple[Path, ...]
    allowed_apps: frozenset[str]
    max_clipboard_bytes: int = 100_000
    runner: Runner = subprocess.run

    def _run(self, argv: list[str], *, input_data: bytes | None = None) -> ToolResult:
        try:
            result = self.runner(
                argv,
                input=input_data,
                capture_output=True,
                timeout=10,
                shell=False,
                check=False,
            )
        except FileNotFoundError:
            return ToolResult.failure(
                "Required macOS utility is unavailable.", code="utility_unavailable"
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failure(
                "The macOS action timed out.", code="action_timeout"
            )
        except OSError:
            return ToolResult.failure(
                "The macOS action could not be started.", code="action_error"
            )
        if result.returncode != 0:
            return ToolResult.failure(
                "macOS rejected the action or permission is unavailable.",
                code="action_failed",
            )
        return ToolResult.success(result.stdout)

    def normalize_app(self, name: str) -> str | None:
        value = ALIASES.get(name.strip().lower(), name.strip())
        if not APP_PATTERN.fullmatch(value) or value not in self.allowed_apps:
            return None
        return value

    def open_app(self, name: str) -> ToolResult:
        app = self.normalize_app(name)
        if app is None:
            return ToolResult.failure(
                "That application is not allowed.", code="app_not_allowed"
            )
        result = self._run(["/usr/bin/open", "-a", app])
        return self._summarize(result, f"Opened {app}.", {"application": app})

    def activate_app(self, name: str) -> ToolResult:
        app = self.normalize_app(name)
        if app is None:
            return ToolResult.failure(
                "That application is not allowed.", code="app_not_allowed"
            )
        result = self._run(["/usr/bin/open", "-a", app])
        return self._summarize(result, f"Activated {app}.", {"application": app})

    def quit_app(self, name: str) -> ToolResult:
        app = self.normalize_app(name)
        if app is None or app in PROTECTED_APPS:
            return ToolResult.failure(
                "That application cannot be quit by Cato.", code="app_protected"
            )
        script = (
            'tell application "System Events" to quit application process '
            "(item 1 of argv)"
        )
        result = self._run(
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                script,
                "-e",
                "end run",
                "--",
                app,
            ]
        )
        return self._summarize(result, f"Quit {app}.", {"application": app})

    def open_path(
        self, path: str, *, directory: bool | None = None, reveal: bool = False
    ) -> ToolResult:
        target = self._approved(path)
        if isinstance(target, ToolResult):
            return target
        if directory is True and not target.is_dir():
            return ToolResult.failure("The path is not a folder.", code="not_directory")
        if directory is False and not target.is_file():
            return ToolResult.failure("The path is not a file.", code="not_file")
        if is_sensitive_path(target):
            return ToolResult.failure(
                "Sensitive paths cannot be opened.", code="sensitive_path"
            )
        argv = (
            ["/usr/bin/open", "-R", str(target)]
            if reveal
            else ["/usr/bin/open", str(target)]
        )
        result = self._run(argv)
        verb = "Revealed" if reveal else "Opened"
        return self._summarize(result, f"{verb} {target.name}.", {"path": str(target)})

    def open_url(self, url: str) -> ToolResult:
        try:
            parsed = urlsplit(url.strip())
        except ValueError:
            return ToolResult.failure("The URL is malformed.", code="invalid_url")
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            return ToolResult.failure(
                "Only public HTTP and HTTPS URLs are supported.",
                code="url_scheme_denied",
            )
        normalized = urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc,
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
        result = self._run(["/usr/bin/open", normalized])
        return self._summarize(result, "Opened the URL.", {"url": normalized})

    def clipboard_read(self) -> ToolResult:
        result = self._run(["/usr/bin/pbpaste"])
        if not result.ok:
            return result
        raw = result.data
        if len(raw) > self.max_clipboard_bytes:
            return ToolResult.failure(
                "Clipboard text is too large.", code="clipboard_too_large"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult.failure(
                "Clipboard does not contain UTF-8 text.", code="clipboard_not_text"
            )
        return ToolResult.success({"text": text}, summary="Read clipboard text.")

    def clipboard_write(self, text: str) -> ToolResult:
        raw = text.encode("utf-8")
        if len(raw) > self.max_clipboard_bytes:
            return ToolResult.failure(
                "Clipboard text is too large.", code="clipboard_too_large"
            )
        result = self._run(["/usr/bin/pbcopy"], input_data=raw)
        return self._summarize(
            result, "Wrote text to the clipboard.", {"bytes": len(raw)}
        )

    def show_notification(self, title: str, message: str) -> ToolResult:
        if (
            not title.strip()
            or len(title) > 100
            or not message.strip()
            or len(message) > 500
        ):
            return ToolResult.failure(
                "Notification text is invalid or too long.", code="invalid_arguments"
            )
        script = "display notification (item 2 of argv) with title (item 1 of argv)"
        result = self._run(
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                script,
                "-e",
                "end run",
                "--",
                title,
                message,
            ]
        )
        return self._summarize(result, "Notification shown.", None)

    def list_running_apps(self) -> ToolResult:
        script = (
            'tell application "System Events" to get name of every application '
            "process whose background only is false"
        )
        result = self._run(["/usr/bin/osascript", "-e", script])
        if not result.ok:
            return result
        apps = sorted(
            {
                item.strip()
                for item in result.data.decode(errors="replace").split(",")
                if item.strip()
            }
        )
        return ToolResult.success(
            {"applications": apps}, summary=f"Found {len(apps)} running applications."
        )

    def open_in_vscode(self, path: str) -> ToolResult:
        target = self._approved(path)
        if isinstance(target, ToolResult):
            return target
        if is_sensitive_path(target):
            return ToolResult.failure(
                "Sensitive paths cannot be opened.", code="sensitive_path"
            )
        executable = shutil.which("code")
        argv = (
            [executable, str(target)]
            if executable
            else ["/usr/bin/open", "-a", "Visual Studio Code", str(target)]
        )
        result = self._run(argv)
        return self._summarize(
            result,
            f"Opened {target.name} in Visual Studio Code.",
            {"path": str(target)},
        )

    def open_in_terminal(self, path: str) -> ToolResult:
        target = self._approved(path)
        if isinstance(target, ToolResult):
            return target
        if not target.is_dir():
            return ToolResult.failure("The path is not a folder.", code="not_directory")
        result = self._run(["/usr/bin/open", "-a", "Terminal", str(target)])
        return self._summarize(
            result, f"Opened {target.name} in Terminal.", {"path": str(target)}
        )

    def permission_status(self) -> ToolResult:
        return ToolResult.success(
            {
                "platform": "macOS" if is_macos() else platform.system(),
                "accessibility": "unknown",
                "automation": "unknown",
                "notifications": "unknown",
                "full_disk_access": "unknown",
                "native_open": "available"
                if Path("/usr/bin/open").exists()
                else "unavailable",
            },
            summary=(
                "Permission status is conservative; macOS does not expose every "
                "grant reliably."
            ),
        )

    def _approved(self, path: str) -> Path | ToolResult:
        try:
            return resolve_approved_path(path, self.approved_roots)
        except FileNotFoundError:
            return ToolResult.failure("Path not found.", code="not_found")
        except (PathOutsideApprovedRoots, OSError):
            return ToolResult.failure(
                "That path is outside Cato's approved roots.", code="path_not_approved"
            )

    @staticmethod
    def _summarize(
        result: ToolResult, summary: str, data: dict[str, Any] | None
    ) -> ToolResult:
        return ToolResult.success(data or {}, summary=summary) if result.ok else result
