"""Thin Telegram Bot API client + update/keyboard helpers.

Supports both the cloud Bot API and a self-hosted Local Bot API server (set
runtime.local_bot_api_base_url) for large files. One intake mechanism only
(long polling here); never start a second consumer of the same token.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

CLOUD_API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


@dataclass
class Button:
    text: str
    data: str  # callback_data, must be <= 64 bytes


def keyboard(rows: list[list[Button]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": b.text, "callback_data": b.data} for b in row] for row in rows
        ]
    }


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str | None = None,
        timeout: float = 65.0,
    ):
        if not token:
            raise TelegramError("Telegram bot token is not configured")
        root = (base_url or CLOUD_API).rstrip("/")
        self._api = f"{root}/bot{token}"
        self._file_root = f"{root}/file/bot{token}"
        self._is_local = base_url is not None and "api.telegram.org" not in root
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    # -- low level -----------------------------------------------------------
    def call(self, method: str, data: dict[str, Any] | None = None,
             files: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        url = f"{self._api}/{method}"
        payload = {k: v for k, v in (data or {}).items() if v is not None}
        # Telegram wants nested objects JSON-encoded in multipart/form fields.
        import json as _json
        for k, v in list(payload.items()):
            if isinstance(v, (dict, list)):
                payload[k] = _json.dumps(v)
        try:
            r = self._client.post(url, data=payload, files=files, timeout=timeout)
        except httpx.HTTPError as e:
            raise TelegramError(f"network error: {e}") from e
        try:
            body = r.json()
        except Exception:
            raise TelegramError(f"non-JSON response ({r.status_code})", code=r.status_code)
        if not body.get("ok"):
            params = body.get("parameters") or {}
            raise TelegramError(
                body.get("description", "Telegram API error"),
                code=body.get("error_code"),
                retry_after=params.get("retry_after"),
            )
        return body["result"]

    # -- identity / setup ----------------------------------------------------
    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        return self.call("getChat", {"chat_id": chat_id})

    def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        return self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    def create_forum_topic(self, chat_id: int, name: str) -> dict[str, Any]:
        return self.call("createForumTopic", {"chat_id": chat_id, "name": name})

    # -- updates -------------------------------------------------------------
    def get_updates(self, offset: int, timeout: int = 50) -> list[dict[str, Any]]:
        try:
            return self.call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": timeout,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=timeout + 15,
            )
        except TelegramError as e:
            if e.retry_after:
                time.sleep(e.retry_after)
                return []
            raise

    # -- sending -------------------------------------------------------------
    def send_message(self, chat_id: int, text: str, *, thread_id: int | None = None,
                     reply_markup: dict | None = None, reply_to: int | None = None) -> dict[str, Any]:
        return self.call("sendMessage", {
            "chat_id": chat_id, "text": text, "message_thread_id": thread_id,
            "reply_markup": reply_markup, "reply_to_message_id": reply_to,
            "disable_web_page_preview": True,
        })

    def _send_file(self, method: str, field: str, chat_id: int, file: str | Path,
                   *, caption: str | None, thread_id: int | None,
                   reply_markup: dict | None, extra: dict | None = None) -> dict[str, Any]:
        data = {"chat_id": chat_id, "caption": caption,
                "message_thread_id": thread_id, "reply_markup": reply_markup}
        if extra:
            data.update(extra)
        # file may be a Telegram file_id (str, no path) or a local path.
        if isinstance(file, (str, Path)) and Path(str(file)).exists():
            with open(file, "rb") as fh:
                return self.call(method, data, files={field: fh})
        data[field] = str(file)  # treat as file_id / URL
        return self.call(method, data)

    def send_photo(self, chat_id, file, *, caption=None, thread_id=None, reply_markup=None):
        return self._send_file("sendPhoto", "photo", chat_id, file,
                               caption=caption, thread_id=thread_id, reply_markup=reply_markup)

    def send_document(self, chat_id, file, *, caption=None, thread_id=None, reply_markup=None):
        return self._send_file("sendDocument", "document", chat_id, file,
                               caption=caption, thread_id=thread_id, reply_markup=reply_markup)

    def send_video(self, chat_id, file, *, caption=None, thread_id=None, reply_markup=None,
                   duration=None, width=None, height=None):
        extra = {k: v for k, v in {"duration": duration, "width": width,
                                   "height": height, "supports_streaming": True}.items()
                 if v is not None}
        return self._send_file("sendVideo", "video", chat_id, file,
                               caption=caption, thread_id=thread_id,
                               reply_markup=reply_markup, extra=extra)

    def copy_message(self, chat_id: int, from_chat_id: int, message_id: int, *,
                     thread_id: int | None = None, caption: str | None = None,
                     reply_markup: dict | None = None) -> dict[str, Any]:
        """copyMessage preserves the media file but lets us replace the caption and
        drop the regeneration controls on the copy."""
        return self.call("copyMessage", {
            "chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id,
            "message_thread_id": thread_id, "caption": caption, "reply_markup": reply_markup,
        })

    def edit_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict | None) -> None:
        try:
            self.call("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup,
            })
        except TelegramError:
            pass  # message may be too old / unchanged; non-fatal

    def answer_callback(self, callback_id: str, text: str | None = None, alert: bool = False) -> None:
        try:
            self.call("answerCallbackQuery", {
                "callback_query_id": callback_id, "text": text, "show_alert": alert,
            })
        except TelegramError:
            pass

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError:
            pass

    # -- files ---------------------------------------------------------------
    def download_file(self, file_id: str, dest_path: str | Path, *, chunk: int = 1 << 20) -> str:
        """Stream a file to disk (never load into RAM). With a Local Bot API
        server the file may already be on disk via file_path."""
        info = self.call("getFile", {"file_id": file_id})
        file_path = info["file_path"]
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Local Bot API returns an absolute local path we can copy directly.
        if self._is_local and Path(file_path).is_absolute() and Path(file_path).exists():
            import shutil
            shutil.copyfile(file_path, dest)
            return str(dest)
        url = f"{self._file_root}/{file_path}"
        with self._client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise TelegramError(f"file download failed ({resp.status_code})")
            with open(dest, "wb") as fh:
                for block in resp.iter_bytes(chunk):
                    fh.write(block)
        return str(dest)


def download_url_stream(url: str, dest_path: str | Path, *, chunk: int = 1 << 20,
                        timeout: float = 120.0) -> str:
    """Generic streaming download (used for resolved Instagram media URLs)."""
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        with c.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise TelegramError(f"media download failed ({resp.status_code})")
            with open(dest, "wb") as fh:
                for block in resp.iter_bytes(chunk):
                    fh.write(block)
    return str(dest)
