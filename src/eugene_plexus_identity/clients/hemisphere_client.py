"""Hemisphere-driver client for the reflection flow.

Identity uses ONE hemisphere-driver for reflection — not the full
bicameral loop. Reflection isn't a chat turn; it's a "think alone
about who I am" process. Single-hemisphere keeps the wire shape
simple and lets the operator point reflection at a different
backend than chat if they want (e.g. a slower-but-cheaper model
for the reflection scheduler).

We use raw dicts for the outbound payload rather than dragging in
hemisphere-driver's generated models — keeps identity's codegen
surface focused on identity.yaml + common.yaml.
"""

from __future__ import annotations

import httpx


class HemisphereError(Exception):
    """Raised when the hemisphere-driver returns a non-2xx response.

    `status_code` is the upstream HTTP status; `detail` is a human-
    readable explanation. The reflection route maps this to 503.
    """

    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class HemisphereClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 180.0,
        service_token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers = (
            {"Authorization": f"Bearer {service_token}"} if service_token else None
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            headers=headers,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def generate(
        self,
        *,
        system_prompt: str,
        user_message: str,
        temperature: float | None = 0.5,
        max_tokens: int | None = 2048,
    ) -> str:
        """Single-shot generation. Returns the response text.

        Raises `HemisphereError` on non-2xx; reflection maps to 503.
        """
        payload: dict[str, object] = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "passIndex": 0,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["maxTokens"] = max_tokens

        response = await self._client.post("/v1/generate", json=payload)
        if response.status_code >= 400:
            raise HemisphereError(
                status_code=response.status_code,
                detail=(
                    f"hemisphere-driver at {self._base_url} returned "
                    f"{response.status_code}: {response.text[:500]}"
                ),
            )
        body = response.json()
        content = body.get("content")
        if not isinstance(content, str):
            raise HemisphereError(
                status_code=502,
                detail=(
                    f"hemisphere-driver response missing `content` string: "
                    f"{body!r}"
                ),
            )
        return content

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["HemisphereClient", "HemisphereError"]
