"""Check the deployed retrieval function, not just its static interface."""
import argparse
import json
from urllib.request import Request, urlopen


def check(base_url: str) -> None:
    def search(claim: str = "") -> dict:
        payload = {"query": "Cedar sensor", "top_k": 3, "rarity": False, "claim": claim}
        request = Request(base_url.rstrip("/") + "/api/search",
                          data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=20) as response:
            if response.status != 200 or "application/json" not in response.headers.get("Content-Type", ""):
                raise RuntimeError("The retrieval endpoint did not return JSON success")
            return json.load(response)

    result = search()
    if not result.get("results"):
        raise RuntimeError("The sample query returned no sources")
    rank = next(row["citation"] for row in result["results"] if row["id"] == "DEMO-1")
    wrong = search(f"The Cedar sensor emits 999 samples per second [{rank}].")
    if wrong.get("grounding", {}).get("unsupported") != 1:
        raise RuntimeError("The numeric contradiction was not rejected")
    print("PASS: deployed retrieval returned sources and rejected an altered numeric claim")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    check(parser.parse_args().base_url)
