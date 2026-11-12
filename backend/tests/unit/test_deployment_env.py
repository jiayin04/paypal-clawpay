"""Deployment must preserve existing device credentials and separate provider keys."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "deploy/prepare_env.py"
spec = importlib.util.spec_from_file_location("clawpay_deployment_env", SCRIPT)
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


def test_runtime_files_separate_credentials_and_preserve_exact_values(tmp_path):
    source = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "server-secret",
        "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_example",
        "DEVICE_TOKEN_PEPPER": "existing-pepper-must-stay-identical",
        "INTERNAL_SERVICE_TOKEN": "admin-secret",
        "PAYPAL_AGENT_TOKEN": "shared-agent-secret",
        "PAYPAL_CLIENT_ID": "paypal-client",
        "PAYPAL_CLIENT_SECRET": 'provider$secret#with"quotes',
        "DEEPSEEK_API_KEY": "llm-secret",
        "UNRELATED_SECRET": "omit-this",
    }
    api, agent, frontend = deployment.runtime_values(source, "https://api.example.com/")
    assert api["DEVICE_TOKEN_PEPPER"] == source["DEVICE_TOKEN_PEPPER"]
    assert "PAYPAL_CLIENT_SECRET" not in api and "PAYPAL_CLIENT_ID" not in api
    assert agent["PAYPAL_CLIENT_SECRET"] == source["PAYPAL_CLIENT_SECRET"]
    assert not {"SUPABASE_SERVICE_ROLE_KEY", "DEEPSEEK_API_KEY", "INTERNAL_SERVICE_TOKEN"}.intersection(agent)
    assert set(frontend) == {"CLAWPAY_API_ORIGIN", "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY"}
    assert "UNRELATED_SECRET" not in api
    assert 'PAYPAL_CLIENT_SECRET=provider$secret#with"quotes\n' in deployment.dotenv_text(agent)
    target = tmp_path / "private" / "runtime.env"
    deployment.write_private(target, deployment.dotenv_text(agent))
    deployment.write_private(target, "replacement")
    assert target.read_text() == "replacement"
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        deployment.runtime_values(source, "http://api.example.com")
    with pytest.raises(ValueError):
        deployment.runtime_values({**source, "SUPABASE_PUBLISHABLE_KEY": "server-secret"}, "https://api.example.com")
    with pytest.raises(ValueError):
        deployment.dotenv_text({"KEY": "secret\nOTHER=injected"})
