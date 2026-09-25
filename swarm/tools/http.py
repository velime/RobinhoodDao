"""Shared async HTTP client."""

from __future__ import annotations

import httpx

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={"User-Agent": "swarm-research-bot/0.1"},
            follow_redirects=True,
        )
    return _client


async def get_json(url: str, params: dict | None = None, headers: dict | None = None):
    r = await client().get(url, params=params, headers=headers)
    r.raise_for_status()
    return r.json()


async def post_json(url: str, json: dict, headers: dict | None = None):
    r = await client().post(url, json=json, headers=headers)
    r.raise_for_status()
    return r.json()


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
