"use strict";
const $ = (id) => document.getElementById(id);
let revision = 0;
let controller;
function clear(message) {
  $("status").textContent = message;
  $("cards").replaceChildren();
  $("grounding").replaceChildren();
  $("explanation").textContent = "Run the current inputs to see fresh retrieval and claim checks.";
}
$("query-form").addEventListener("input", () => {
  revision++;
  controller?.abort();
  clear("Inputs changed. Retrieve again.");
  $("run").disabled = false;
  $("results").setAttribute("aria-busy", "false");
});
$("query-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const current = ++revision;
  controller?.abort();
  const pending = new AbortController();
  controller = pending;
  const timer = setTimeout(() => pending.abort(), 12000);
  clear("Retrieving and checking...");
  $("run").disabled = true;
  $("results").setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/api/search", { method: "POST", headers: { "Content-Type": "application/json" }, signal: pending.signal, body: JSON.stringify({ query: $("query").value, top_k: Number($("top-k").value), rarity: $("rarity").value === "true", claim: $("claim").value }) });
    const result = await response.json();
    if (current !== revision) return;
    if (!response.ok) throw new Error(result.error || "No result is available.");
    $("status").textContent = result.results.length ? `${result.results.length} sources retrieved` : "No lexical evidence in this corpus";
    $("explanation").textContent = result.scorer;
    for (const row of result.results) {
      const card = document.createElement("article");
      card.className = "card";
      const title = document.createElement("h3");
      title.textContent = `[${row.citation}] ${row.title}`;
      const text = document.createElement("p");
      text.textContent = row.text;
      const meta = document.createElement("p");
      meta.className = "meta";
      meta.textContent = `${row.id} | score ${row.score.toFixed(4)} | rarity multiplier ${row.rarity_multiplier} | source hash ${row.content_hash}`;
      card.append(title, text, meta);
      $("cards").append(card);
    }
    for (const row of result.grounding.claims) {
      const verdict = document.createElement("div");
      verdict.className = `claim-check ${row.verdict === "grounded" ? "" : "bad"}`;
      verdict.textContent = `${row.verdict.toUpperCase()}: ${row.text} (word overlap ${row.best_overlap}, numeric conflict ${row.numeric_conflict})`;
      $("grounding").append(verdict);
    }
    if ($("claim").value && !result.grounding.claims.length) $("grounding").textContent = "No checkable claim found. Use a sentence with at least three content words and a citation such as [1].";
  } catch (error) {
    if (current === revision) clear(error.name === "AbortError" ? "Request timed out. No result is available." : error.message);
  } finally {
    clearTimeout(timer);
    if (current === revision) {
      $("run").disabled = false;
      $("results").setAttribute("aria-busy", "false");
    }
  }
});
