from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import base64
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.request

from .driver import Driver, DriverError, DriverUnavailable, ElementNotFound


@dataclass(frozen=True)
class CdpTarget:
    id: str
    title: str
    url: str
    websocket_url: str


class CdpProtocolError(DriverError):
    """Raised when the CDP endpoint returns an error response."""


POST_CLICK_DELAY_SEC = 0.35
POST_TEXT_DELAY_SEC = 0.5
MODAL_CLOSE_NAMES = (
    "关闭时间范围设置",
    "完成",
    "取消 取消",
    "关闭自动化导出",
    "关闭任务中心",
)


class CdpWebSocket:
    def __init__(self, websocket_url: str, timeout: float = 10) -> None:
        self.websocket_url = websocket_url
        self.timeout = timeout
        self._socket: socket.socket | None = None

    def connect(self) -> None:
        parsed = urlparse(self.websocket_url)
        if parsed.scheme != "ws":
            raise DriverUnavailable(f"Only ws:// CDP endpoints are supported: {self.websocket_url}")

        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        sock = socket.create_connection((host, port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = _recv_until(sock, b"\r\n\r\n")
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            sock.close()
            raise DriverUnavailable("CDP WebSocket upgrade failed.")
        self._socket = sock

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def recv_json(self) -> dict[str, Any]:
        while True:
            opcode, payload = self._recv_frame()
            if opcode == 1:
                return json.loads(payload.decode("utf-8"))
            if opcode == 8:
                raise DriverUnavailable("CDP WebSocket closed.")
            if opcode == 9:
                self._send_frame(payload, opcode=10)

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        sock = self._require_socket()
        first = 0x80 | opcode
        mask_bit = 0x80
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, mask_bit | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", first, mask_bit | 126, length)
        else:
            header = struct.pack("!BBQ", first, mask_bit | 127, length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        sock.sendall(header + mask + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        sock = self._require_socket()
        header = _recv_exact(sock, 2)
        first, second = header
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
        mask = _recv_exact(sock, 4) if masked else b""
        payload = _recv_exact(sock, length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise DriverUnavailable("CDP WebSocket is not connected.")
        return self._socket


class CdpConnection:
    def __init__(self, websocket: CdpWebSocket) -> None:
        self.websocket = websocket
        self._next_id = 1

    @classmethod
    def connect(cls, websocket_url: str, timeout: float = 10) -> "CdpConnection":
        websocket = CdpWebSocket(websocket_url, timeout=timeout)
        websocket.connect()
        return cls(websocket)

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        message_id = self._next_id
        self._next_id += 1
        self.websocket.send_json({"id": message_id, "method": method, "params": params or {}})
        while True:
            response = self.websocket.recv_json()
            if response.get("id") != message_id:
                continue
            if "error" in response:
                raise CdpProtocolError(str(response["error"]))
            return dict(response.get("result") or {})

    def close(self) -> None:
        self.websocket.close()


class CdpDriver(Driver):
    def __init__(self, connection: CdpConnection) -> None:
        self.connection = connection
        self.connection.send("Runtime.enable")
        self.connection.send("Page.enable")
        self.connection.send("Page.bringToFront")

    @classmethod
    def connect(cls, endpoint: str) -> "CdpDriver":
        target = select_page_target(fetch_cdp_targets(endpoint))
        if target is None:
            raise DriverUnavailable("No usable WeFlow page target was exposed by CDP.")
        return cls(CdpConnection.connect(target.websocket_url))

    def click_by_name(self, name: str, retries: int = 3) -> None:
        last_result: Any = None
        for _ in range(max(1, retries)):
            last_result = self._evaluate(_click_script(name))
            if isinstance(last_result, dict) and last_result.get("ok"):
                time.sleep(POST_CLICK_DELAY_SEC)
                return
            time.sleep(0.5)
        raise ElementNotFound(f"Could not click UI element named {name!r}: {last_result}")

    def click_if_present(self, name: str, timeout: float = 2) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._evaluate(_click_script(name))
            if isinstance(result, dict) and result.get("ok"):
                time.sleep(POST_CLICK_DELAY_SEC)
                return True
            time.sleep(0.25)
        return False

    def set_text(self, field_name: str, text: str) -> None:
        result = self._evaluate(_set_text_script(field_name, text))
        if not isinstance(result, dict) or not result.get("ok"):
            raise ElementNotFound(f"Could not set text field {field_name!r}: {result}")
        time.sleep(POST_TEXT_DELAY_SEC)

    def wait_for(self, name: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        last_result: Any = None
        while time.monotonic() < deadline:
            last_result = self._evaluate(_wait_script(name))
            if isinstance(last_result, dict) and last_result.get("ok"):
                return
            time.sleep(0.5)
        raise ElementNotFound(f"Timed out waiting for {name!r}: {last_result}")

    def wait_for_absent(self, name: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        last_result: Any = None
        while time.monotonic() < deadline:
            last_result = self._evaluate(_wait_script(name))
            if isinstance(last_result, dict) and not last_result.get("ok"):
                return
            time.sleep(0.5)
        raise ElementNotFound(f"Timed out waiting for {name!r} to disappear: {last_result}")

    def wait_for_enabled(self, name: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        last_result: Any = None
        while time.monotonic() < deadline:
            last_result = self._evaluate(_enabled_script(name))
            if isinstance(last_result, dict) and last_result.get("ok"):
                return
            time.sleep(0.5)
        raise ElementNotFound(f"Timed out waiting for enabled UI element {name!r}: {last_result}")

    def wait_for_text_sequence(self, first: str, second: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        last_result: Any = None
        while time.monotonic() < deadline:
            last_result = self._evaluate(_text_sequence_script(first, second))
            if isinstance(last_result, dict) and last_result.get("ok"):
                return
            time.sleep(0.5)
        raise ElementNotFound(f"Timed out waiting for text sequence {first!r} -> {second!r}: {last_result}")

    def ensure_selected(self, name: str, timeout: float = 60) -> None:
        selected_name = f"取消选择 {name}"
        selected = self._evaluate(_wait_script(selected_name))
        if isinstance(selected, dict) and selected.get("ok"):
            return
        self.click_by_name(f"选择 {name}", retries=3)
        self.wait_for(selected_name, timeout=timeout)

    def ensure_checked(self, name: str, timeout: float = 60) -> None:
        state = self._evaluate(_checkbox_state_script(name))
        if isinstance(state, dict) and state.get("ok") and state.get("checked"):
            return
        self.click_by_name(name)
        deadline = time.monotonic() + timeout
        last_result: Any = state
        while time.monotonic() < deadline:
            last_result = self._evaluate(_checkbox_state_script(name))
            if isinstance(last_result, dict) and last_result.get("ok") and last_result.get("checked"):
                return
            time.sleep(0.25)
        raise ElementNotFound(f"Timed out waiting for checkbox {name!r} to become checked: {last_result}")

    def ensure_action_available(self, action_name: str, trigger_name: str, timeout: float = 60) -> None:
        available = self._evaluate(_enabled_script(action_name))
        if isinstance(available, dict) and available.get("ok"):
            return
        if not trigger_name:
            raise ElementNotFound(f"Action {action_name!r} was unavailable and no trigger was provided.")
        self.click_by_name(trigger_name)
        self.wait_for_enabled(action_name, timeout=timeout)

    def close_any_modal(self, timeout: float = 5) -> int:
        deadline = time.monotonic() + timeout
        closed = 0
        while time.monotonic() < deadline:
            if not self.close_current_modal(timeout=0.5):
                break
            closed += 1
        return closed

    def close_current_modal(self, timeout: float = 5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for name in MODAL_CLOSE_NAMES:
                if self.click_if_present(name, timeout=0.25):
                    return True
            time.sleep(0.25)
        return False

    def screenshot(self) -> bytes:
        result = self.connection.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        data = result.get("data")
        if not isinstance(data, str):
            raise CdpProtocolError("Page.captureScreenshot returned no data.")
        return base64.b64decode(data)

    def visible_elements(self, limit: int = 200) -> list[dict[str, Any]]:
        result = self._evaluate(_visible_elements_script(limit))
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    def close(self) -> None:
        self.connection.close()

    def _evaluate(self, expression: str) -> Any:
        result = self.connection.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
        )
        if "exceptionDetails" in result:
            raise CdpProtocolError(str(result["exceptionDetails"]))
        return result.get("result", {}).get("value")


def fetch_cdp_targets(endpoint: str) -> list[dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"{endpoint.rstrip('/')}/json/list", timeout=5) as response:
            return list(json.loads(response.read().decode("utf-8")))
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise DriverUnavailable(f"Could not read CDP target list from {endpoint}: {exc}") from exc


def select_page_target(targets: list[dict[str, Any]]) -> CdpTarget | None:
    page_targets = [target for target in targets if target.get("type") == "page" and target.get("webSocketDebuggerUrl")]
    if not page_targets:
        return None

    def score(target: dict[str, Any]) -> tuple[int, int, str]:
        url = str(target.get("url") or "")
        notification_penalty = 1 if "notification-window" in url else 0
        home_penalty = 0 if "#/home" in url else 1
        return (notification_penalty, home_penalty, url)

    selected = sorted(page_targets, key=score)[0]
    return CdpTarget(
        id=str(selected.get("id") or ""),
        title=str(selected.get("title") or ""),
        url=str(selected.get("url") or ""),
        websocket_url=str(selected["webSocketDebuggerUrl"]),
    )


def _click_script(name: str) -> str:
    return f"""
{_dom_helpers()}
(() => {{
  const target = {json.dumps(name, ensure_ascii=False)};
  const found = findByName(target);
  if (!found) return {{ ok: false, reason: "not found", target }};
  const clickable = clickableAncestor(found);
  if (!enabled(clickable)) return {{ ok: false, reason: "disabled", text: textOf(clickable), tag: clickable.tagName }};
  clickable.scrollIntoView({{ block: "center", inline: "center" }});
  clickElement(clickable);
  return {{ ok: true, text: textOf(clickable), tag: clickable.tagName }};
}})()
"""


def _set_text_script(field_name: str, text: str) -> str:
    return f"""
{_dom_helpers()}
(() => {{
  const fieldName = {json.dumps(field_name, ensure_ascii=False)};
  const text = {json.dumps(text, ensure_ascii=False)};
  const field = findField(fieldName);
  if (!field) return {{ ok: false, reason: "field not found", fieldName }};
  field.scrollIntoView({{ block: "center", inline: "center" }});
  field.focus();
  if (field.isContentEditable) {{
    field.textContent = text;
  }} else {{
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), "value")?.set;
    if (setter) setter.call(field, text); else field.value = text;
  }}
  field.dispatchEvent(new InputEvent("input", {{ bubbles: true, inputType: "insertText", data: text }}));
  field.dispatchEvent(new Event("change", {{ bubbles: true }}));
  return {{ ok: true, tag: field.tagName }};
}})()
"""


def _wait_script(name: str) -> str:
    return f"""
{_dom_helpers()}
(() => {{
  const target = {json.dumps(name, ensure_ascii=False)};
  const found = findByName(target);
  return found ? {{ ok: true, text: textOf(found), tag: found.tagName }} : {{ ok: false, target }};
}})()
"""


def _enabled_script(name: str) -> str:
    return f"""
{_dom_helpers()}
(() => {{
  const target = {json.dumps(name, ensure_ascii=False)};
  const found = findByName(target);
  if (!found) return {{ ok: false, reason: "not found", target }};
  const clickable = clickableAncestor(found);
  return enabled(clickable)
    ? {{ ok: true, text: textOf(clickable), tag: clickable.tagName }}
    : {{ ok: false, reason: "disabled", text: textOf(clickable), tag: clickable.tagName }};
}})()
"""


def _visible_elements_script(limit: int) -> str:
    return f"""
{_dom_helpers()}
(() => {{
  const selector = "button,a,input,textarea,[contenteditable='true'],[role],[tabindex]";
  return Array.from(document.querySelectorAll(selector))
    .filter(visible)
    .slice(0, {int(limit)})
    .map((element) => {{
      const target = clickableAncestor(element);
      return {{
        tag: element.tagName,
        role: element.getAttribute("role") || "",
        text: textOf(element).trim().replace(/\\s+/g, " ").slice(0, 120),
        enabled: enabled(target)
      }};
    }});
}})()
"""


def _text_sequence_script(first: str, second: str) -> str:
    return f"""
{_dom_helpers()}
(() => {{
  const first = {json.dumps(first, ensure_ascii=False)};
  const second = {json.dumps(second, ensure_ascii=False)};
  const text = norm(document.body?.innerText || document.body?.textContent || "");
  const firstText = norm(first);
  const secondText = norm(second);
  const firstIndex = text.indexOf(firstText);
  const secondIndex = firstIndex >= 0 ? text.indexOf(secondText, firstIndex + firstText.length) : -1;
  return secondIndex >= 0
    ? {{ ok: true, first: firstText, second: secondText }}
    : {{ ok: false, first: firstText, second: secondText }};
}})()
"""


def _checkbox_state_script(name: str) -> str:
    return f"""
{_dom_helpers()}
(() => {{
  const target = {json.dumps(name, ensure_ascii=False)};
  for (const root of activeSearchRoots()) {{
    const labels = Array.from(root.querySelectorAll("label")).filter((label) => visible(label) && isMatch(label, target));
    for (const label of labels) {{
      const input = label.querySelector("input[type='checkbox'],input[type='radio']");
      if (input) return {{ ok: true, checked: input.checked === true, text: textOf(label) }};
    }}
  }}
  return {{ ok: false, reason: "checkbox not found", target }};
}})()
"""


def _dom_helpers() -> str:
    return r"""
function norm(value) {
  return String(value || "").replace(/\s+/g, "").trim();
}
function visible(element) {
  if (!element || element === document.documentElement) return false;
  const style = window.getComputedStyle(element);
  if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function textOf(element) {
  const parts = [
    element.getAttribute?.("aria-label"),
    element.getAttribute?.("title"),
    element.getAttribute?.("placeholder"),
    element.value,
    element.innerText,
    element.textContent
  ];
  return parts.filter(Boolean).join(" ");
}
function isMatch(element, target) {
  const left = norm(textOf(element));
  const right = norm(target);
  if (!right.startsWith("取消") && left.startsWith(`取消${right}`)) return false;
  return left === right || left.includes(right);
}
function elementRank(element, target) {
  const left = norm(textOf(element));
  const right = norm(target);
  const exactPenalty = left === right ? 0 : 1;
  const interactivePenalty = element.matches?.("button,a,input,textarea,label,[contenteditable='true'],[role='button'],[role='link'],[tabindex]") ? 0 : 1;
  return [exactPenalty, interactivePenalty, left.length];
}
function findByName(target) {
  const selector = "button,a,input,textarea,[contenteditable='true'],[role],[tabindex],label,div,span";
  const roots = activeSearchRoots();
  for (const root of roots) {
    const found = Array.from(root.querySelectorAll(selector))
      .filter((element) => visible(element) && isMatch(element, target))
      .sort((left, right) => {
        const a = elementRank(left, target);
        const b = elementRank(right, target);
        return a[0] - b[0] || a[1] - b[1] || a[2] - b[2];
      })[0] || null;
    if (found) return found;
  }
  return null;
}
function activeSearchRoots() {
  const modalSelector = ".modal-overlay,.export-dialog,[role='dialog'],.modal";
  const modals = Array.from(document.querySelectorAll(modalSelector)).filter(visible);
  if (modals.length) return modals.reverse();
  return [document];
}
function disabled(element) {
  let current = element;
  while (current && current !== document.body) {
    if (current.disabled === true) return true;
    if (current.getAttribute?.("disabled") !== null) return true;
    if (current.getAttribute?.("aria-disabled") === "true") return true;
    current = current.parentElement;
  }
  const className = element.className || "";
  if (/\b(disabled|is-disabled|van-button--disabled)\b/.test(className)) return true;
  if (window.getComputedStyle(element).pointerEvents === "none") return true;
  return false;
}
function enabled(element) {
  return visible(element) && !disabled(element);
}
function clickableAncestor(element) {
  if (element.matches?.("input[readonly]")) {
    const siblingButton = element.parentElement?.querySelector?.("button");
    if (siblingButton && visible(siblingButton)) return siblingButton;
  }
  let current = element;
  while (current && current !== document.body) {
    if (current.matches?.("button,a,input,textarea,label,[contenteditable='true'],[role='button'],[role='link'],[tabindex]")) {
      return current;
    }
    if (typeof current.onclick === "function" || window.getComputedStyle(current).cursor === "pointer") {
      return current;
    }
    current = current.parentElement;
  }
  return element;
}
function clickElement(element) {
  if (element.matches?.("label,input[type='checkbox'],input[type='radio']")) {
    element.click();
    return;
  }
  const rect = element.getBoundingClientRect();
  const clientX = rect.left + rect.width / 2;
  const clientY = rect.top + rect.height / 2;
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
    element.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window, clientX, clientY }));
  }
}
function labelTextFor(field) {
  const id = field.getAttribute("id");
  const labels = [];
  if (id) {
    const explicit = document.querySelector(`label[for="${CSS.escape(id)}"]`);
    if (explicit) labels.push(textOf(explicit));
  }
  const parentLabel = field.closest("label");
  if (parentLabel) labels.push(textOf(parentLabel));
  const parent = field.parentElement;
  if (parent) labels.push(textOf(parent));
  return labels.join(" ");
}
function findField(fieldName) {
  const fields = Array.from(document.querySelectorAll("input:not([type='hidden']),textarea,[contenteditable='true']"))
    .filter(visible);
  if (!fieldName) return fields[0] || null;
  return fields.find((field) => norm(textOf(field) + " " + labelTextFor(field)).includes(norm(fieldName))) || null;
}
"""


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise DriverUnavailable("CDP WebSocket closed while receiving data.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_until(sock: socket.socket, marker: bytes) -> bytes:
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data
