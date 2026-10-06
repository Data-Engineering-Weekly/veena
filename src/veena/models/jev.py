"""Jev (TypeSafe System One) classifier adapter.

Jev's public API was not documented when this was written. The request and response shapes
below are an assumed contract behind an injectable transport, so the adapter can be wired and
tested now and corrected in one place when access arrives.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from veena.errors import ModelError, ModelNotConfiguredError
from veena.models.protocol import Classification

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

type Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]
_MAX_BODY = 1 << 20


def _http_transport(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={**headers, "content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(_MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        snippet = exc.read(200).decode(errors="replace") if hasattr(exc, "read") else ""
        msg = f"jev returned HTTP {exc.code}: {snippet[:200]}"
        raise ModelError(msg) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        msg = f"jev is unreachable: {exc}"
        raise ModelError(msg) from exc
    if len(raw) > _MAX_BODY:
        msg = "jev response exceeded 1 MiB"
        raise ModelError(msg)
    try:
        payload: dict[str, Any] = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        msg = f"jev response is not JSON: {raw[:200]!r}"
        raise ModelError(msg) from exc
    return payload


class JevClassifier:
    """Posts a closed-set decision to Jev and reads back a value with its probability.

    Assumed contract: `POST {base_url}/v1/decide` with `{"options": [...], "context": {...}}`
    returns `{"value": "...", "probability": 0.0..1.0, "alternatives": {"...": 0.0}}`.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: Transport | None = None,
        model_id: str = "jev",
    ) -> None:
        """Read the endpoint and key from arguments or `JEV_BASE_URL` and `JEV_API_KEY`."""
        self._base_url = (base_url or os.environ.get("JEV_BASE_URL", "")).rstrip("/")
        self._api_key = api_key or os.environ.get("JEV_API_KEY", "")
        self._transport = transport or _http_transport
        self.model_id = model_id

    def classify[T: StrEnum](self, context: BaseModel, options: type[T]) -> Classification[T]:
        """Return Jev's choice and probability.

        Raises:
            ModelNotConfiguredError: No base URL or key is configured, or the URL is not https.
            ModelError: The call failed, or the response is missing fields or names an option
                outside the set. Error text never includes the bearer token.
        """
        if not self._base_url or not self._api_key:
            msg = "set JEV_BASE_URL and JEV_API_KEY to use the Jev adapter"
            raise ModelNotConfiguredError(msg)
        if not self._base_url.startswith("https://"):
            msg = "JEV_BASE_URL must use https"
            raise ModelNotConfiguredError(msg)
        names = [o.value for o in options]
        body = {"options": names, "context": context.model_dump(mode="json")}
        payload = self._transport(
            f"{self._base_url}/v1/decide", body, {"authorization": f"Bearer {self._api_key}"}
        )
        try:
            value = str(payload["value"])
            probability = float(payload["probability"])
        except (KeyError, TypeError, ValueError) as exc:
            keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
            msg = f"jev response lacks value or probability; keys: {keys}"
            raise ModelError(msg) from exc
        if value not in names:
            msg = f"jev returned {value!r}, not one of {names}"
            raise ModelError(msg)
        if not 0.0 <= probability <= 1.0:
            msg = f"jev returned probability {probability}, outside [0, 1]"
            raise ModelError(msg)
        raw_alternatives = payload.get("alternatives") or {}
        alternatives = tuple(
            (options(k), float(v))
            for k, v in dict(raw_alternatives).items()
            if k in names and k != value
        )
        return Classification(
            value=options(value),
            probability=probability,
            alternatives=alternatives,
            model_id=self.model_id,
        )
