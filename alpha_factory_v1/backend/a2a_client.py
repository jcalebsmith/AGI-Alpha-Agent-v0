
"""
a2a_client.py
-------------
Transport scaffold for Alpha‑Factory remote‑swarm.

• Supports gRPC (preferred) and WebSocket fall‑back.
• mTLS based on SPIFFE / SPIRE (https://spiffe.io) – no hard‑coded secrets.
• Zero‑dependency on the Alpha‑Factory runtime: can be vendored as‑is.

Usage
-----
>>> client = A2AClient.remote(
...     service='alpha-factory-remote.default.svc.cluster.local:443',
...     spiffe_id='spiffe://alpha-factory/agency/finance-agent'
... )
>>> await client.send(TaskRequest(...))
>>> async for event in client.stream('trace'):
...     print(event)

Environment
-----------
* SPIFFE_ENDPOINT_SOCKET – unix‑domain socket where the Workload API is exposed.
  (Typically `/run/spire/sockets/agent.sock` when running with the SPIRE agent.)
* A2A_INSECURE – set to `1` to disable mTLS (dev only).

NOTE:  Proto stubs are lazily imported, so you only need to `pip install grpcio`
       when you actually want gRPC transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, AsyncGenerator, Literal, Type

_DEFAULT_SOCKET = os.getenv("SPIFFE_ENDPOINT_SOCKET", "/run/spire/sockets/agent.sock")

# --------------------------------------------------------------------------- #
# Public dataclasses shared by all transports
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class TaskRequest:
    agent_id: str
    payload: dict[str, Any]
    priority: Literal["LOW", "NORMAL", "HIGH"] = "NORMAL"


@dataclass(slots=True)
class TaskResponse:
    task_id: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None


# --------------------------------------------------------------------------- #
# Core client façade
# --------------------------------------------------------------------------- #

class A2AClient:
    """Unified façade that hides the underlying transport (gRPC / WebSocket)."""

    def __init__(self, _impl) -> None:  # noqa: D401
        self._impl = _impl

    # Factory helpers -------------------------------------------------------

    @classmethod
    async def remote(
        cls,
        service: str,
        *,
        spiffe_id: str | None = None,
        prefer_grpc: bool = True,
        websocket_path: str = "/ws/a2a",
    ) -> "A2AClient":
        """Auto‑determine the best transport and return an initialised client."""
        if prefer_grpc:
            try:
                impl = await _GrpcTransport.new(service, spiffe_id=spiffe_id)
                return cls(impl)
            except Exception:  # pylint: disable=broad-except
                # fallback below
                pass
        impl = await _WsTransport.new(service, websocket_path, spiffe_id=spiffe_id)
        return cls(impl)

    # High‑level operations -------------------------------------------------

    async def send(self, req: TaskRequest) -> TaskResponse:
        return await self._impl.send(req)

    async def stream(self, topic: str) -> AsyncGenerator[dict[str, Any], None]:
        async for ev in self._impl.stream(topic):
            yield ev

    # Async context‑manager sugar ------------------------------------------

    async def __aenter__(self) -> "A2AClient":  # noqa: D401
        return self

    async def __aexit__(                       # noqa: D401
        self,
        exc_type: Type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._impl.close()


# --------------------------------------------------------------------------- #
# gRPC transport
# --------------------------------------------------------------------------- #

class _GrpcTransport:
    """Lightweight gRPC transport with SPIFFE mTLS."""

    def __init__(self, channel, stub_cls) -> None:  # noqa: D401
        self._channel = channel
        self._stub = stub_cls(channel)

    # Factory ...............................................................

    @classmethod
    async def new(cls, target: str, *, spiffe_id: str | None) -> "_GrpcTransport":
        import grpc
        from workloadapi import X509Source, WorkloadApiClient  # type: ignore

        insecure = os.getenv("A2A_INSECURE") == "1"
        if insecure:
            channel = grpc.aio.insecure_channel(target)
        else:
            # Fetch an identity X509 SVID from the Workload API
            source = X509Source(WorkloadApiClient(address=_DEFAULT_SOCKET))
            creds = grpc.ssl_channel_credentials(
                private_key=source.key,
                certificate_chain=source.cert_chain,
                root_certificates=source.bundle,
            )
            # Optional: enforce peer SPIFFE ID via TLSAuth
            if spiffe_id:
                auth = grpc.ssl_target_name_override(spiffe_id)
                channel = grpc.aio.secure_channel(target, creds, (("grpc.ssl_target_name_override", auth),))
            else:
                channel = grpc.aio.secure_channel(target, creds)
        # Lazy import of auto‑generated stub
        from proto.alpha_factory.v1 import alpha_pb2_grpc as stubs  # type: ignore
        return cls(channel, stubs.RouterStub)

    # Public API ............................................................

    async def send(self, req: TaskRequest) -> TaskResponse:
        from proto.alpha_factory.v1 import alpha_pb2 as pb  # type: ignore
        msg = pb.TaskRequest(**asdict(req))
        reply = await self._stub.SendTask(msg)
        return TaskResponse(
            task_id=reply.task_id,
            status=reply.status,
            result=json.loads(reply.result) if reply.result else None,
            error=reply.error or None,
        )

    async def stream(self, topic: str) -> AsyncGenerator[dict[str, Any], None]:
        from proto.alpha_factory.v1 import alpha_pb2 as pb  # type: ignore
        req = pb.EventStreamRequest(topic=topic)
        call = self._stub.EventStream(req)
        async for ev in call:
            yield json.loads(ev.payload)

    async def close(self) -> None:
        await self._channel.close()


# --------------------------------------------------------------------------- #
# WebSocket transport (fallback)
# --------------------------------------------------------------------------- #

class _WsTransport:
    """Lightweight WebSocket‑client fallback with optional wss://mTLS."""

    def __init__(self, ws) -> None:  # noqa: D401
        self._ws = ws

    # Factory ...............................................................

    @classmethod
    async def new(
        cls,
        host: str,
        path: str,
        *,
        spiffe_id: str | None,
    ) -> "_WsTransport":
        import websockets  # type: ignore

        uri = f"wss://{host}{path}"
        ssl_ctx: ssl.SSLContext | bool
        if os.getenv("A2A_INSECURE") == "1":
            uri = uri.replace("wss://", "ws://")
            ssl_ctx = False
        else:
            ssl_ctx = _spiffe_ssl_context(spiffe_id)
        ws = await websockets.connect(uri, ssl=ssl_ctx, max_size=2 ** 20)
        return cls(ws)

    # Public API ............................................................

    async def send(self, req: TaskRequest) -> TaskResponse:
        await self._ws.send(json.dumps(asdict(req)))
        raw = await self._ws.recv()
        return TaskResponse(**json.loads(raw))

    async def stream(self, topic: str) -> AsyncGenerator[dict[str, Any], None]:
        await self._ws.send(json.dumps({"action": "subscribe", "topic": topic}))
        while True:
            raw = await self._ws.recv()
            yield json.loads(raw)

    async def close(self) -> None:
        await self._ws.close()

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _spiffe_ssl_context(spiffe_id: str | None) -> ssl.SSLContext:
    """Return an `ssl.SSLContext` pre‑loaded with SPIFFE SVID + bundle."""
    from workloadapi import X509Source, WorkloadApiClient  # type: ignore

    source = X509Source(WorkloadApiClient(address=_DEFAULT_SOCKET))
    ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    ctx.load_cert_chain(certfile=source.cert_chain_path, keyfile=source.key_path)
    ctx.load_verify_locations(cafile=source.bundle_path)
    # Optional: tls‑authz based on server SPIFFE ID
    if spiffe_id:
        from pyspiffe.spiffe_id.spiffe_id import SpiffeId  # type: ignore
        from pyspiffe.bundle.x509.x509_bundle import X509BundleSet  # type: ignore
        from pyspiffe.cert.x509 import cert_validator  # type: ignore

        bundle_set = X509BundleSet.new_empty_set()
        bundle_set.add(bundle=source.x509_bundle())
        validator = cert_validator.create_default_x509_validator(bundle_set)
        ctx.verify_flags |= ssl.VERIFY_X509_TRUSTED_FIRST  # type: ignore[attr-defined]

        def _verify(conn, x509, errnum, depth, ok):  # noqa: D401
            if not ok:
                return ok
            peer_spiffe_id = SpiffeId.parse(cert_validator.extract_ids(x509)[0])
            return peer_spiffe_id == SpiffeId.parse(spiffe_id)

        ctx.verify_flags = ssl.VERIFY_PEER
        ctx.set_verify(ssl.CERT_REQUIRED, _verify)  # type: ignore[attr-defined]
    return ctx
