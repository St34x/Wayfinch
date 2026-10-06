# SPDX-License-Identifier: Apache-2.0
"""Test harness: minimal `glasses` adapter -- aiohttp WebSocket on 127.0.0.1, bearer token.

WS protocol (JSON text frames), client -> adapter:
  {"type":"msg", "lane":"alpha", "text":"..."}               -> MessageEvent on chat_id "glasses:alpha"
  {"type":"voice", "lane":"alpha", "b64":"...", "ext":".wav"} -> VOICE MessageEvent (voice event)
  A frame without "lane" uses extra.default_lane (setup_home.sh: first TEST_PROFILES entry); with neither,
  the adapter answers {"type":"error", "error":"lane required"} and drops the frame.
  {"type":"approval", "session_key":"...", "choice":"once"}   -> tools.approval.resolve_gateway_approval
  {"type":"clarify", "clarify_id":"...", "response":"..."}    -> tools.clarify_gateway.resolve_gateway_clarify
  {"type":"slash_confirm", "confirm_id":"...", "choice":"once|always|cancel"}
adapter -> client: {"type":"out"|"approval"|"clarify"|"slash_confirm"|"ack"|"error", ...}

Every inbound/outbound event is also appended to extra.event_log (JSONL) with the HERMES_HOME
that was active when the adapter method ran -- that is the evidence the harness tests use.
Not production code: no outbox persistence, no per-device tokens, no rate limits.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import Any

from aiohttp import WSMsgType, web
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult, cache_audio_from_bytes
from gateway.platforms.event import MessageEvent, MessageType

logger = logging.getLogger(__name__)


def _active_home() -> str:
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home())
    except Exception as exc:  # pragma: no cover - evidence only
        return f"<err {exc}>"


class GlassesAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig, **kwargs):
        super().__init__(config, Platform("glasses"))
        extra = getattr(config, "extra", None) or {}
        self._host = "127.0.0.1"  # loopback only, never configurable in the test harness
        self._port = int(extra.get("port") or 18700)
        token_file = extra.get("token_file")
        self._token = open(token_file).read().strip() if token_file else ""
        self._log_path = extra.get("event_log")
        self._default_lane = str(extra.get("default_lane") or "")
        self._user_id = str(extra.get("user_id") or "operator")
        # Long-answer hand-off: answers > long_threshold chars go to the OWNING profile's Telegram home channel.
        self._long_threshold = int(extra.get("long_threshold") or 0)
        self._clients: set = set()
        self._outbox: list = []
        self._runner: web.AppRunner | None = None

    # -- evidence ---------------------------------------------------------
    def _log(self, **kw) -> None:
        kw.setdefault("ts", time.strftime("%H:%M:%S"))
        kw.setdefault("t", round(time.time(), 3))
        kw.setdefault("home", _active_home())
        kw.setdefault("owner_profile", getattr(self, "_owner_profile", None))
        logger.info("[glasses-harness] %s", json.dumps(kw, ensure_ascii=False)[:400])
        if self._log_path:
            with open(self._log_path, "a") as fh:
                fh.write(json.dumps(kw, ensure_ascii=False) + "\n")

    async def _push(self, obj: dict) -> bool:
        data = json.dumps(obj, ensure_ascii=False)
        if not self._clients:
            self._outbox.append(data)
            return False
        for ws in list(self._clients):
            try:
                await ws.send_str(data)
            except Exception:
                self._clients.discard(ws)
        return True

    # -- lifecycle --------------------------------------------------------
    async def connect(self, *, is_reconnect: bool = False, **_kw) -> bool:
        app = web.Application()
        app.router.add_get("/ws", self._ws_handler)
        app.router.add_get("/health", self._health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await site.start()
        except OSError as exc:
            self._set_fatal_error("bind_failed", str(exc), retryable=False)
            self._log(event="bind_failed", error=str(exc))
            return False
        self._mark_connected()
        self._log(event="connected", host=self._host, port=self._port)
        return True

    async def disconnect(self) -> None:
        for ws in list(self._clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()
        self._mark_disconnected()

    # -- inbound ----------------------------------------------------------
    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _ws_handler(self, request: web.Request):
        if not self._token or request.headers.get("Authorization") != f"Bearer {self._token}":
            self._log(event="ws_reject", remote=request.remote)
            return web.Response(status=401, text="unauthorized")
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        self._log(event="ws_connect", remote=request.remote)
        while self._outbox:
            await ws.send_str(self._outbox.pop(0))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    await self._on_frame(ws, json.loads(msg.data))
                except Exception as exc:
                    logger.exception("glasses frame failed")
                    await ws.send_str(json.dumps({"type": "error", "error": str(exc)}))
        finally:
            self._clients.discard(ws)
        return ws

    async def _on_frame(self, ws, frame: dict) -> None:
        kind = frame.get("type")
        if kind in ("msg", "voice"):
            lane = str(frame.get("lane") or self._default_lane)
            if not lane:
                await ws.send_str(json.dumps({"type": "error", "error": "lane required"}))
                return
            chat_id = f"glasses:{lane}"
            mid = frame.get("id") or uuid.uuid4().hex[:12]
            source = self.build_source(
                chat_id=chat_id,
                chat_name=chat_id,
                chat_type="dm",
                user_id=self._user_id,
                user_name=self._user_id,
                message_id=mid,
            )
            if kind == "msg":
                event = MessageEvent(
                    text=str(frame.get("text") or ""), message_type=MessageType.TEXT, source=source, message_id=mid
                )
            else:
                path = cache_audio_from_bytes(base64.b64decode(frame["b64"]), ext=frame.get("ext") or ".wav")
                mime = frame.get("mime") or "audio/wav"
                event = MessageEvent(
                    text="",
                    message_type=MessageType.VOICE,
                    source=source,
                    message_id=mid,
                    media_urls=[path],
                    media_types=[mime],
                )
                event.metadata["glasses_t0"] = time.time()
            self._log(
                event="inbound",
                kind=kind,
                chat_id=chat_id,
                mid=mid,
                routed_profile=getattr(source, "profile", None),
                route_rejected=getattr(source, "profile_route_rejected", None),
                text=(event.text or "")[:120],
            )
            await ws.send_str(
                json.dumps({
                    "type": "ack",
                    "id": mid,
                    "chat_id": chat_id,
                    "routed_profile": getattr(source, "profile", None),
                })
            )
            await self.handle_message(event)
        elif kind == "approval":
            from tools.approval import resolve_gateway_approval

            n = resolve_gateway_approval(frame["session_key"], frame["choice"])
            self._log(event="approval_resolve", session_key=frame["session_key"], choice=frame["choice"], resolved=n)
            await ws.send_str(json.dumps({"type": "ack", "approval_resolved": n}))
        elif kind == "clarify":
            from tools.clarify_gateway import resolve_gateway_clarify

            ok = resolve_gateway_clarify(frame["clarify_id"], frame["response"])
            self._log(event="clarify_resolve", clarify_id=frame["clarify_id"], ok=ok)
            await ws.send_str(json.dumps({"type": "ack", "clarify_resolved": ok}))
        elif kind == "slash_confirm":
            # NB: base.py docstring names GatewayRunner._resolve_slash_confirm, which does not exist at
            # a4648c5f58; the real resolver is tools.slash_confirm.resolve(session_key, confirm_id, choice).
            from tools import slash_confirm as _sc

            res = await _sc.resolve(frame["session_key"], frame["confirm_id"], frame["choice"])
            self._log(event="slash_confirm_resolve", confirm_id=frame["confirm_id"], result=str(res)[:200])
            await ws.send_str(json.dumps({"type": "ack", "slash_confirm": str(res)[:200]}))

    # -- long-answer hand-off to the owning profile's Telegram ---------------
    _SYSTEM_PREFIXES = ("\u26a1", "\u23f3", "\u23e9", "\U0001f4be", "\U0001f399", "\U0001f4ec")

    def _owning_profile(self, chat_id: str) -> str:
        """Same route table as inbound (gateway.profile_routes via build_source); NOT get_hermes_home()."""
        src = self.build_source(chat_id=chat_id, chat_type="dm", user_id=self._user_id)
        return getattr(src, "profile", None) or "default"

    async def _long_handoff(self, chat_id: str, content: str) -> dict:
        t0 = time.time()
        profile = self._owning_profile(chat_id)
        runner = self.gateway_runner
        tg_platform = Platform("telegram")
        tg = None
        if runner is not None:
            tg = runner._adapters_for_profile(None if profile == "default" else profile).get(tg_platform)
        info: dict[str, Any] = {"chat_id": chat_id, "profile": profile, "tg_adapter": tg is not None}
        if tg is not None:
            try:
                info["tg_owner"] = str(runner._owning_profile(tg, tg_platform))
            except Exception as exc:
                info["tg_owner"] = f"<err {exc}>"
            info["tg_bot_id"] = str(getattr(getattr(tg, "config", None), "token", "") or "").split(":", 1)[0]
        home = getattr(getattr(tg, "config", None), "home_channel", None) if tg is not None else None
        info["home"] = getattr(home, "chat_id", None)
        if tg is None:
            info["status"] = "no_telegram"
        elif home is None:
            info["status"] = "no_home_channel"
        else:
            try:
                res = await tg.send(home.chat_id, content)
                info["status"] = "delivered" if res.success else "failed"
                info["error"] = (res.error or "")[:200] if not res.success else None
            except Exception as exc:  # adapter raised instead of returning SendResult
                info["status"], info["error"] = "raised", f"{type(exc).__name__}: {str(exc)[:200]}"
        info["elapsed"] = round(time.time() - t0, 2)
        self._log(event="long_handoff", **info)
        return info

    # -- outbound ---------------------------------------------------------
    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: dict[str, Any] | None = None
    ) -> SendResult:
        if (
            self._long_threshold
            and len(content or "") > self._long_threshold
            and not (content or "").startswith(self._SYSTEM_PREFIXES)
        ):
            info = await self._long_handoff(chat_id, content)
            first = (content or "").split(". ")[0][:80]
            pointer = "%s\u2026 \u2192 long answer \u2192 Telegram (%s): %s" % (first, info["profile"], info["status"])
            mid = uuid.uuid4().hex[:12]
            self._log(event="send", chat_id=chat_id, reply_to=reply_to, text=pointer, long=True)
            await self._push({
                "type": "out",
                "id": mid,
                "chat_id": chat_id,
                "text": pointer,
                "reply_to": reply_to,
                "longRef": {
                    "channel": "telegram",
                    "profile": info["profile"],
                    "status": info["status"],
                    "chars": len(content),
                },
            })
            return SendResult(success=True, message_id=mid)
        mid = uuid.uuid4().hex[:12]
        self._log(
            event="send",
            chat_id=chat_id,
            reply_to=reply_to,
            text=(content or "")[:300],
            metadata_keys=sorted((metadata or {}).keys()),
        )
        await self._push({"type": "out", "id": mid, "chat_id": chat_id, "text": content, "reply_to": reply_to})
        return SendResult(success=True, message_id=mid)

    async def _send_exec_approval_prompt(self, prompt) -> SendResult:
        mid = uuid.uuid4().hex[:12]
        self._log(
            event="approval_prompt",
            chat_id=prompt.chat_id,
            session_key=prompt.session_key,
            command=prompt.command[:200],
            choices=prompt.choices,
            smart_denied=prompt.smart_denied,
        )
        await self._push({
            "type": "approval",
            "id": mid,
            "chat_id": prompt.chat_id,
            "session_key": prompt.session_key,
            "command": prompt.command,
            "description": prompt.description,
            "choices": prompt.choices,
        })
        return SendResult(success=True, message_id=mid)

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices,
        clarify_id: str,
        session_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        mid = uuid.uuid4().hex[:12]
        self._log(
            event="clarify_prompt",
            chat_id=chat_id,
            clarify_id=clarify_id,
            session_key=session_key,
            question=question[:200],
            choices=choices,
        )
        await self._push({
            "type": "clarify",
            "id": mid,
            "chat_id": chat_id,
            "clarify_id": clarify_id,
            "session_key": session_key,
            "question": question,
            "choices": choices,
        })
        return SendResult(success=True, message_id=mid)

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        mid = uuid.uuid4().hex[:12]
        self._log(event="slash_confirm_prompt", chat_id=chat_id, confirm_id=confirm_id, title=title)
        await self._push({
            "type": "slash_confirm",
            "id": mid,
            "chat_id": chat_id,
            "confirm_id": confirm_id,
            "title": title,
            "message": message,
            "session_key": session_key,
        })
        return SendResult(success=True, message_id=mid)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm"}
