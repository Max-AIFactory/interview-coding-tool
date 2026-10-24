import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

SAMPLES = BASE / "samples"


@pytest.fixture
def project(tmp_path):
    """A fresh app + project in a temp folder. Yields (client, project)."""
    app = create_app(base_dir=BASE, data_dir=tmp_path / "data", backups_dir=tmp_path / "backups",
                     exports_dir=tmp_path / "exports", start_timer=False)
    with TestClient(app) as client:
        yield client, app.state.project


@pytest.fixture
def client(project):
    return project[0]


def import_md(client, path: Path, **form):
    data = {k: str(v) for k, v in form.items()}
    return client.post("/api/v1/interviews/import",
                       files={"file": (path.name, path.read_bytes(), "text/markdown")}, data=data)


@pytest.fixture
def demo(client):
    """Client with both sample transcripts imported. Returns (client, codes-by-key)."""
    for name in ("DE-00.md", "CN-00.md"):
        r = import_md(client, SAMPLES / name)
        assert r.status_code == 200, r.text
    codes = {c["code_key"]: c for c in client.get("/api/v1/codes").json()["flat"]}
    return client, codes


def locate(client, iid, text, **kw):
    r = client.post(f"/api/v1/interviews/{iid}/locate", json={"text": text, **kw})
    assert r.status_code == 200, r.text
    hits = r.json()
    assert hits, f"text not found: {text!r}"
    return hits[0]


def code_quote(client, codes, iid, text, keys, valence=0, agency=None, **extra):
    loc = locate(client, iid, text)
    body = {"paragraph_id": loc["paragraph_id"], "start_char": loc["start_char"], "end_char": loc["end_char"],
            "codes": [{"code_id": codes[k]["id"], "valence": valence, "agency": agency} for k in keys], **extra}
    r = client.post("/api/v1/segments", json=body)
    assert r.status_code == 201, r.text
    return r.json()
