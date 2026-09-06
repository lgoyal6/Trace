"""The failure shapes every body-bearing route declares, in one place.

A request body that is not decodable never reaches pydantic. Starlette rejects it
first and answers `400 {"detail": "There was an error parsing the body"}`, which is a
different status and a different body from the 422 that a well-formed but invalid
body produces. Every POST route in this service could return that 400 and none of them
declared it, so a client generated from `frontend/.openapi.json` had no branch for a
status the service really sends.

Bytes that decode as UTF-8 but are not JSON still land on 422 - `b"\\xff\\xfe"` does -
which is why the hand-written malformed bodies in backend/tests/test_api_contract.py
never saw this: they are all Python strings, so they are all valid UTF-8 by
construction. Schemathesis generates the byte string, not the str.
"""

from pydantic import BaseModel


class ParseErrorResponse(BaseModel):
    """Starlette's own shape for an undecodable body, written down."""

    detail: str


# `content` is spelled out rather than given as `model`. FastAPI applies the route's
# response class media type to every `responses` entry that does not name one, so on
# POST /query/stream a `model` here was documented as `text/event-stream` - the one
# route where this matters, and the one where the 400 really is a JSON object.
UNPARSEABLE_BODY = {
    400: {
        "content": {
            "application/json": {
                "schema": ParseErrorResponse.model_json_schema()
            }
        },
        "description": "the request body could not be decoded",
    }
}
