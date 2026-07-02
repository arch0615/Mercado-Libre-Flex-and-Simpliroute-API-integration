from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_scan_page_renders(client):
    resp = client.get("/scan")
    assert resp.status_code == 200
    html = resp.text
    # The page targets the protected import endpoint and loads a QR decoder.
    assert "/internal/scan" in html
    assert "html5-qrcode" in html
    assert "Escanear etiqueta" in html
