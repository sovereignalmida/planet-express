"""
casa_amy.py — Amy Wong: Diagnostician & Researcher
"I'm not a scientist... wait, yes I am!"

Called only when a normal remediation attempt has ALREADY failed — a plain
restart-and-verify plan step, or a Zoidberg canary-update rollback. Reads the
container's own logs, categorizes the failure, and — only when it genuinely looks
like a documented third-party issue — searches that project's own docs/GitHub
issues for a known fix. Never executes anything; produces a diagnosis and a
proposed remediation for a human to approve via Telegram.

Uses the "large" tier model (config.model_for("large")), not the "small" tier
Hermes/Farnsworth use: this needs real reasoning to synthesize research and
propose a multi-step fix, unlike their terse deterministic classification — and
since it only runs on failures (rare), the cost is bounded regardless of provider.
"""

import json
import logging
from datetime import datetime, timezone

import config

log = logging.getLogger("planetexpress.amy")

MAX_TOKENS = 8000
MAX_SEARCH_USES = 4  # Anthropic only — OpenAI's Responses API web_search tool has no per-call cap
MAX_FETCH_USES = 4   # Anthropic only — no web_fetch-equivalent tool on the OpenAI side

SYSTEM_PROMPT = """You are Amy Wong, diagnostician and researcher for the Planet Express home lab (CasaMediaServer).

You are called only when a normal remediation attempt has ALREADY failed — a plain
restart-and-verify, or a canary update rollback. Your job is to figure out WHY, using the
container's own logs plus, only if genuinely warranted, a small amount of external research
into that specific project's own documentation or GitHub issues.

RULES:
- Categorize the failure first from the logs alone, before deciding whether research is
  warranted: config_error, schema_migration, resource_exhaustion, dependency_not_ready, or
  unknown.
- Only search/fetch the web if the failure looks like a known application-level bug or a
  documented upgrade requirement — not for generic infra issues (disk full, OOM, network),
  which you can diagnose from the logs and context alone without research.
- When you do research, prefer the project's own official docs and GitHub issues/discussions
  over third-party blog posts or forum answers of unknown reliability. Cite the source URL for
  any claim that comes from external research.
- You NEVER execute anything. Your output is a diagnosis and a PROPOSED remediation for a human
  to review and approve — never assume it will run automatically.
- Any proposed step that looks like a schema/data migration MUST be preceded by an explicit
  backup step in your proposal — no exceptions.
- If the fix requires editing a docker-compose.yml file, say so explicitly
  (requires_compose_edit: true), and — when the current service's YAML block is given to you
  in the context — set proposed_service_yaml to the COMPLETE corrected block, verbatim,
  ready to literally replace the current one: same top-level "  service_key:" line, same
  2-space base indentation, every unrelated line (comments included) preserved exactly except
  the lines that actually need to change. Do not touch any other service or reformat anything
  you're not fixing. Still also fill compose_edit_description with a one-sentence human summary
  of what changed and why. If no current_service_yaml was given in the context, leave
  proposed_service_yaml null and describe the change in compose_edit_description only — do not
  attempt to write compose YAML from memory. You do not write the file yourself either way;
  a human approves it through a separate diff-review step.
- Be concrete: real shell commands, not vague suggestions.

Return ONLY valid JSON, this exact structure, nothing else:
{
  "category": "config_error|schema_migration|resource_exhaustion|dependency_not_ready|unknown",
  "diagnosis": "one to three sentences explaining the root cause",
  "researched": false,
  "sources": [],
  "proposed_remediation": {
    "summary": "short title",
    "steps": [
      {"n": 1, "description": "...", "command": "..."}
    ],
    "requires_compose_edit": false,
    "compose_edit_description": null,
    "proposed_service_yaml": null,
    "requires_confirmation": true
  },
  "confidence": "high|medium|low"
}"""


def _ask_anthropic(user_content: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key())
    with client.messages.stream(
        model=config.model_for("large"),
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        tools=[
            {"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCH_USES},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": MAX_FETCH_USES},
        ],
        messages=[{"role": "user", "content": user_content}],
    ) as stream:
        response = stream.get_final_message()
    return next((b.text for b in response.content if b.type == "text"), "")


def _ask_openai(user_content: str) -> str:
    import openai

    client = openai.OpenAI(api_key=config.openai_api_key())
    response = client.responses.create(
        model=config.model_for("large"),
        reasoning={"effort": "high"},
        max_output_tokens=MAX_TOKENS,
        tools=[{"type": "web_search"}],
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    return response.output_text


def diagnose(
    stack: str,
    service: str,
    container_name: str,
    reason: str,
    logs_tail: str,
    current_service_yaml: str | None = None,
) -> dict:
    """Ask Amy to diagnose a failure that already survived a normal restart/rollback
    attempt. Returns her structured diagnosis + proposed remediation. Never executes
    anything — web_search (and web_fetch on Anthropic) are server-side tools the
    provider runs directly; there is no client-side tool loop to manage here.

    current_service_yaml, when the caller can find it, is the exact verbatim compose
    block for this service — gives Amy something concrete to edit instead of asking
    her to write compose YAML from memory, which is how you get a plausible-looking
    but wrong diff."""
    context = {
        "stack": stack,
        "service": service,
        "container": container_name,
        "failure_reason": reason,
        "recent_logs": logs_tail[-4000:],  # keep the prompt bounded
        "current_service_yaml": current_service_yaml,
    }

    log.info(f"Amy investigating {stack}/{service} ({container_name}): {reason}")

    user_content = f"Diagnose this failure:\n{json.dumps(context, separators=(',', ':'))}"

    if config.LLM_PROVIDER == "anthropic":
        text = _ask_anthropic(user_content)
    elif config.LLM_PROVIDER == "openai":
        text = _ask_openai(user_content)
    else:
        raise RuntimeError(f"Unknown LLM_PROVIDER {config.LLM_PROVIDER!r} — expected 'anthropic' or 'openai'.")

    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        log.error(f"Amy returned invalid JSON: {e}\nRaw:\n{text[:500]}")
        result = {
            "category": "unknown",
            "diagnosis": "Amy's analysis could not be parsed — see planetexpress logs for raw output.",
            "researched": False,
            "sources": [],
            "proposed_remediation": {
                "summary": "Manual investigation required",
                "steps": [],
                "requires_compose_edit": False,
                "compose_edit_description": None,
                "proposed_service_yaml": None,
                "requires_confirmation": True,
            },
            "confidence": "low",
            "_parse_error": str(e),
        }

    result.setdefault("diagnosed_at", datetime.now(timezone.utc).isoformat())
    log.info(
        f"Amy's diagnosis for {stack}/{service}: category={result.get('category')}, "
        f"confidence={result.get('confidence')}, researched={result.get('researched')}"
    )
    return result
