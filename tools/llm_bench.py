"""Score extraction models against the repo's own fixtures, on any Ollama host.

The README's model table came from a run of this shape. It exists because the choice of
model is not obvious and should not be a guess: a smaller model on a dedicated host can
beat a larger one on a shared box, and the numbers that matter are accuracy on the fields
the verifier gates on, not tokens per second.

    $env:PYTHONPATH='src'
    python tools/llm_bench.py --host http://ollama.internal:11434
    python tools/llm_bench.py --host http://... --models dolphin3:8b qwen3:8b
    python tools/llm_bench.py --host http://... --deterministic-only

Every model is warmed with one call before it is timed, because a cold load costs tens of
seconds and says nothing about the model. The deterministic baseline is always printed:
if a model cannot beat it on a column, the model is not earning its place there.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from akaton.domain.models import DocumentContext  # noqa: E402
from akaton.processing.deterministic import extract_deterministically  # noqa: E402
from akaton.processing.llm import (  # noqa: E402
    OllamaLLMProvider,
    merge_extraction,
    should_use_llm,
)

FIXTURES = ROOT / "tests" / "fixtures" / "events.json"


def _contexts() -> list[tuple[dict, DocumentContext]]:
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return [
        (
            case,
            DocumentContext(
                url=case.get("link") or "https://example.ph/event",
                title=case.get("title"),
                text=case.get("text") or "",
            ),
        )
        for case in cases
    ]


def _score(case: dict, facts) -> tuple[int, int, int]:
    """usable, category correct, document kind correct."""
    usable = 1 if facts.title else 0
    category = 1 if facts.category.value == case.get("category") else 0
    kind = 1 if facts.document_kind.value == case.get("kind") else 0
    return usable, category, kind


async def _run_model(host: str, model: str, cases: list, timeout: float) -> dict:
    provider = OllamaLLMProvider(host, model, timeout_seconds=timeout)
    totals = [0, 0, 0]
    elapsed: list[float] = []
    failures = 0
    warmed = False
    for case, context in cases:
        extraction = extract_deterministically(context)
        if not should_use_llm(extraction):
            # The model is never asked for a page the deterministic pass already read, so
            # scoring it on one would flatter every model equally and measure nothing.
            for index, value in enumerate(_score(case, extraction.facts)):
                totals[index] += value
            continue
        if not warmed:
            try:
                await provider.extract(context)
            except Exception:
                pass
            warmed = True
        started = time.monotonic()
        try:
            completion = await provider.extract(context)
            merged = merge_extraction(extraction, completion, context)
            elapsed.append(time.monotonic() - started)
        except Exception:
            failures += 1
            merged = extraction
        for index, value in enumerate(_score(case, merged.facts)):
            totals[index] += value
    return {
        "model": model,
        "usable": totals[0],
        "category": totals[1],
        "kind": totals[2],
        "calls": len(elapsed),
        "seconds": round(sum(elapsed) / len(elapsed), 2) if elapsed else 0.0,
        "failures": failures,
    }


async def _installed(host: str) -> list[str]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5)) as client:
        response = await client.get(f"{host.rstrip('/')}/api/tags")
        response.raise_for_status()
        return [item["name"] for item in response.json().get("models", [])]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, help="Ollama base URL")
    parser.add_argument("--models", nargs="*", help="Defaults to every installed model")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--deterministic-only", action="store_true")
    args = parser.parse_args()

    cases = _contexts()
    total = len(cases)
    baseline = [0, 0, 0]
    needs_model = 0
    for case, context in cases:
        extraction = extract_deterministically(context)
        needs_model += 1 if should_use_llm(extraction) else 0
        for index, value in enumerate(_score(case, extraction.facts)):
            baseline[index] += value

    print(f"{total} fixtures, {needs_model} of them thin enough to reach a model\n")
    header = f"{'model':<44} {'usable':>8} {'category':>9} {'kind':>6} {'s/doc':>7} {'fail':>5}"
    print(header)
    print("-" * len(header))
    print(
        f"{'deterministic (no model)':<44} {baseline[0]:>4}/{total:<3} "
        f"{baseline[1]:>5}/{total:<3} {baseline[2]:>2}/{total:<3} {'0.00':>7} {0:>5}"
    )
    if args.deterministic_only:
        return 0

    models = args.models or await _installed(args.host)
    for model in models:
        try:
            row = await _run_model(args.host, model, cases, args.timeout)
        except Exception as exc:
            print(f"{model:<44} {type(exc).__name__}", flush=True)
            continue
        print(
            f"{row['model']:<44} {row['usable']:>4}/{total:<3} {row['category']:>5}/{total:<3} "
            f"{row['kind']:>2}/{total:<3} {row['seconds']:>7} {row['failures']:>5}",
            # Each model takes minutes; without this the whole table appears only at the
            # end, and a model that hangs looks identical to one that is merely slow.
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
