"""Create separate runtime files without printing secrets. Run with the backend's uv environment."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.core.config import Settings

AGENT_FIELDS = {
    "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_AGENT_TOKEN",
    "PAYPAL_WEBHOOK_ID", "PAYPAL_HTTP_TIMEOUT_SECONDS", "PAYPAL_BRAND_NAME",
    "LOG_LEVEL", "LOG_FORMAT",
}


def runtime_values(source: dict[str, str], api_url: str) -> tuple[dict, dict, dict]:
    parsed = urlsplit(api_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("Use the public HTTPS API origin, for example https://api.example.com")
    required = {
        "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "DEVICE_TOKEN_PEPPER",
        "INTERNAL_SERVICE_TOKEN", "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_AGENT_TOKEN",
    }
    missing = sorted(k for k in required if not source.get(k))
    public_key = source.get("SUPABASE_PUBLISHABLE_KEY") or source.get("SUPABASE_ANON_KEY")
    if not public_key:
        missing.append("SUPABASE_PUBLISHABLE_KEY")
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))
    if public_key.startswith("sb_secret_") or public_key == source["SUPABASE_SERVICE_ROLE_KEY"]:
        raise ValueError("Use a public Supabase publishable/anon key for the frontend")
    if public_key.count(".") == 2:
        try:
            encoded = public_key.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if claims.get("role") == "service_role":
                raise ValueError("Use an anon JWT, never a service-role JWT, for the frontend")
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("Invalid public Supabase JWT") from None
    allowed = {name.upper() for name in Settings.model_fields} | {"SUPABASE_ANON_KEY"}
    api = {k: v for k, v in source.items() if k in allowed and k not in {"PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"}}
    api.update(PUBLIC_BASE_URL=api_url.rstrip("/"), APP_ENV="production", PAYPAL_ENV="sandbox", MCP_ENABLED="true")
    agent = {k: v for k, v in source.items() if k in AGENT_FIELDS}
    agent.update(APP_ENV="production", PAYPAL_ENV="sandbox")
    frontend = {
        "CLAWPAY_API_ORIGIN": api_url.rstrip("/"),
        "SUPABASE_URL": source["SUPABASE_URL"],
        "SUPABASE_PUBLISHABLE_KEY": public_key,
    }
    return api, agent, frontend


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def dotenv_text(values: dict[str, str]) -> str:
    if any(any(c in value for c in "\n\r\0") for value in values.values()):
        raise ValueError("Runtime env values must each fit on one line")
    # Compose >=2.30 reads these files in raw mode: no quoting or second interpolation.
    return "".join(f"{key}={value}\n" for key, value in sorted(values.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    args = parser.parse_args()
    source = {k.upper(): v for k, v in dotenv_values(ROOT / "backend/.env").items() if v}
    try:
        api, agent, frontend = runtime_values(source, args.api_url)
        # Validate before creating any files. Compose supplies the loopback agent endpoint.
        Settings(_env_file=None, **{k.lower(): v for k, v in api.items()})
        Settings(_env_file=None, **{k.lower(): v for k, v in agent.items()})
        files = {"api.env": dotenv_text(api), "paypal.env": dotenv_text(agent), "frontend.json": json.dumps(frontend, indent=2) + "\n"}
        for name, content in files.items():
            write_private(ROOT / "deploy/.secrets" / name, content)
        print("Prepared deploy/.secrets/{api.env,paypal.env,frontend.json} with private permissions. No values printed.")
    except (ValueError, OSError) as exc:
        # Settings errors hide input values, but omit their body too as a precaution.
        if type(exc).__name__ == "ValidationError":
            parser.error("Runtime settings failed validation. Check backend/.env.")
        parser.error(str(exc))


if __name__ == "__main__":
    main()
