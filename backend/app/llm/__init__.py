"""Card 1's LLM support package: pricing math, token estimation, and JSON
repair helpers consumed by backend/snowflake/llm.py's CortexLLMClient.

Replaces backend/app/llm_client.py (deleted at freeze). Contains no provider
client itself - that lives in backend/snowflake/llm.py per the FROZEN stub
filename table in plan-v2/00-SHARED-CONTRACTS.md section 2.3.
"""
from __future__ import annotations
