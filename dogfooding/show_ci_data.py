"""Show the data CheckCIWithLogs would feed into the fix prompt for a
given GitHub Actions run URL.

Reuses the production extractor (``_summarize_log``) and log-fetch helper
(``_get_failed_logs``) from ``norn.stages.check_ci``, so what you see here
is byte-identical to what ``check_and_fix_ci.py`` would hand to Claude.

Usage::

    # Exact text the fix step sees in check_and_fix_ci.py (recommended):
    uv run python dogfooding/show_ci_data.py <run-url> --pipeline

    # Building blocks of that pipeline, individually:
    uv run python dogfooding/show_ci_data.py <run-url> --extract-step
    uv run python dogfooding/show_ci_data.py <run-url> --extract-step --no-haiku

    # Other modes:
    uv run python dogfooding/show_ci_data.py <run-url>                # generic anchor extractor
    uv run python dogfooding/show_ci_data.py <run-url> --raw          # untouched concatenated logs
    uv run python dogfooding/show_ci_data.py <run-url> --json         # CheckCI dict as JSON
    uv run python dogfooding/show_ci_data.py <run-url> --surefire     # Maven/Surefire extractor
    uv run python dogfooding/show_ci_data.py <run-url> --surefire --no-haiku

Accepted URL forms (anything containing ``/actions/runs/<id>``):
  - https://github.com/owner/repo/actions/runs/123456789
  - https://github.com/owner/repo/actions/runs/123456789/job/987654321
  - https://github.com/owner/repo/actions/runs/123456789/attempts/2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from typing import Any


def _ensure_githubkit() -> None:
    """Install ``githubkit`` into the active interpreter if missing.

    ``CheckCI`` imports ``githubkit`` lazily (it lives in the ``github``
    optional extra); rather than fail late with a ``ModuleNotFoundError``,
    install it eagerly into ``sys.executable`` before importing the
    norn stages.
    """
    try:
        import githubkit  # noqa: F401
        return
    except ImportError:
        pass

    print("[show_ci_data] installing githubkit into active venv...", file=sys.stderr)
    subprocess.check_call(
        ["uv", "pip", "install", "--quiet", "--python", sys.executable, "githubkit"],
    )
    import githubkit  # noqa: F401


_ensure_githubkit()

from norn.models import PipelineContext, StageResult
from norn.stages.check_ci import (
    _create_client,
    _get_failed_logs,
    _resolve_token,
    _summarize_log,
)
from norn.stages.check_ci_surefire import (
    _haiku_summarize,
    extract_surefire_failures,
)
from norn.stages.extract_failing_step import (
    ExtractFailingStep,
    _haiku_compress,
    _slice_failing_step,
)


_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/runs/(?P<run_id>\d+)"
)


def parse_run_url(url: str) -> tuple[str, str, int]:
    m = _URL_RE.search(url)
    if not m:
        raise SystemExit(
            f"Could not parse a GitHub Actions run URL from: {url!r}\n"
            "Expected something like "
            "https://github.com/owner/repo/actions/runs/123456789"
        )
    return m["owner"], m["repo"], int(m["run_id"])


async def _fetch_run_meta(gh: Any, owner: str, repo: str, run_id: int) -> dict:
    resp = await gh.rest.actions.async_get_workflow_run(
        owner=owner, repo=repo, run_id=run_id,
    )
    r = resp.parsed_data
    return {
        "run_id": r.id,
        "name": r.name or "",
        "status": r.status or "",
        "conclusion": r.conclusion,
        "url": r.html_url,
        "head_sha": r.head_sha or "",
    }


async def _amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="show_ci_data.py",
        description=(
            "Reproduce the CheckCIWithLogs payload for a given GitHub "
            "Actions run URL — same dict CheckCI returns, plus the fix-prompt "
            "string CheckCIWithLogs would build from it."
        ),
    )
    parser.add_argument("url", help="GitHub Actions run URL")
    parser.add_argument(
        "--raw", action="store_true",
        help="skip the layered extractor; show raw concatenated job logs",
    )
    parser.add_argument(
        "--max-lines", type=int, default=400,
        help="cap on log lines kept by the summarizer (default: 400)",
    )
    parser.add_argument(
        "--lead-context", type=int, default=30,
        help="lines kept above the anchor line (default: 30)",
    )
    parser.add_argument(
        "--context-lines", type=int, default=20,
        help="window for the marker/keyword fallback pass (default: 20)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit the CheckCI-shaped dict as JSON (no fix-prompt body)",
    )
    parser.add_argument(
        "--surefire", action="store_true",
        help=(
            "use the Maven/Surefire-aware extractor from check_ci_surefire "
            "instead of the generic anchor pass — keeps only failed-class "
            "headers, per-test <<< ERROR! / <<< FAILURE! blocks with their "
            "stack traces, and the trailing [ERROR] Errors:/Failures: summary"
        ),
    )
    parser.add_argument(
        "--extract-step", action="store_true",
        help=(
            "show only the ExtractFailingStep output: Python slices the "
            "failing ##[group]<step>...##[endgroup] block by the "
            "'Failed steps:' header, then Haiku compresses the slice."
        ),
    )
    parser.add_argument(
        "--pipeline", action="store_true",
        help=(
            "reproduce the check_and_fix_ci.py chain: ExtractFailingStep "
            "(python slice → Haiku). This is exactly what the fix prompt "
            "sees as {isolate failing step.output}."
        ),
    )
    parser.add_argument(
        "--no-haiku", action="store_true",
        help=(
            "with --surefire: skip the Haiku compression pass and show only "
            "the deterministic extract"
        ),
    )
    parser.add_argument(
        "--haiku-min-chars", type=int, default=500,
        help="with --surefire: skip Haiku if the extract is below this size (default: 500)",
    )
    parser.add_argument(
        "--haiku-model", default="haiku",
        help="with --surefire: model shorthand passed to claude-agent-sdk (default: haiku)",
    )
    parser.add_argument(
        "--app-packages", action="append", default=[],
        metavar="PKG",
        help=(
            "with --surefire: application package(s) to always preserve in "
            "the compressed stacktrace (e.g. --app-packages 'com.e4marine.*'). "
            "Repeatable, or comma-separated. If omitted, Haiku infers them."
        ),
    )
    args = parser.parse_args(argv)

    app_packages: list[str] = []
    for entry in args.app_packages:
        app_packages.extend(p.strip() for p in entry.split(",") if p.strip())

    owner, repo, run_id = parse_run_url(args.url)

    token = await _resolve_token()
    if not token:
        print(
            "No GitHub token. Set GH_TOKEN/GITHUB_TOKEN or run 'gh auth login'.",
            file=sys.stderr,
        )
        return 2

    gh = _create_client(token)

    meta = await _fetch_run_meta(gh, owner, repo, run_id)

    logs = ""
    if meta["conclusion"] != "success":
        raw = await _get_failed_logs(gh, owner, repo, run_id)
        if args.raw or not raw:
            logs = raw
        elif args.pipeline:
            # Reproduce the exact pipeline chain that feeds the fix step:
            #   check ci → isolate failing step
            # (CompressTestLog used to follow but it re-extracted Haiku's
            # output destructively — its bats/pytest matchers dropped
            # context Haiku had already preserved. Removed from the
            # pipeline, removed here too.)
            check_ci_result = StageResult(
                name="check ci", success=False, output=raw,
            )
            ctx = PipelineContext()
            ctx.results["check ci"] = check_ci_result

            print(
                "[show_ci_data] ExtractFailingStep (python slice → haiku)...",
                file=sys.stderr,
            )
            extract_stage = ExtractFailingStep(source_stage="check ci")
            extract_result = await extract_stage.run(ctx)
            print(
                f"[show_ci_data]   → {len(extract_result.output)} chars",
                file=sys.stderr,
            )
            logs = extract_result.output
        elif args.extract_step:
            sliced = _slice_failing_step(raw)
            if sliced:
                print(
                    f"[show_ci_data] python slice: {len(raw)} → {len(sliced)} chars "
                    f"({100.0 * len(sliced) / max(1, len(raw)):.0f}%)",
                    file=sys.stderr,
                )
                current = sliced
            else:
                print(
                    "[show_ci_data] python slice empty (no Failed steps header "
                    "or no matching group) — sending raw log to Haiku",
                    file=sys.stderr,
                )
                current = raw

            if args.no_haiku:
                logs = current
            else:
                print(
                    f"[show_ci_data] sending {len(current)} chars to "
                    f"{args.haiku_model} for compression...",
                    file=sys.stderr,
                )
                compressed = await _haiku_compress(current, model=args.haiku_model)
                if compressed:
                    print(
                        f"[show_ci_data] haiku output: {len(compressed)} chars",
                        file=sys.stderr,
                    )
                    logs = compressed
                else:
                    print(
                        "[show_ci_data] haiku failed — using python slice",
                        file=sys.stderr,
                    )
                    logs = current
        elif args.surefire:
            extracted = extract_surefire_failures(raw)
            if not extracted:
                print(
                    "[show_ci_data] no surefire markers found — falling back "
                    "to generic anchor extractor",
                    file=sys.stderr,
                )
                logs = _summarize_log(
                    raw,
                    context_lines=args.context_lines,
                    max_lines=args.max_lines,
                    lead_context=args.lead_context,
                )
            else:
                print(
                    f"[show_ci_data] surefire extract: {len(extracted)} chars",
                    file=sys.stderr,
                )
                if args.no_haiku or len(extracted) < args.haiku_min_chars:
                    logs = extracted
                else:
                    print(
                        f"[show_ci_data] sending to {args.haiku_model} for compression...",
                        file=sys.stderr,
                    )
                    summary = await _haiku_summarize(
                        extracted,
                        model=args.haiku_model,
                        app_packages=app_packages or None,
                    )
                    logs = summary or extracted
                    if summary:
                        print(
                            f"[show_ci_data] haiku output: {len(summary)} chars",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            "[show_ci_data] haiku failed — using raw extract",
                            file=sys.stderr,
                        )
        else:
            logs = _summarize_log(
                raw,
                context_lines=args.context_lines,
                max_lines=args.max_lines,
                lead_context=args.lead_context,
            )

    output = {
        "run_id": meta["run_id"],
        "name": meta["name"],
        "status": meta["status"],
        "conclusion": meta["conclusion"],
        "url": meta["url"],
        "logs": logs,
    }

    if args.json:
        json.dump(output, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    print(f"── CheckCI.output dict ──")
    print(f"  run_id     : {output['run_id']}")
    print(f"  name       : {output['name']}")
    print(f"  status     : {output['status']}")
    print(f"  conclusion : {output['conclusion']}")
    print(f"  url        : {output['url']}")
    print(f"  logs       : {len(output['logs'])} chars")
    print()

    if args.pipeline:
        header = (
            "── fix prompt body (what {isolate failing step.output} expands to) ──"
        )
        body = output["logs"]
    else:
        header = (
            "── CheckCIWithLogs body (what the fix prompt sees as "
            "{check ci.output}) ──"
        )
        body = (
            f"Failing workflow: {output['name'] or '(unknown)'}\n"
            f"Run URL: {output['url']}\n\n"
            f"{output['logs']}"
        )
    print(header)
    print(body)
    return 0 if meta["conclusion"] == "success" else 1


def main() -> int:
    return asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
