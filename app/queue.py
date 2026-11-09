"""Orchestrator: update dispatch, command handling, callback buttons and the
worker loops that drive jobs through the durable state machine.

Separation of concerns:
  * The dispatcher thread only reads updates and enqueues work (fast, no network
    generation calls).
  * Worker threads claim jobs atomically and perform the slow provider calls.
  * Every paid POST is preceded by a persisted `submitting` state; the returned
    id is stored immediately and only that id is polled afterward.
"""
from __future__ import annotations

import secrets as _secrets
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from . import captions, images, instagram, videos
from .config import Config, DATA_DIR, SECRETS_DIR, redact
from .providers.apify import ApifyClient, ApifyError
from .providers.wavespeed import (
    ContentRejected,
    InsufficientCredits,
    Prediction,
    RateLimited,
    WaveSpeedClient,
    WaveSpeedError,
)
from .storage import (
    KIND_CAPTION,
    KIND_IMAGE,
    KIND_INSTAGRAM_FETCH,
    KIND_VIDEO_FINAL,
    KIND_VIDEO_PREVIEW,
    S_AWAITING_ACCEPT,
    S_CANCELLED,
    S_DONE,
    S_FAILED,
    S_POLLING,
    S_PREPARING,
    S_PREVIEW_READY,
    S_QUEUED,
    S_READY,
    S_SENDING,
    S_SUBMITTING,
    S_UNCERTAIN,
    S_VIDEO_QUEUED,
    Job,
    Storage,
)
from .telegram import Button, TelegramClient, TelegramError, download_url_stream, keyboard

JOBS_DIR = DATA_DIR / "jobs"


def new_job_id() -> str:
    return _secrets.token_hex(6)


def _job_dir(job_id: str) -> Path:
    d = JOBS_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


class Orchestrator:
    def __init__(self, config: Config, storage: Storage):
        self.cfg = config
        self.db = storage
        self.tg = TelegramClient(
            config.secrets.telegram_bot_token,
            base_url=config.get("runtime", "local_bot_api_base_url"),
        )
        self.ws = WaveSpeedClient(
            config.secrets.wavespeed_api_key,
            base_url=config.get("providers", "wavespeed_base_url",
                                default="https://api.wavespeed.ai"),
        )
        self.apify: ApifyClient | None = None
        if config.secrets.has_apify:
            self.apify = ApifyClient(
                config.secrets.apify_token,
                config.get("providers", "apify_actor",
                           default="intropix/instagram-posts-reels-scraper"),
            )
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- paused flag (persisted) ------------------------------------------
    @property
    def paused(self) -> bool:
        return self.db.kv_get("paused", "0") == "1"

    def set_paused(self, value: bool) -> None:
        self.db.kv_set("paused", "1" if value else "0")

    # ---- small helpers -----------------------------------------------------
    def _chat(self) -> int:
        return self.cfg.group_chat_id

    def _topic(self, key: str) -> int | None:
        return self.cfg.topic_id(key)

    def _notice(self, topic_key: str, text: str) -> None:
        """Queue a user-facing notice (delivered by the notice loop, with
        secrets stripped)."""
        safe = redact(text, self.cfg.secrets)
        self.db.add_notice(self._chat(), self._topic(topic_key), safe)

    def _reference(self) -> dict[str, Any] | None:
        path = self.cfg.get("reference", "image_path")
        if not path or not Path(path).exists():
            return None
        if not self.cfg.get("reference", "usage_rights_confirmed"):
            return None
        return {
            "path": path,
            "hash": self.cfg.get("reference", "image_hash"),
        }

    def _authorized(self, job: Job, user_id: int | None) -> bool:
        return (job.owner is not None and user_id == job.owner) or self.cfg.is_admin(user_id)

    # ====================================================================
    #  DISPATCH
    # ====================================================================
    def run(self) -> None:
        self.db.recover_locks_on_boot()
        me = self.tg.get_me()
        self._notice("tech_log", f"Bot online as @{me.get('username','?')}. Intake resumed.")
        # Start workers.
        self._spawn(self._loop_images, "img")
        self._spawn(self._loop_previews, "prev")
        self._spawn(self._loop_wan, "wan")
        self._spawn(self._loop_captions, "cap")
        if self.apify is not None:
            self._spawn(self._loop_instagram, "ig")
        self._spawn(self._loop_notices, "notice")
        try:
            self._dispatch_loop()
        finally:
            self._stop.set()

    def stop(self) -> None:
        self._stop.set()

    def _spawn(self, target, name: str) -> None:
        t = threading.Thread(target=self._guard(target), name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _guard(self, fn):
        def wrapper():
            while not self._stop.is_set():
                try:
                    fn()
                    return
                except Exception:  # keep a worker alive across unexpected errors
                    self._log(traceback.format_exc())
                    time.sleep(2)
        return wrapper

    def _log(self, text: str) -> None:
        print(redact(text, self.cfg.secrets), flush=True)

    def _dispatch_loop(self) -> None:
        offset = self.db.get_last_update_id() + 1
        while not self._stop.is_set():
            try:
                updates = self.tg.get_updates(offset, timeout=50)
            except TelegramError as e:
                self._log(f"getUpdates error: {e}")
                time.sleep(3)
                continue
            for upd in updates:
                uid = upd.get("update_id", 0)
                offset = max(offset, uid + 1)
                self.db.set_last_update_id(uid)
                if not self.db.mark_update_seen(uid):
                    continue  # already processed (restart replay safe)
                try:
                    self._handle_update(upd)
                except Exception:
                    self._log(traceback.format_exc())

    def _handle_update(self, upd: dict[str, Any]) -> None:
        if "callback_query" in upd:
            self._handle_callback(upd["callback_query"])
        elif "message" in upd:
            self._handle_message(upd["message"])

    # ---- messages ----------------------------------------------------------
    def _handle_message(self, msg: dict[str, Any]) -> None:
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        user = msg.get("from", {})
        user_id = user.get("id")
        thread_id = msg.get("message_thread_id")
        text = msg.get("text") or msg.get("caption") or ""

        # Private chat with an admin: secure setup flow (key/reference intake).
        if chat.get("type") == "private":
            self._handle_private(msg, user_id, text)
            return

        # Only our configured group is served.
        if chat_id != self._chat():
            return

        # Commands.
        if text.startswith("/"):
            self._handle_command(text, chat_id, thread_id, user_id)
            return

        # A reply that answers a pending "regenerate with prompt" request.
        pending = self.db.take_pending_prompt(user_id, chat_id, thread_id)
        if pending and pending["purpose"] == "regen_prompt" and text.strip():
            self._apply_regen_prompt(pending, text.strip(), user_id)
            return

        topic = self.cfg.topic_for_thread(thread_id if thread_id is not None else 0)
        if topic == "content_input":
            self._intake_image(msg, user_id, chat_id)
        elif topic == "video_input":
            self._intake_video_or_url(msg, user_id, chat_id, text)

    def _handle_private(self, msg: dict[str, Any], user_id: int | None, text: str) -> None:
        if not self.cfg.is_admin(user_id):
            return
        pending = self.db.take_pending_prompt(user_id, msg["chat"]["id"], None)
        if pending and pending["purpose"] == "set_key" and text.strip():
            # Store the WaveSpeed key privately and delete the message.
            self._store_secret_wavespeed(text.strip())
            self.tg.delete_message(msg["chat"]["id"], msg["message_id"])
            self.tg.send_message(msg["chat"]["id"], "Key stored privately and message deleted.")
            self._notice("tech_log", "WaveSpeed key updated by admin (value not shown).")
            return
        # Default help in private.
        self.tg.send_message(
            msg["chat"]["id"],
            "Admin DM. Use the Tech Log setup buttons/commands in the group. "
            "Secrets sent here are stored privately and the message is deleted.",
        )

    # ---- commands ----------------------------------------------------------
    def _handle_command(self, text: str, chat_id: int, thread_id: int | None, user_id: int | None) -> None:
        cmd = text.split()[0].split("@")[0].lower()
        is_admin = self.cfg.is_admin(user_id)
        if cmd in ("/start", "/help"):
            self.tg.send_message(chat_id, self._help_text(), thread_id=thread_id)
        elif cmd == "/status":
            self.tg.send_message(chat_id, self._status_text(), thread_id=thread_id)
        elif cmd == "/pause":
            if not is_admin:
                return self._deny(chat_id, thread_id)
            self.set_paused(True)
            self.tg.send_message(chat_id, "Paused. No new paid submissions; in-flight paid jobs keep running.", thread_id=thread_id)
        elif cmd == "/resume":
            if not is_admin:
                return self._deny(chat_id, thread_id)
            self.set_paused(False)
            self.tg.send_message(chat_id, "Resumed.", thread_id=thread_id)
        elif cmd == "/prompt":
            if not is_admin:
                return self._deny(chat_id, thread_id)
            self.tg.send_message(chat_id, self._prompt_text(), thread_id=thread_id)

    def _deny(self, chat_id: int, thread_id: int | None) -> None:
        self.tg.send_message(chat_id, "Admins only.", thread_id=thread_id)

    def _help_text(self) -> str:
        return (
            "Content Bot\n"
            "• Content Input: send images (as files for best quality) → edited result in Content Output.\n"
            "• Video Input: send videos or Instagram URLs → preview, Accept, then final video.\n"
            "• Buttons: Regenerate, Regenerate with Prompt, Generate Caption, Accept.\n"
            "Commands: /status /pause /resume /prompt /help"
        )

    def _status_text(self) -> str:
        counts = self.db.counts_by_state()
        ref = "set" if self._reference() else "NOT set"
        parts = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "no jobs"
        return (
            f"Status: {'PAUSED' if self.paused else 'running'} | reference {ref} | "
            f"apify {'on' if self.apify else 'off'}\nJobs: {parts}"
        )

    def _prompt_text(self) -> str:
        pv = self.cfg.get("prompt_variables", default={})
        lines = [f"{k}: {v}" for k, v in pv.items() if not k.startswith("_")]
        return "Prompt variables (edit config.json → prompt_variables):\n" + "\n".join(lines)

    # ====================================================================
    #  INTAKE
    # ====================================================================
    def _image_file_id(self, msg: dict[str, Any]) -> str | None:
        if "photo" in msg and msg["photo"]:
            return msg["photo"][-1]["file_id"]  # largest size
        doc = msg.get("document")
        if doc and str(doc.get("mime_type", "")).startswith("image/"):
            return doc["file_id"]
        return None

    def _video_file_id(self, msg: dict[str, Any]) -> str | None:
        if "video" in msg and msg["video"]:
            return msg["video"]["file_id"]
        doc = msg.get("document")
        if doc and str(doc.get("mime_type", "")).startswith("video/"):
            return doc["file_id"]
        return None

    def _intake_image(self, msg: dict[str, Any], user_id: int, chat_id: int) -> None:
        file_id = self._image_file_id(msg)
        if not file_id:
            return
        job_id = new_job_id()
        self.db.create_job(
            job_id, KIND_IMAGE, owner=user_id, chat_id=chat_id,
            input_message_id=msg["message_id"], input_file_id=file_id,
            root_id=job_id, state=S_QUEUED,
        )
        self._snapshot_for_new_job(job_id, source_topic="content_input",
                                   destination_topic="content_output")
        self._notice("tech_log", f"Image job {job_id} queued.")

    def _intake_video_or_url(self, msg: dict[str, Any], user_id: int, chat_id: int, text: str) -> None:
        file_id = self._video_file_id(msg)
        if file_id:
            job_id = new_job_id()
            self.db.create_job(
                job_id, KIND_VIDEO_PREVIEW, owner=user_id, chat_id=chat_id,
                input_message_id=msg["message_id"], input_file_id=file_id,
                root_id=job_id, state=S_QUEUED,
            )
            self._snapshot_for_new_job(job_id, source_topic="video_input",
                                       destination_topic="video_output")
            self._notice("tech_log", f"Video job {job_id} queued.")
            return
        # Instagram URLs?
        refs, rejected = instagram.parse_message(text)
        if rejected and not refs:
            return  # unrelated text, ignore
        if refs and self.apify is None:
            self._notice("video_input",
                         "Instagram URL intake is disabled (no Apify token configured). "
                         "Send the video file directly, or set APIFY_TOKEN to enable URLs.")
            return
        for ref in refs:
            job_id = new_job_id()
            self.db.create_job(
                job_id, KIND_INSTAGRAM_FETCH, owner=user_id, chat_id=chat_id,
                input_message_id=msg["message_id"], source_path=None,
                root_id=job_id, state=S_QUEUED,
                extra={"url": ref.url, "shortcode": ref.shortcode},
            )
            self._notice("tech_log", f"Instagram fetch {job_id} queued for {ref.shortcode}.")

    def _snapshot_for_new_job(self, job_id: str, *, source_topic: str, destination_topic: str,
                              params: dict[str, Any] | None = None) -> None:
        ref = self._reference()
        self.db.save_snapshot(
            job_id,
            reference_path=ref["path"] if ref else None,
            reference_hash=ref["hash"] if ref else None,
            image_prompt=self.cfg.prompts["image"].text,
            image_prompt_version=self.cfg.prompts["image"].version,
            video_prompt=self.cfg.prompts["video"].text,
            video_prompt_version=self.cfg.prompts["video"].version,
            caption_prompt=self.cfg.prompts["caption"].text,
            caption_prompt_version=self.cfg.prompts["caption"].version,
            resize_policy=self.cfg.get("generation", "aspect_change_default", default="padding"),
            source_topic=source_topic,
            destination_topic=destination_topic,
            config_version=self.cfg.config_version,
            params=_json_dumps(params or {}),
        )

    # ====================================================================
    #  CALLBACKS (buttons)
    # ====================================================================
    def _handle_callback(self, cq: dict[str, Any]) -> None:
        cb_id = cq.get("id")
        user_id = cq.get("from", {}).get("id")
        data = cq.get("data", "")
        msg = cq.get("message", {})
        # Idempotency: a duplicate click never triggers a second paid action.
        if not self.db.mark_callback_seen(cb_id):
            self.tg.answer_callback(cb_id, "Already handled.")
            return
        try:
            action, job_id, rev = (data.split(":") + ["", ""])[:3]
        except ValueError:
            self.tg.answer_callback(cb_id, "Unknown action.")
            return
        job = self.db.get_job(job_id)
        if not job:
            self.tg.answer_callback(cb_id, "This job is no longer available.")
            return
        if not self._authorized(job, user_id):
            self.tg.answer_callback(cb_id, "Only the uploader or an admin can do this.", alert=True)
            return

        if action == "rg":
            self._regenerate(job, user_id, custom_prompt=None)
            self.tg.answer_callback(cb_id, "Regenerating…")
        elif action == "rp":
            self.db.add_pending_prompt(user_id, self._chat(),
                                       msg.get("message_thread_id"), "regen_prompt", job_id)
            self.tg.answer_callback(cb_id, "Reply with the full prompt for this one output.")
        elif action == "cap":
            self._enqueue_caption(job)
            self.tg.answer_callback(cb_id, "Generating caption…")
        elif action == "ac":
            self._accept_preview(job, int(rev or 0), user_id, msg)
            self.tg.answer_callback(cb_id, "Accepted. Producing final video…")
        else:
            self.tg.answer_callback(cb_id, "Unknown action.")

    def _regenerate(self, job: Job, user_id: int, custom_prompt: str | None) -> None:
        if self.paused:
            self._notice("tech_log", "Paused: regeneration not submitted.")
            return
        snap = self.db.get_snapshot(job.job_id) or {}
        new_id = new_job_id()
        # New job reuses the ORIGINAL base + stored reference, never the produced
        # output as the new base.
        self.db.create_job(
            new_id, job.kind, owner=user_id, chat_id=job.chat_id,
            input_message_id=job.input_message_id, input_file_id=job.input_file_id,
            source_hash=job.source_hash, source_path=job.source_path,
            root_id=job.root_id or job.job_id, parent_id=job.job_id, state=S_QUEUED,
            extra={"custom_prompt": custom_prompt} if custom_prompt else {},
        )
        # Copy the snapshot forward (same reference/prompt versions), overriding
        # prompt only for this job if a custom prompt was given.
        fields = {k: snap.get(k) for k in (
            "reference_path", "reference_hash", "image_prompt", "image_prompt_version",
            "video_prompt", "video_prompt_version", "caption_prompt", "caption_prompt_version",
            "target_width", "target_height", "resize_policy", "source_topic",
            "destination_topic", "config_version",
        )}
        params = _json_loads(snap.get("params") or "{}")
        if custom_prompt:
            params["custom_prompt"] = custom_prompt
        fields["params"] = _json_dumps(params)
        self.db.save_snapshot(new_id, **fields)

    def _apply_regen_prompt(self, pending: dict[str, Any], prompt_text: str, user_id: int) -> None:
        job = self.db.get_job(pending["job_id"])
        if not job:
            return
        if len(prompt_text) > 4000:
            self._notice("tech_log", "Prompt too long; ignored.")
            return
        self._regenerate(job, user_id, custom_prompt=prompt_text)
        self._notice("tech_log", f"Regenerating {job.job_id} with a custom prompt.")

    def _accept_preview(self, job: Job, rev: int, user_id: int, msg: dict[str, Any]) -> None:
        if job.kind != KIND_VIDEO_PREVIEW:
            return
        prov = self.db.get_provider(job.job_id) or {}
        current_rev = int(prov.get("preview_revision") or 0)
        # Stale preview / double click never creates a second paid job.
        if rev != current_rev:
            self._notice("tech_log", "Stale preview Accept ignored; regenerate first.")
            return
        if not self.db.set_state(job.job_id, S_VIDEO_QUEUED, expect=[S_AWAITING_ACCEPT, S_PREVIEW_READY]):
            return  # already accepted / not in an acceptable state
        self.db.save_provider(job.job_id, approved_by=user_id, approved_revision=rev)
        # Create the final Wan job as a child.
        final_id = new_job_id()
        snap = self.db.get_snapshot(job.job_id) or {}
        self.db.create_job(
            final_id, KIND_VIDEO_FINAL, owner=job.owner, chat_id=job.chat_id,
            input_message_id=job.input_message_id, source_path=job.source_path,
            source_hash=job.source_hash, root_id=job.root_id or job.job_id,
            parent_id=job.job_id, state=S_QUEUED,
            extra={"approved_preview_path": prov.get("output_path")},
        )
        self.db.save_snapshot(final_id, **{k: snap.get(k) for k in (
            "reference_path", "reference_hash", "video_prompt", "video_prompt_version",
            "caption_prompt", "caption_prompt_version", "source_topic",
            "destination_topic", "config_version", "params",
        )})
        # Remove buttons from the accepted preview message.
        if msg:
            self.tg.edit_reply_markup(msg["chat"]["id"], msg["message_id"], None)

    def _enqueue_caption(self, job: Job) -> None:
        prov = self.db.get_provider(job.job_id) or {}
        receipt = prov.get("final_receipt")
        if not receipt:
            self._notice("tech_log", "No final media receipt for caption yet.")
            return
        cid = self.db.create_caption_job(job.job_id, str(receipt), "instagram_content")
        if cid is None:
            self._notice("tech_log", "A caption is already being generated for this output.")

    # ====================================================================
    #  WORKERS
    # ====================================================================
    def _loop_images(self) -> None:
        while not self._stop.is_set():
            job = self.db.claim_next([KIND_IMAGE], [S_QUEUED, S_POLLING, S_READY, S_SENDING], "img")
            if not job:
                time.sleep(1.0)
                continue
            try:
                self._process_image(job)
            except Exception:
                self._fail(job, "internal error")
                self._log(traceback.format_exc())
            finally:
                self.db.release(job.job_id)

    def _loop_previews(self) -> None:
        while not self._stop.is_set():
            job = self.db.claim_next([KIND_VIDEO_PREVIEW], [S_QUEUED, S_POLLING, S_READY, S_SENDING], "prev")
            if not job:
                time.sleep(1.0)
                continue
            try:
                self._process_preview(job)
            except Exception:
                self._fail(job, "internal error")
                self._log(traceback.format_exc())
            finally:
                self.db.release(job.job_id)

    def _loop_wan(self) -> None:
        while not self._stop.is_set():
            job = self.db.claim_next([KIND_VIDEO_FINAL], [S_VIDEO_QUEUED, S_POLLING, S_READY, S_SENDING], "wan")
            if not job:
                time.sleep(1.0)
                continue
            try:
                self._process_wan(job)
            except Exception:
                self._fail(job, "internal error")
                self._log(traceback.format_exc())
            finally:
                self.db.release(job.job_id)

    def _loop_captions(self) -> None:
        while not self._stop.is_set():
            rows = self._claim_caption()
            if not rows:
                time.sleep(1.0)
                continue
            try:
                self._process_caption(rows)
            except Exception:
                self._log(traceback.format_exc())

    def _loop_instagram(self) -> None:
        while not self._stop.is_set():
            job = self.db.claim_next([KIND_INSTAGRAM_FETCH], [S_QUEUED, S_POLLING], "ig")
            if not job:
                time.sleep(1.0)
                continue
            try:
                self._process_instagram(job)
            except Exception:
                self._fail(job, "instagram fetch error")
                self._log(traceback.format_exc())
            finally:
                self.db.release(job.job_id)

    def _loop_notices(self) -> None:
        while not self._stop.is_set():
            for n in self.db.pending_notices():
                try:
                    self.tg.send_message(n["chat_id"], n["text"], thread_id=n["thread_id"])
                    self.db.mark_notice_delivered(n["id"])
                except TelegramError:
                    time.sleep(1)
            time.sleep(1.0)

    # ---- image pipeline ----------------------------------------------------
    def _process_image(self, job: Job) -> None:
        if job.state == S_QUEUED:
            if not self._guard_paid(job):
                return
            self._prepare_source_image(job)
            self._submit_qwen(job, kind="image")
        elif job.state == S_POLLING:
            self._poll_and_deliver_image(job)
        elif job.state in (S_READY, S_SENDING):
            self._deliver_image(job)

    def _prepare_source_image(self, job: Job) -> None:
        self.db.set_state(job.job_id, S_PREPARING, expect=S_QUEUED)
        d = _job_dir(job.job_id)
        raw = str(d / "source.bin")
        self.tg.download_file(job.input_file_id, raw)
        info = images.inspect(raw, max_pixels=self.cfg.get("limits", "max_image_pixels", default=50_000_000))
        base_png = images.normalize_to_png(raw, str(d / "base.png"))
        self.db.set_state(job.job_id, S_PREPARING, source_path=base_png, source_hash=info.sha256)
        # Target geometry = received dimensions (match_source policy).
        self.db.save_snapshot(job.job_id, target_width=info.width, target_height=info.height)
        self._notice("tech_log", f"Image {job.job_id}: processing ({info.width}x{info.height}).")

    def _submit_qwen(self, job: Job, *, kind: str) -> None:
        ref = self._reference()
        if not ref:
            self.db.set_state(job.job_id, S_FAILED)
            self._notice("tech_log", "No reference image set. Use the Tech Log setup to add one.")
            return
        snap = self.db.get_snapshot(job.job_id) or {}
        params = _json_loads(snap.get("params") or "{}")
        prompt = params.get("custom_prompt") or snap.get("image_prompt")
        d = _job_dir(job.job_id)
        base_path = job.source_path or str(d / "base.png")
        # Uploads are not paid; do them before writing `submitting`.
        base_url = self.ws.upload_media(base_path, "image/png")
        ref_url = self.ws.upload_media(ref["path"])
        self.db.save_provider(job.job_id, provider="wavespeed",
                              model=self.cfg.get("providers", "qwen_image_model"))
        payload = {
            "prompt": prompt,
            "images": [base_url, ref_url],  # base first, reference second
        }
        tw, th = snap.get("target_width"), snap.get("target_height")
        if tw and th:
            payload["size"] = f"{tw}*{th}"
        seed = self.cfg.get("generation", "default_seed")
        if seed is not None:
            payload["seed"] = seed
        # Persist `submitting` BEFORE the unsafe paid POST.
        self.db.set_state(job.job_id, S_SUBMITTING, expect=[S_PREPARING, S_QUEUED])
        try:
            pid = self.ws.submit(self.cfg.get("providers", "qwen_image_model"), payload)
        except WaveSpeedError as e:
            return self._handle_provider_error(job, e, stage="submit")
        self.db.save_provider(job.job_id, prediction_id=pid)
        self.db.set_state(job.job_id, S_POLLING, expect=S_SUBMITTING)
        self._poll_and_deliver_image(job)

    def _poll_and_deliver_image(self, job: Job) -> None:
        prov = self.db.get_provider(job.job_id) or {}
        pid = prov.get("prediction_id")
        if not pid:
            self.db.set_state(job.job_id, S_UNCERTAIN)
            return
        try:
            pred = self.ws.poll_until_terminal(
                pid,
                interval=self.cfg.get("limits", "poll_interval_seconds", default=5),
                timeout=self.cfg.get("limits", "poll_timeout_seconds", default=1800),
                should_stop=self._stop.is_set,
            )
        except WaveSpeedError as e:
            return self._handle_provider_error(job, e, stage="poll")
        if not pred.succeeded or not pred.outputs:
            self.db.set_state(job.job_id, S_FAILED)
            self._notice("content_output", f"Image {job.job_id}: generation failed.")
            return
        d = _job_dir(job.job_id)
        produced = str(d / "produced.png")
        download_url_stream(pred.outputs[0], produced)
        snap = self.db.get_snapshot(job.job_id) or {}
        tw, th = snap.get("target_width"), snap.get("target_height")
        final_png = str(d / "final.png")
        if tw and th:
            info = images.enforce_output_geometry(produced, final_png, tw, th)
        else:
            info = images.inspect(produced)
            final_png = produced
        self.db.save_provider(job.job_id, output_path=final_png, output_hash=info.sha256,
                              output_url=pred.outputs[0])
        self.db.set_state(job.job_id, S_READY, expect=[S_POLLING, S_SUBMITTING])
        self._deliver_image(job)

    def _deliver_image(self, job: Job) -> None:
        prov = self.db.get_provider(job.job_id) or {}
        final_png = prov.get("output_path")
        if not final_png or not Path(final_png).exists():
            self.db.set_state(job.job_id, S_UNCERTAIN)
            return
        self.db.set_state(job.job_id, S_SENDING, expect=[S_READY, S_SENDING])
        # Send as a document to preserve quality.
        receipt = self.tg.send_document(
            self._chat(), final_png,
            thread_id=self._topic("content_output"),
            reply_markup=keyboard([[self._b("Regenerate", "rg", job.job_id)],
                                   [self._b("Regenerate with Prompt", "rp", job.job_id)],
                                   [self._b("Generate Caption", "cap", job.job_id)]]),
        )
        self.db.save_provider(job.job_id, final_receipt=receipt["message_id"],
                              final_file_id=_first_file_id(receipt))
        self.db.set_state(job.job_id, S_DONE, expect=S_SENDING)

    # ---- video preview pipeline -------------------------------------------
    def _process_preview(self, job: Job) -> None:
        if job.state == S_QUEUED:
            if not self._guard_paid(job):
                return
            self._prepare_source_video(job)
            self._submit_preview(job)
        elif job.state == S_POLLING:
            self._poll_and_deliver_preview(job)
        elif job.state in (S_READY, S_SENDING):
            self._deliver_preview(job)

    def _prepare_source_video(self, job: Job) -> None:
        self.db.set_state(job.job_id, S_PREPARING, expect=S_QUEUED)
        d = _job_dir(job.job_id)
        raw = str(d / "source.mp4")
        if job.input_file_id:
            self.tg.download_file(job.input_file_id, raw)
        elif job.source_path and Path(job.source_path).exists():
            raw = job.source_path
        if not videos.verify_decodable(raw):
            self.db.set_state(job.job_id, S_FAILED)
            self._notice("video_output", f"Video {job.job_id}: file is not decodable.")
            raise RuntimeError("undecodable")
        limit = self.cfg.get("generation", "video_trim_seconds", default=15)
        trimmed = str(d / "trimmed.mp4")
        info, was_trimmed = videos.ensure_trimmed(raw, trimmed, limit)
        frame = videos.first_frame(trimmed, str(d / "frame0.png"))
        img = images.inspect(frame)
        self.db.set_state(job.job_id, S_PREPARING, source_path=trimmed)
        self.db.save_snapshot(job.job_id, target_width=img.width, target_height=img.height)
        if was_trimmed:
            self._notice("video_output", f"Video {job.job_id}: trimmed to the first {limit}s.")

    def _submit_preview(self, job: Job) -> None:
        ref = self._reference()
        if not ref:
            self.db.set_state(job.job_id, S_FAILED)
            self._notice("tech_log", "No reference image set.")
            return
        snap = self.db.get_snapshot(job.job_id) or {}
        params = _json_loads(snap.get("params") or "{}")
        prompt = params.get("custom_prompt") or snap.get("image_prompt")
        d = _job_dir(job.job_id)
        frame_url = self.ws.upload_media(str(d / "frame0.png"), "image/png")
        ref_url = self.ws.upload_media(ref["path"])
        self.db.save_provider(job.job_id, provider="wavespeed",
                              model=self.cfg.get("providers", "qwen_image_model"))
        payload = {"prompt": prompt, "images": [frame_url, ref_url]}
        tw, th = snap.get("target_width"), snap.get("target_height")
        if tw and th:
            payload["size"] = f"{tw}*{th}"
        self.db.set_state(job.job_id, S_SUBMITTING, expect=[S_PREPARING, S_QUEUED])
        try:
            pid = self.ws.submit(self.cfg.get("providers", "qwen_image_model"), payload)
        except WaveSpeedError as e:
            return self._handle_provider_error(job, e, stage="submit")
        self.db.save_provider(job.job_id, prediction_id=pid)
        self.db.set_state(job.job_id, S_POLLING, expect=S_SUBMITTING)
        self._poll_and_deliver_preview(job)

    def _poll_and_deliver_preview(self, job: Job) -> None:
        prov = self.db.get_provider(job.job_id) or {}
        pid = prov.get("prediction_id")
        if not pid:
            self.db.set_state(job.job_id, S_UNCERTAIN)
            return
        try:
            pred = self.ws.poll_until_terminal(
                pid,
                interval=self.cfg.get("limits", "poll_interval_seconds", default=5),
                timeout=self.cfg.get("limits", "poll_timeout_seconds", default=1800),
                should_stop=self._stop.is_set,
            )
        except WaveSpeedError as e:
            return self._handle_provider_error(job, e, stage="poll")
        if not pred.succeeded or not pred.outputs:
            self.db.set_state(job.job_id, S_FAILED)
            self._notice("video_output", f"Preview {job.job_id}: failed.")
            return
        d = _job_dir(job.job_id)
        preview_png = str(d / "preview.png")
        download_url_stream(pred.outputs[0], preview_png)
        info = images.inspect(preview_png)
        self.db.save_provider(job.job_id, output_path=preview_png, output_hash=info.sha256,
                              output_url=pred.outputs[0])
        self.db.set_state(job.job_id, S_READY, expect=[S_POLLING, S_SUBMITTING])
        self._deliver_preview(job)

    def _deliver_preview(self, job: Job) -> None:
        prov = self.db.get_provider(job.job_id) or {}
        preview_png = prov.get("output_path")
        if not preview_png or not Path(preview_png).exists():
            self.db.set_state(job.job_id, S_UNCERTAIN)
            return
        rev = int(prov.get("preview_revision") or 0)
        self.db.set_state(job.job_id, S_SENDING, expect=[S_READY, S_SENDING])
        receipt = self.tg.send_photo(
            self._chat(), preview_png,
            caption=f"Preview {job.job_id} (rev {rev}). Accept to produce the final video.",
            thread_id=self._topic("video_output"),
            reply_markup=keyboard([
                [self._b("Accept", "ac", job.job_id, rev)],
                [self._b("Regenerate", "rg", job.job_id)],
                [self._b("Regenerate with Prompt", "rp", job.job_id)],
            ]),
        )
        self.db.save_provider(job.job_id, preview_receipt=receipt["message_id"])
        self.db.set_state(job.job_id, S_AWAITING_ACCEPT, expect=S_SENDING)

    # ---- Wan final video pipeline -----------------------------------------
    def _process_wan(self, job: Job) -> None:
        if job.state == S_VIDEO_QUEUED:
            if not self._guard_paid(job):
                return
            self._submit_wan(job)
        elif job.state == S_POLLING:
            self._poll_and_deliver_wan(job)
        elif job.state in (S_READY, S_SENDING):
            self._deliver_wan(job)

    def _submit_wan(self, job: Job) -> None:
        ref = self._reference()
        snap = self.db.get_snapshot(job.job_id) or {}
        prompt = snap.get("video_prompt")
        approved_preview = job.extra.get("approved_preview_path")
        src_video = job.source_path
        if not (approved_preview and src_video and ref):
            self.db.set_state(job.job_id, S_FAILED)
            self._notice("video_output", f"Final {job.job_id}: missing approved preview/source/reference.")
            return
        clip_url = self.ws.upload_media(src_video, "video/mp4")
        approved_url = self.ws.upload_media(approved_preview, "image/png")
        ref_url = self.ws.upload_media(ref["path"])
        self.db.save_provider(job.job_id, provider="wavespeed",
                              model=self.cfg.get("providers", "wan_video_model"))
        payload = {
            "prompt": prompt,
            "video": clip_url,
            "reference_images": [approved_url, ref_url],  # approved preview = Image 1
            "resolution": self.cfg.get("generation", "wan_resolution", default="720p"),
            "generate_audio": self.cfg.get("generation", "wan_generate_audio", default=True),
            "enable_prompt_expansion": self.cfg.get("generation", "wan_prompt_expansion", default=False),
        }
        self.db.set_state(job.job_id, S_SUBMITTING, expect=S_VIDEO_QUEUED)
        try:
            pid = self.ws.submit(self.cfg.get("providers", "wan_video_model"), payload)
        except WaveSpeedError as e:
            return self._handle_provider_error(job, e, stage="submit")
        self.db.save_provider(job.job_id, prediction_id=pid)
        self.db.set_state(job.job_id, S_POLLING, expect=S_SUBMITTING)
        self._poll_and_deliver_wan(job)

    def _poll_and_deliver_wan(self, job: Job) -> None:
        prov = self.db.get_provider(job.job_id) or {}
        pid = prov.get("prediction_id")
        if not pid:
            self.db.set_state(job.job_id, S_UNCERTAIN)
            return
        try:
            pred = self.ws.poll_until_terminal(
                pid,
                interval=self.cfg.get("limits", "poll_interval_seconds", default=5),
                timeout=self.cfg.get("limits", "poll_timeout_seconds", default=1800),
                should_stop=self._stop.is_set,
            )
        except WaveSpeedError as e:
            return self._handle_provider_error(job, e, stage="poll")
        if not pred.succeeded or not pred.outputs:
            self.db.set_state(job.job_id, S_FAILED)
            self._notice("video_output", f"Final video {job.job_id}: failed.")
            return
        d = _job_dir(job.job_id)
        out = str(d / "final.mp4")
        download_url_stream(pred.outputs[0], out)
        if not videos.verify_decodable(out):
            self.db.set_state(job.job_id, S_UNCERTAIN)
            self._notice("video_output", f"Final video {job.job_id}: downloaded file not decodable.")
            return
        info = videos.probe(out)
        self.db.save_provider(job.job_id, output_path=out, output_url=pred.outputs[0])
        self.db.update_extra(job.job_id, duration=info.duration, width=info.width, height=info.height)
        self.db.set_state(job.job_id, S_READY, expect=[S_POLLING, S_SUBMITTING])
        self._deliver_wan(job)

    def _deliver_wan(self, job: Job) -> None:
        prov = self.db.get_provider(job.job_id) or {}
        out = prov.get("output_path")
        if not out or not Path(out).exists():
            self.db.set_state(job.job_id, S_UNCERTAIN)
            return
        self.db.set_state(job.job_id, S_SENDING, expect=[S_READY, S_SENDING])
        receipt = self.tg.send_video(
            self._chat(), out,
            thread_id=self._topic("video_output"),
            duration=int(job.extra.get("duration", 0)) or None,
            width=job.extra.get("width"), height=job.extra.get("height"),
            reply_markup=keyboard([[self._b("Generate Caption", "cap", job.job_id)]]),
        )
        self.db.save_provider(job.job_id, final_receipt=receipt["message_id"],
                              final_file_id=_first_file_id(receipt))
        self.db.set_state(job.job_id, S_DONE, expect=S_SENDING)

    # ---- caption pipeline --------------------------------------------------
    def _claim_caption(self) -> dict[str, Any] | None:
        # Simple claim: pick one queued caption job and mark processing.
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT * FROM caption_jobs WHERE state='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self.db._conn.execute(
                "UPDATE caption_jobs SET state='processing' WHERE id=? AND state='queued'",
                (row["id"],),
            )
            self.db._conn.commit()
        return dict(row)

    def _process_caption(self, cap: dict[str, Any]) -> None:
        job = self.db.get_job(cap["output_job_id"])
        if not job:
            self.db.set_caption_state(cap["id"], "failed")
            return
        prov = self.db.get_provider(job.job_id) or {}
        snap = self.db.get_snapshot(job.job_id) or {}
        d = _job_dir(job.job_id)
        # Analyse the FINAL produced media.
        if job.kind == KIND_IMAGE:
            media_paths = [prov.get("output_path")]
        else:
            media_paths = videos.sample_frames(prov.get("output_path"), str(d / "capframes"), 3)
        media_paths = [p for p in media_paths if p and Path(p).exists()]
        if not media_paths:
            self.db.set_caption_state(cap["id"], "failed")
            return
        urls = [self.ws.upload_media(p, "image/png") for p in media_paths]
        payload = captions.build_vision_payload(
            self.cfg.get("providers", "vision_model", default="openai/gpt-4o-mini"),
            snap.get("caption_prompt") or self.cfg.prompts["caption"].text,
            urls,
        )
        try:
            pid = self.ws.submit(self.cfg.get("providers", "vision_model_endpoint",
                                              default="wavespeed-ai/any-llm/vision"), payload)
            pred = self.ws.poll_until_terminal(pid, interval=3, timeout=300)
        except WaveSpeedError as e:
            self.db.set_caption_state(cap["id"], "failed")
            self._notice("tech_log", f"Caption {job.job_id}: {e}")
            return
        caption = captions.validate_caption(pred.text or "")
        if not caption:
            self.db.set_caption_state(cap["id"], "rejected")
            self._notice("tech_log", f"Caption {job.job_id}: no usable caption produced.")
            return
        # copyMessage the SAME media into Instagram Content with the caption and
        # no regeneration controls.
        dest_thread = self._topic(cap.get("destination_topic") or "instagram_content")
        if dest_thread is None:
            self.db.set_caption_state(cap["id"], "failed")
            self._notice("tech_log", "Instagram Content topic not configured.")
            return
        self.tg.copy_message(
            self._chat(), self._chat(), int(cap["source_receipt"]),
            thread_id=dest_thread, caption=caption, reply_markup=None,
        )
        self.db.set_caption_state(cap["id"], "done", caption=caption)
        self._notice("tech_log", f"Caption posted to Instagram Content for {job.job_id}.")

    # ---- instagram fetch pipeline -----------------------------------------
    def _process_instagram(self, job: Job) -> None:
        if self.apify is None:
            self.db.set_state(job.job_id, S_FAILED)
            return
        url = job.extra.get("url")
        self.db.set_state(job.job_id, S_PREPARING, expect=S_QUEUED)
        try:
            run = self.apify.start_run([url])
            self.db.update_extra(job.job_id, run_id=run.run_id, dataset_id=run.dataset_id)
            self.db.save_provider(job.job_id, provider="apify", run_id=run.run_id)
            run = self.apify.wait_for_run(run.run_id)
            if run.status != "SUCCEEDED" or not run.dataset_id:
                self.db.set_state(job.job_id, S_FAILED)
                self._notice("video_input", f"Instagram {job.extra.get('shortcode')}: unavailable or private.")
                return
            items = self.apify.dataset_items(run.dataset_id)
        except ApifyError as e:
            self.db.set_state(job.job_id, S_FAILED)
            self._notice("video_input", f"Instagram fetch failed: {e}")
            return
        vids = self.apify.extract_video_urls(items)
        if not vids:
            self.db.set_state(job.job_id, S_FAILED)
            self._notice("video_input", f"Instagram {job.extra.get('shortcode')}: no supported video found.")
            return
        # Each video becomes its own preview job.
        for v in vids:
            child = new_job_id()
            d = _job_dir(child)
            raw = str(d / "source.mp4")
            try:
                download_url_stream(v["url"], raw)
            except Exception:
                self._notice("video_input", f"Instagram {v['media_id']}: media URL expired/unavailable.")
                continue
            if not videos.verify_decodable(raw):
                self._notice("video_input", f"Instagram {v['media_id']}: unsupported media format.")
                continue
            self.db.create_job(
                child, KIND_VIDEO_PREVIEW, owner=job.owner, chat_id=job.chat_id,
                input_message_id=job.input_message_id, source_path=raw,
                root_id=child, parent_id=job.job_id, state=S_QUEUED,
            )
            self._snapshot_for_new_job(child, source_topic="video_input",
                                       destination_topic="video_output")
        self.db.set_state(job.job_id, S_DONE, expect=S_PREPARING)

    # ====================================================================
    #  ERROR HANDLING & GUARDS
    # ====================================================================
    def _guard_paid(self, job: Job) -> bool:
        """Block new paid submissions while paused. Returns True if allowed."""
        if self.paused:
            self._notice("tech_log", f"Paused: {job.kind} {job.job_id} held (no paid submission).")
            # leave it queued; it will be picked up after /resume
            self.db.set_state(job.job_id, S_QUEUED, next_attempt_at=time.time() + 30)
            return False
        return True

    def _handle_provider_error(self, job: Job, err: WaveSpeedError, *, stage: str) -> None:
        safe = redact(str(err), self.cfg.secrets)
        self.db.save_provider(job.job_id, sanitized_error=safe[:300])
        if isinstance(err, InsufficientCredits):
            self.db.set_state(job.job_id, S_FAILED)
            self._notice("tech_log", "WaveSpeed: insufficient credits.")
            return
        if isinstance(err, ContentRejected):
            self.db.set_state(job.job_id, S_FAILED)
            self._notice("tech_log", f"Provider rejected content for {job.job_id}.")
            return
        if isinstance(err, RateLimited):
            # Honor Retry-After by requeuing, not by a blind re-POST.
            back = time.time() + getattr(err, "retry_after", 10)
            target = S_POLLING if stage == "poll" else S_QUEUED
            self.db.set_state(job.job_id, target, next_attempt_at=back, bump_attempt=True)
            return
        if stage == "submit":
            # A POST that failed without a known id: uncertain, never auto-resubmit.
            self.db.set_state(job.job_id, S_UNCERTAIN)
            self._notice("tech_log", f"{job.job_id}: submission outcome uncertain ({safe}).")
            return
        if stage == "poll" and not err.retryable:
            self.db.set_state(job.job_id, S_UNCERTAIN)
            self._notice("tech_log", f"{job.job_id}: result uncertain ({safe}).")
            return
        # retryable: bump attempt and requeue for polling
        if job.attempts < 8:
            self.db.set_state(job.job_id, S_POLLING, next_attempt_at=time.time() + 15, bump_attempt=True)
        else:
            self.db.set_state(job.job_id, S_UNCERTAIN)

    def _fail(self, job: Job, reason: str) -> None:
        try:
            self.db.set_state(job.job_id, S_FAILED)
            self._notice("tech_log", f"{job.job_id}: {reason}.")
        except Exception:
            pass

    # ---- misc --------------------------------------------------------------
    def _store_secret_wavespeed(self, value: str) -> None:
        """Persist a WaveSpeed key to the protected secrets dir (not Git, not
        logs). The running client is not swapped mid-process here; a restart
        picks up the new key."""
        SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        env_path = SECRETS_DIR / ".env"
        lines = []
        if env_path.exists():
            lines = [ln for ln in env_path.read_text().splitlines()
                     if not ln.startswith("WAVESPEED_API_KEY=")]
        lines.append(f"WAVESPEED_API_KEY={value}")
        env_path.write_text("\n".join(lines) + "\n")
        try:
            env_path.chmod(0o600)
        except OSError:
            pass

    def _b(self, text: str, action: str, job_id: str, rev: int | None = None) -> Button:
        data = f"{action}:{job_id}:{rev if rev is not None else ''}"
        return Button(text=text, data=data[:64])


def _first_file_id(receipt: dict[str, Any]) -> str | None:
    for key in ("document", "video"):
        if receipt.get(key):
            return receipt[key].get("file_id")
    if receipt.get("photo"):
        return receipt["photo"][-1]["file_id"]
    return None


def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj)


def _json_loads(text: str) -> Any:
    import json
    try:
        return json.loads(text)
    except Exception:
        return {}
