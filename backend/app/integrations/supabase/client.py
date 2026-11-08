"""Supabase service-role client factory.

SECURITY: the service-role key bypasses Row Level Security. This client is for
trusted server-side code only. Never return it, its key, or its URL to clients,
the dashboard, or ESP32 firmware.
"""

from __future__ import annotations

import httpx
from postgrest import SyncPostgrestClient
from supabase import Client, ClientOptions, create_client

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, ExternalServiceError


def create_service_client(settings: Settings) -> Client:
    if not settings.supabase_configured:
        raise ConfigurationError("Supabase is not configured")
    assert settings.supabase_url is not None
    assert settings.supabase_service_role_key is not None
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key.get_secret_value(),
        options=ClientOptions(
            postgrest_client_timeout=settings.supabase_timeout_seconds,
            auto_refresh_token=False,
            persist_session=False,
        ),
    )


def create_user_postgrest(
    settings: Settings, jwt: str, http_client: httpx.Client
) -> SyncPostgrestClient:
    """PostgREST client acting as the end user (RLS and column grants apply).

    Uses the publishable key, never the service-role key. ``headers=`` replaces
    the library defaults, so the full set is passed. No deprecated ``timeout=``:
    the timeout lives on ``http_client``, which the caller must close.
    """
    if not settings.supabase_url or settings.supabase_publishable_key is None:
        raise ConfigurationError("Dashboard reads are not configured")
    publishable = settings.supabase_publishable_key.get_secret_value()
    return SyncPostgrestClient(
        f"{settings.supabase_url}/rest/v1",
        schema="public",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "apikey": publishable,
            "Authorization": f"Bearer {jwt}",
        },
        http_client=http_client,
    )


def check_supabase(client: Client) -> None:
    """Lightweight connectivity + schema check (requires migrations applied)."""
    try:
        client.table("merchants").select("id").limit(1).execute()
    except Exception as exc:  # any failure means "not ready"
        raise ExternalServiceError("Supabase readiness check failed") from exc
