"""
Toronto Zoning — Quick Answer synthesiser
==========================================

Reuses the full retrieval pipeline (Qdrant + cross-refs) from query.py
but replaces the full synthesis step with a fast, brevity-focused model.

Output goal: 3-8 bullet points, plain English, exceptions called out first,
decision-ready. No lengthy by-law quotations.
"""

from __future__ import annotations

import os
import time
from typing import Optional

try:
    from langsmith import traceable as _traceable
except ImportError:
    def _traceable(*args, **kwargs):  # no-op if langsmith not installed
        def decorator(fn): return fn
        return decorator

# Re-use every retrieval helper from the main pipeline — no duplication.
# Import `query` as a module (not individual names) for the mutable singletons
# (_openai, _qdrant) so we always read the *current* value after init_vertex()
# runs, not the None that was set at import time.
import query
from query import (
    retrieve,
    build_rich_parcel_context,
    sanitize_question,
    sanitize_output,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# gpt-4.1-mini is fast and cheap — good enough for a plain-English bullet summary.
# Override via env var if needed.
_QUICK_MODEL = os.getenv("QUICK_ANSWER_MODEL", "gpt-4.1-mini")


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────

_QUICK_SYSTEM = """You are an instant zoning lookup tool for Toronto By-law 569-2013.
A user asked a factual question about a specific parcel. Deliver a complete, direct answer
they can act on immediately. Answer as long as the question requires — no length limit.

=== CITATION FORMAT (CRITICAL) ===
ALWAYS write section IDs as [Section X.Y.Z] — the frontend auto-generates correct links.
NEVER write bare URLs. The frontend renders correct links from section IDs.
For Chapter 900 exceptions: cite as [Section 900.3.10] not just [Section 900].

=== OUTPUT FORMAT — FOLLOW EXACTLY ===

Use this structure for every response. The frontend renders markdown — use it.

## 📋 Quick Answer
State the key value in 1–2 sentences. Bold the number. Cite the section inline.
Example: "The maximum building height is **10 m** [Section 10.20.40.10]."

(If exception exists — ONLY if listed in PARCEL DATA:)
⚠️ **Exception #NNN** — [what it changes vs base zone, with specific value]

---

## 📐 [Topic heading — Height / Setbacks / Coverage / Parking / etc.]
• **[Rule name]:** **[value with units]** — plain-English note [Section X.X.X]
• Bold every numeric value. List ALL rules that apply — never stop at the first.
• Include conditionals and "despite" clauses as ⚠️ lines.

(Repeat ## section for each topic the question covers)

---

📄 Sections cited: [every section ID used, comma-separated]

=== STRICT RULES ===
1. DIRECT ANSWERS block in PARCEL DATA is authoritative — use those numbers first.
2. Never cite an overlay that is "None" or "No overlay" in PARCEL DATA.
3. Exception overrides base zone → show exception value and state what it replaces.
4. If the value is genuinely unknown → "Check [Section X] — this covers [topic]."
5. Never invent or abbreviate section numbers.
6. Cover every relevant rule completely. A partial answer is a bad answer.
7. Override order: Exception > Despite clause > Overlay > Zone rule > General regulation.
8. Bold EVERY numeric value: **10 m**, **33%**, **0.6 FSI**, **4 units**.
9. Use ✅ for permitted, ❌ for violations, ⚠️ for exceptions and warnings.
10. Multi-topic questions: give each topic its own ## section.
"""


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

@_traceable(name="quick_answer", tags=["quick_mode"])
def quick_answer(
    question:         str,
    zone_symbol:      str           = "",
    bylaw_chapter:    str           = "",
    exception_number: Optional[int] = None,
    parcel:           dict          = None,
    ai_context:       str           = "",
) -> dict:
    """
    Same retrieval as the full pipeline; shorter synthesis via a fast model.
    Returns the same dict shape as query.answer() so app.py can use it identically.
    """
    if query._openai is None:
        raise RuntimeError("Call init_vertex() first.")

    try:
        clean_q = sanitize_question(question)
    except ValueError as e:
        return {
            "reply": str(e), "sections_used": [],
            "zone_symbol": zone_symbol, "bylaw_chapter": bylaw_chapter,
            "chunks_count": 0, "error": "input_sanitization",
        }

    # ── Retrieval: skip reranker for speed (quick mode) ──────────────────────
    try:
        chunks = retrieve(
            question         = clean_q,
            zone_symbol      = zone_symbol,
            bylaw_chapter    = bylaw_chapter,
            exception_number = exception_number,
            skip_rerank      = True,
        )
    except Exception as e:
        print(f"[QUICK_ANSWER] retrieve() failed: {type(e).__name__}: {e}")
        return {
            "reply": "Retrieval failed — the vector search encountered an error. Please try again.",
            "sections_used": [], "zone_symbol": zone_symbol,
            "bylaw_chapter": bylaw_chapter, "chunks_count": 0, "error": "retrieval_error",
        }

    # ── Build context — same helper as full pipeline ──────────────────────────
    parcel_ctx = build_rich_parcel_context(parcel) if parcel else ai_context

    # ── Condensed by-law context: section IDs + first 200 chars of text ───────
    bylaw_lines: list[str] = []
    for c in chunks:
        sid   = c.get("section_id", "?")
        title = c.get("section_title", "")
        text  = (c.get("text") or "").strip()
        src   = c.get("source", "")
        is_exc = c.get("is_exception") or src in ("exception", "exception_direct")
        prefix = "EXCEPTION" if is_exc else "RULE"
        override = " [OVERRIDES base zone]" if is_exc else ""
        bylaw_lines.append(f"[{prefix}] {sid} — {title}{override}\n{text}")
    bylaw_ctx = "\n\n".join(bylaw_lines) if bylaw_lines else "(No sections retrieved.)"

    prompt = (
        f"=== PARCEL DATA ===\n{parcel_ctx}\n\n"
        f"=== RELEVANT BY-LAW SECTIONS ({len(chunks)}) ===\n{bylaw_ctx}\n\n"
        f"=== QUESTION ===\n{clean_q}\n\n"
        f"Answer in plain English, covering all relevant rules completely, following the format exactly."
    )

    print(f"[QUICK_ANSWER] model={_QUICK_MODEL}  chunks={len(chunks)}  prompt={len(prompt)} chars")

    import threading
    _t_synthesis = time.perf_counter()
    synth_usage  = {}
    try:
        resp = query._openai.chat.completions.create(
            model       = _QUICK_MODEL,
            messages    = [
                {"role": "system", "content": _QUICK_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            temperature = 0.1,
        )
        if resp.usage:
            synth_usage = {
                "prompt":     resp.usage.prompt_tokens,
                "completion": resp.usage.completion_tokens,
                "total":      resp.usage.total_tokens,
            }
        raw_text = resp.choices[0].message.content or ""
    except Exception as e:
        print(f"[QUICK_ANSWER] OpenAI call failed: {type(e).__name__}: {e}")
        raw_text = ""
    synthesis_ms = int((time.perf_counter() - _t_synthesis) * 1000)

    if not raw_text:
        return {
            "reply": (
                "The model returned an empty response. "
                f"Try Full mode instead — it uses {query.CHAT_MODEL}."
            ),
            "sections_used": [c.get("section_id", "") for c in chunks],
            "zone_symbol":   zone_symbol,
            "bylaw_chapter": bylaw_chapter,
            "chunks_count":  len(chunks),
            "error":         "empty_model_response",
        }

    reply = sanitize_output(raw_text)
    print(f"[QUICK_ANSWER] reply={len(reply)} chars:\n{reply}\n")

    # ── ⏱  QUICK ANSWER PERF REPORT ──────────────────────────────────────────
    rp          = getattr(query._retrieve_perf_local, "__dict__", {}).get(threading.get_ident(), {})
    retrieve_ms = rp.get("retrieve_ms", 0)
    total_ms    = retrieve_ms + synthesis_ms

    def _fmt_tok(u):
        if not u: return "—"
        return f"in={u.get('prompt',0):,}  out={u.get('completion',0):,}  total={u.get('total',0):,}"

    from query import _print_perf
    _print_perf(
        title=f"QUICK-ANSWER SYNTHESIS  [{_QUICK_MODEL}]",
        rows=[
            (f"Synthesis  ({_QUICK_MODEL})",  f"{synthesis_ms:>6} ms",  _fmt_tok(synth_usage)),
            ("─" * 35,                         "─" * 9,                  ""),
            ("Retrieve (from above)",           f"{retrieve_ms:>6} ms",  ""),
            ("END-TO-END  (retrieve+synth)",    f"{total_ms:>6} ms",     f"synth: {_fmt_tok(synth_usage)}"),
        ],
    )

    return {
        "reply":         reply,
        "sections_used": [c.get("section_id", "") for c in chunks],
        "zone_symbol":   zone_symbol,
        "bylaw_chapter": bylaw_chapter,
        "chunks_count":  len(chunks),
    }
