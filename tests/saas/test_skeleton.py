"""The skeleton boots and stays isolated from the owner's growth-loop database."""
from __future__ import annotations


def test_app_boots(client):
    r = client.get("/_openapi.json")
    assert r.status_code == 200


def test_saas_db_is_not_the_growth_db():
    from saas.config import settings

    assert "doomscroller.db" not in settings.database_url


def test_metering_is_a_noop_without_a_meter():
    from app.clipper import metering

    metering.record("llm", provider="x", model="y", input_tokens=10)  # must not raise
    with metering.metered() as m:
        metering.record("stt", provider="deepgram", model="nova-3", audio_seconds=60)
        metering.record("llm", provider="anthropic", model="h", input_tokens=100, output_tokens=20)
        with metering.timed("render", stage="clip") as f:
            f["output_seconds"] = 30
    s = m.summary()
    assert s["stt_seconds"] == {"deepgram:nova-3": 60.0}
    assert s["llm"]["anthropic:h"]["calls"] == 1
    assert s["render"]["output_seconds"] == 30
    assert metering.active() is None
