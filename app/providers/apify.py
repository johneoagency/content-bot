"""Apify adapter for Instagram URL ingestion (optional module) [3].

If no token is configured the whole module is inert and the bot tells the user
how to enable it; file uploads keep working. Run id and dataset id are stored by
the caller so an in-flight run can be resumed after a restart.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


class ApifyError(RuntimeError):
    def __init__(self, message: str, *, stage: str = "", retryable: bool = False):
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable


@dataclass
class ApifyRun:
    run_id: str
    dataset_id: str | None
    status: str

    @property
    def is_terminal(self) -> bool:
        return self.status in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


class ApifyClient:
    def __init__(self, token: str, actor: str, *, timeout: float = 60.0):
        if not token:
            raise ApifyError("Apify token is not configured", stage="config")
        self.actor = actor
        self._token = token
        self._client = httpx.Client(base_url="https://api.apify.com/v2", timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _p(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {"token": self._token}
        if extra:
            params.update(extra)
        return params

    def validate(self) -> bool:
        """Confirm the token and that the configured actor is reachable."""
        actor_path = self.actor.replace("/", "~")
        try:
            r = self._client.get(f"/acts/{actor_path}", params=self._p())
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def start_run(self, urls: list[str]) -> ApifyRun:
        actor_path = self.actor.replace("/", "~")
        body = {"directUrls": urls, "resultsType": "posts", "addParentData": False}
        r = self._client.post(f"/acts/{actor_path}/runs", params=self._p(), json=body)
        if r.status_code == 402:
            raise ApifyError("Apify: insufficient credits", stage="start")
        if r.status_code in (401, 403):
            raise ApifyError("Apify: authentication failed", stage="start")
        if r.status_code >= 500:
            raise ApifyError("Apify: server error", stage="start", retryable=True)
        if r.status_code >= 400:
            raise ApifyError(f"Apify: start failed ({r.status_code})", stage="start")
        data = r.json().get("data", {})
        return ApifyRun(
            run_id=data.get("id", ""),
            dataset_id=data.get("defaultDatasetId"),
            status=data.get("status", "RUNNING"),
        )

    def get_run(self, run_id: str) -> ApifyRun:
        r = self._client.get(f"/actor-runs/{run_id}", params=self._p())
        if r.status_code >= 400:
            raise ApifyError(f"Apify: run status failed ({r.status_code})", stage="poll")
        data = r.json().get("data", {})
        return ApifyRun(
            run_id=run_id,
            dataset_id=data.get("defaultDatasetId"),
            status=data.get("status", "RUNNING"),
        )

    def wait_for_run(self, run_id: str, *, interval: float = 5.0, timeout: float = 600.0) -> ApifyRun:
        deadline = time.time() + timeout
        while True:
            run = self.get_run(run_id)
            if run.is_terminal:
                return run
            if time.time() > deadline:
                raise ApifyError("Apify: run timed out", stage="poll", retryable=False)
            time.sleep(interval)

    def dataset_items(self, dataset_id: str) -> list[dict[str, Any]]:
        r = self._client.get(
            f"/datasets/{dataset_id}/items", params=self._p({"clean": "true"})
        )
        if r.status_code >= 400:
            raise ApifyError("Apify: dataset fetch failed", stage="dataset")
        return r.json()

    @staticmethod
    def extract_video_urls(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pull resolvable video URLs out of scraped posts. Each distinct video
        becomes its own entry. Returns [{media_id, url, is_video}]."""
        results: list[dict[str, Any]] = []
        for item in items:
            post_id = str(item.get("id") or item.get("shortCode") or item.get("code") or "")
            # Single video post
            if item.get("videoUrl"):
                results.append({"media_id": post_id, "url": item["videoUrl"], "is_video": True})
            # Carousel / sidecar children
            for child in item.get("childPosts") or item.get("sidecarMedia") or []:
                if isinstance(child, dict) and child.get("videoUrl"):
                    cid = str(child.get("id") or post_id)
                    results.append({"media_id": cid, "url": child["videoUrl"], "is_video": True})
        return results
