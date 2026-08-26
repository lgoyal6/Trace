"""Reassigned to Card 1 at contracts-v1.

NOTE: this test builds a standalone FastAPI app mounting only
backend.api.routes.conditions.router, instead of importing
backend.api.main:app, because backend/api/dependencies.py (Card 2A's file)
still imports the deleted backend.app.llm_client and
backend.app.retrieval.hybrid at module scope, which fails to import
entirely under NEULIT_PROFILE=fake right now. That is outside Card 1's
ownership (see Blockers.md) - mounting the router directly keeps this test
green without touching a Card 2A file, and still exercises the exact route
handler /conditions serves through the real app.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.conditions import router as conditions_router

app = FastAPI()
app.include_router(conditions_router)
client = TestClient(app)


def test_conditions_endpoint_returns_all_fourteen_conditions():
    response = client.get("/conditions")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 14
    scalp = next(c for c in body if c["name"] == "Scalp angiosarcoma")
    assert scalp["rarity"] == "rare"
    assert scalp["atlas_label"] == "Precentral Gyrus, Postcentral Gyrus"
