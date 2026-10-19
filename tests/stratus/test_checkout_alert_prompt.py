"""A prior diagnosis must not disclose the checkout fault to mitigation."""

import asyncio
from types import SimpleNamespace

from clients.stratus.stratus_agent.driver import driver


def test_checkout_mitigation_prompt_contains_only_assigned_alert(monkeypatch):
    monkeypatch.setattr(driver.AlertOracle, "wait_for_firing", lambda self: None)
    monkeypatch.setattr(driver, "get_app_info", lambda: {
        "app_name": "OpenTelemetry Demo Astronomy Shop",
        "descriptions": "An ecommerce application",
        "namespace": "astronomy-shop",
        "target_alert": {"alertname": "HighRequestErrorRate", "method": "POST", "name": "/api/checkout"},
    })
    original_load = driver.yaml.safe_load

    def load_config(text):
        config = original_load(text)
        if isinstance(config, dict) and "retry_mode" in config:
            config["retry_mode"] = "none"
        return config

    monkeypatch.setattr(driver.yaml, "safe_load", load_config)
    captured = {}

    async def single_run(messages):
        captured["messages"] = messages
        agent = SimpleNamespace(callback=SimpleNamespace(usage_metadata={
            "model": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        }))
        return agent, SimpleNamespace(values={"num_steps": 0}), []

    monkeypatch.setattr(driver, "mitigation_agent_single_run", single_run)
    asyncio.run(driver.mitigation_task_main("ROOT CAUSE: PRODUCT_CATALOG_ADDR is product-catalog:8082"))

    prompt = "\n".join(message.content for message in captured["messages"])
    assert "HighRequestErrorRate" in prompt
    assert "POST /api/checkout" in prompt
    assert "ROOT CAUSE" not in prompt
    assert "PRODUCT_CATALOG_ADDR" not in prompt
    assert "8082" not in prompt
    assert "fix all the faults" not in prompt.lower()
