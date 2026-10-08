from __future__ import annotations

import base64
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from xgc2_xrpc import Client, Fault, Host, Limits, Runtime, TransportError

from xgc2_stt.config import Settings
from xgc2_stt.model_worker import ModelDomain


class Model:
    def init_streaming_state(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(text="partial", language="Chinese")

    def streaming_transcribe(self, _samples: Any, _state: Any) -> None:
        pass

    def finish_streaming_transcribe(self, state: Any) -> None:
        state.text = "final"


def test_model_rejects_overlapping_stale_and_oversized_sessions(tmp_path: Path) -> None:
    domain = ModelDomain(Settings(model_session_max_seconds=10), Model())
    with (
        Runtime(blocking_workers=1) as runtime,
        Host(str(tmp_path / "model.sock"), domain.routes(), runtime=runtime, instance_id="one"),
        Client(str(tmp_path / "model.sock"), runtime=runtime, instance_id="one") as client,
    ):
        session = client.json("/v1/session/start", {})["session_id"]
        with pytest.raises(Fault) as error:
            client.json("/v1/session/start", {})
        assert error.value.code == "resource_exhausted"
        for payload, code in (
            ({"session_id": "stale", "pcm16": "AA=="}, "conflict"),
            ({"session_id": session, "pcm16": "!"}, "invalid_argument"),
            ({"session_id": session, "pcm16": "A" * 220_000}, "resource_exhausted"),
        ):
            with pytest.raises(Fault) as error:
                client.json("/v1/session/chunk", payload)
            assert error.value.code == code
        chunk = {"session_id": session, "pcm16": base64.b64encode(b"\0" * 160_000).decode()}
        client.json("/v1/session/chunk", chunk)
        client.json("/v1/session/chunk", chunk)
        with pytest.raises(Fault) as error:
            client.json("/v1/session/chunk", chunk)
        assert error.value.code == "resource_exhausted"
        assert client.json("/v1/session/finish", {"session_id": session})["text"] == "final"
        assert domain.session is None


def test_expired_session_cannot_control_replacement() -> None:
    domain = ModelDomain(Settings(model_session_idle_seconds=5), Model())
    first = domain.start(None, {})["session_id"]
    domain.last_seen -= 6
    second = domain.start(None, {})["session_id"]
    assert first != second
    with pytest.raises(Fault, match="stale"):
        domain.abort(None, {"session_id": first})
    assert domain.session_id == second


def test_cancelled_inference_retains_session_and_worker_slot(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    class BlockingModel(Model):
        def streaming_transcribe(self, _samples: Any, _state: Any) -> None:
            entered.set()
            release.wait(5)

    domain = ModelDomain(Settings(), BlockingModel())
    runtime = Runtime(blocking_workers=1)
    host = Host(
        str(tmp_path / "model.sock"),
        domain.routes(),
        runtime=runtime,
        instance_id="one",
        limits=Limits(shutdown_timeout=0.05),
    )
    host.start()
    client = Client(str(tmp_path / "model.sock"), runtime=runtime, instance_id="one")
    try:
        session = client.json("/v1/session/start", {})["session_id"]
        with pytest.raises(TransportError):
            client.json("/v1/session/chunk", {"session_id": session, "pcm16": "AAA="}, timeout=0.05)
        assert entered.wait(1)
        with pytest.raises(Fault) as error:
            client.json("/v1/session/abort", {"session_id": session})
        assert error.value.code == "resource_exhausted"
        with pytest.raises(RuntimeError, match="quiesced"):
            host.close()
        assert host._lease is not None
        assert domain.session_id == session
    finally:
        release.set()
        client.close()
        host.close()
        runtime.close()
