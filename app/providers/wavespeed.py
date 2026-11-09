"""WaveSpeed adapter: media upload, model submission, prediction polling.

Flow (per the build guide and WaveSpeed docs [1][2][4]):
  1. Upload local media -> get a hosted URL.
  2. POST the model endpoint with the prompt + image/video URLs -> get data.id.
  3. GET /api/v3/predictions/{id}/result until a terminal status.

The caller is responsible for persisting `submitting` BEFORE calling submit(),
storing the returned id immediately, and only polling that id afterwards.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


class WaveSpeedError(RuntimeError):
    """Base provider error with a short, safe message."""

    def __init__(self, message: str, *, stage: str = "", retryable: bool = False):
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable


class InsufficientCredits(WaveSpeedError):  # HTTP 402
    pass


class RateLimited(WaveSpeedError):  # HTTP 429
    def __init__(self, message: str, retry_after: float = 5.0, stage: str = ""):
        super().__init__(message, stage=stage, retryable=True)
        self.retry_after = retry_after


class ContentRejected(WaveSpeedError):
    """Provider refused the content. Never retried blindly."""


@dataclass
class Prediction:
    id: str
    status: str                 # created|processing|completed|failed
    outputs: list[str]          # URL outputs
    text: str | None            # for structured/text outputs (captions)
    raw: dict[str, Any]

    @property
    def is_terminal(self) -> bool:
        return self.status in {"completed", "failed", "canceled", "cancelled"}

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"


class WaveSpeedClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.wavespeed.ai",
        *,
        timeout: float = 60.0,
    ):
        if not api_key:
            raise WaveSpeedError("WaveSpeed API key is not configured", stage="config")
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    # -- validation ----------------------------------------------------------
    def validate(self) -> bool:
        """Lightweight auth check without a paid generation. Returns True if the
        key is accepted. Does not guarantee available credits."""
        try:
            r = self._client.get("/api/v3/balance")
            if r.status_code in (200, 404):
                # 404 only means this account has no balance endpoint; auth still
                # passed if it was not 401/403.
                return True
            if r.status_code in (401, 403):
                return False
            return r.status_code < 500
        except httpx.HTTPError:
            return False

    # -- media upload --------------------------------------------------------
    def upload_media(self, file_path: str, content_type: str | None = None) -> str:
        """Upload a local file, return a hosted URL usable by model endpoints."""
        import mimetypes

        ctype = content_type or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        with open(file_path, "rb") as fh:
            files = {"file": (file_path.rsplit("/", 1)[-1], fh, ctype)}
            r = self._client.post("/api/v3/media/upload", files=files)
        self._raise_for_status(r, stage="upload")
        data = r.json()
        url = (
            _dig(data, "data", "url")
            or _dig(data, "data", "download_url")
            or data.get("url")
        )
        if not url:
            raise WaveSpeedError("Upload returned no media URL", stage="upload")
        return url

    # -- submission ----------------------------------------------------------
    def submit(self, model: str, payload: dict[str, Any]) -> str:
        """POST a model endpoint. Returns the prediction id (data.id)."""
        r = self._client.post(f"/api/v3/{model}", json=payload)
        self._raise_for_status(r, stage="submit")
        data = r.json()
        pid = _dig(data, "data", "id") or data.get("id")
        if not pid:
            raise WaveSpeedError("Submit returned no prediction id", stage="submit")
        return str(pid)

    # -- polling -------------------------------------------------------------
    def get_result(self, prediction_id: str) -> Prediction:
        r = self._client.get(f"/api/v3/predictions/{prediction_id}/result")
        self._raise_for_status(r, stage="poll")
        data = r.json()
        node = data.get("data", data)
        status = str(node.get("status", "processing")).lower()
        outputs = node.get("outputs") or []
        if isinstance(outputs, str):
            outputs = [outputs]
        url_outputs = [o for o in outputs if isinstance(o, str) and o.startswith("http")]
        text_output = None
        # Some models return text in outputs or in a dedicated field.
        non_url = [o for o in outputs if isinstance(o, str) and not o.startswith("http")]
        if non_url:
            text_output = "\n".join(non_url).strip()
        for key in ("text", "output_text", "result"):
            if node.get(key) and isinstance(node[key], str):
                text_output = node[key].strip()
        err = node.get("error")
        if status == "failed" and err and _looks_like_content_rejection(str(err)):
            raise ContentRejected(_safe_error(str(err)), stage="poll")
        return Prediction(
            id=prediction_id,
            status=status,
            outputs=url_outputs,
            text=text_output,
            raw=node,
        )

    def poll_until_terminal(
        self,
        prediction_id: str,
        *,
        interval: float = 5.0,
        timeout: float = 1800.0,
        should_stop=None,
    ) -> Prediction:
        """Poll a single id until terminal, with backoff on transient network
        errors. Raises WaveSpeedError(retryable=False) -> 'uncertain' decision is
        made by the caller on timeout."""
        deadline = time.time() + timeout
        delay = interval
        while True:
            if should_stop and should_stop():
                raise WaveSpeedError("Polling cancelled", stage="poll", retryable=False)
            try:
                pred = self.get_result(prediction_id)
                if pred.is_terminal:
                    return pred
                delay = interval
            except RateLimited as e:
                time.sleep(min(e.retry_after, 60))
            except httpx.HTTPError:
                # transient network error: backoff with simple jitter
                delay = min(delay * 1.7, 60)
                time.sleep(delay + _jitter(delay))
                continue
            if time.time() > deadline:
                raise WaveSpeedError(
                    "Timed out waiting for provider result",
                    stage="poll",
                    retryable=False,
                )
            time.sleep(interval + _jitter(interval))

    # -- error mapping -------------------------------------------------------
    def _raise_for_status(self, r: httpx.Response, *, stage: str) -> None:
        if r.status_code < 400:
            return
        if r.status_code == 402:
            raise InsufficientCredits("WaveSpeed: insufficient credits", stage=stage)
        if r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", "5") or 5)
            raise RateLimited("WaveSpeed: rate limited", retry_after=retry_after, stage=stage)
        if r.status_code in (401, 403):
            raise WaveSpeedError("WaveSpeed: authentication failed", stage=stage)
        if r.status_code >= 500:
            raise WaveSpeedError(
                f"WaveSpeed: server error ({r.status_code})", stage=stage, retryable=True
            )
        raise WaveSpeedError(
            f"WaveSpeed: request failed ({r.status_code})", stage=stage
        )


# -- helpers -----------------------------------------------------------------
def _dig(data: dict[str, Any], *keys: str) -> Any:
    node: Any = data
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node


def _jitter(base: float) -> float:
    import random

    return random.uniform(0, base * 0.3)


def _looks_like_content_rejection(message: str) -> bool:
    m = message.lower()
    return any(
        s in m
        for s in ("content", "policy", "nsfw", "moderation", "rejected", "not allowed")
    )


def _safe_error(message: str) -> str:
    """Trim provider error to a short, non-leaking string."""
    text = message.strip().splitlines()[0]
    return text[:200]
