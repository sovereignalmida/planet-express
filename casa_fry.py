"""
casa_fry.py — Philip J. Fry: Onboarding
"I'm hip! I'm now! I'm with it!"

Called on-demand via the Telegram /install command, never on a schedule. Given a
project URL, resolves the project's real GitHub repo and authoritative
docker-compose.yml/.env.example (not just paraphrased docs prose), and returns
structured requirements for a human-approved compose diff. Never executes
anything and never writes a compose file itself — casa_farnsworth.py's
_run_install() synthesizes the final stack-specific YAML (container name,
casaproxy network, Traefik labels) from what Fry returns, then routes it through
casa_bender.py's existing diff-approve flow.

Uses the "large" tier model (config.model_for("large")), same as Amy — this needs
real research synthesis, and only runs on-demand so cost is bounded regardless of
provider.
"""

import json
import logging
from datetime import datetime, timezone

import config

log = logging.getLogger("planetexpress.fry")

MAX_TOKENS = 8000
MAX_SEARCH_USES = 4  # Anthropic only — OpenAI's Responses API web_search tool has no per-call cap
MAX_FETCH_USES = 4   # Anthropic only — no web_fetch-equivalent tool on the OpenAI side

SYSTEM_PROMPT = """You are Fry, onboarding assistant for the Planet Express home lab (CasaMediaServer).

Given a URL to a self-hosted project (docs page, GitHub repo, or landing page), your job is to
find that project's REAL, authoritative deployment requirements — not paraphrase marketing copy.

RULES:
- Find the project's actual GitHub repository first. Prefer its own docker-compose.yml or
  docker-compose.example.yml and .env.example committed in the repo over anything described in
  prose on a docs site — docs pages drift out of date, the repo's own compose file is ground
  truth.
- Cite the source URL(s) you actually used.
- You NEVER execute anything, and you never invent an image tag, port, or volume path you didn't
  actually find — if the authoritative compose file isn't findable, say so
  (sufficient_context: false) rather than guessing from memory.
- Extract the upstream service's compose block as close to verbatim as you found it in
  upstream_service_yaml, for audit purposes only — the caller does NOT paste this in directly (it
  builds a new service block itself from image/volumes/healthcheck below, since this host never
  binds host ports and always supplies its own container_name/networks/labels). What matters most
  is that image, volumes, and healthcheck below are each accurate and verbatim.
- Flag guardrails explicitly, they gate whether a human even sees an auto-proposed diff:
  - needs_docker_socket: true if the compose mounts /var/run/docker.sock directly (not through a
    socket-proxy sidecar) — mounting the real Docker socket directly into an arbitrary third-party
    container is a meaningful blast-radius increase and should never be silently auto-proposed.
  - has_own_reverse_proxy: true if the project bundles or assumes its own reverse proxy /
    TLS termination (e.g. its own nginx/Caddy/Traefik container) — this host already has Traefik
    fronting everything, and running two reverse proxies for one app is a real conflict a human
    needs to resolve, not something to paper over.
  - required_env: list every environment variable the upstream compose/.env.example actually
    requires (name + one-line purpose). Do not invent values — a human generates real secrets
    separately.
  - requires_companion_services: true if the authoritative compose declares any OTHER required
    service besides the main app (its own Postgres/Redis/worker/etc, not an optional add-on) —
    the caller only ever builds a single-service stack file, so a project that genuinely needs
    a companion service must be flagged for manual onboarding, never silently dropped.
  - has_extra_directives: true if the main app's own service block relies on anything beyond
    image/ports/volumes/healthcheck to function — command, entrypoint, depends_on, devices,
    cap_add/cap_drop, a non-default user/security context, OR any environment variable that
    is set (hardcoded or defaulted) to make the app run/function correctly and is NOT already
    a human-supplied secret you've listed in required_env. The caller only ever carries through
    required_env's entries (as secrets a human fills in separately) — it never emits any other
    environment block at all, so any other env var the upstream actually needs would otherwise
    be silently dropped. Flag this true rather than let a silently-misconfigured stack through.
  - named_volumes_need_special_config: true if any of the upstream's named volumes are declared
    with anything beyond a bare "name: {}" at the top level (external: true, a custom driver,
    driver_opts, an explicit external name) — the caller only ever emits plain default named
    volumes, so a project whose data genuinely depends on special volume config must be flagged
    rather than silently given an empty, wrongly-configured volume instead.
- Be concrete and complete on volumes: list every named volume or bind mount the upstream compose
  actually declares, verbatim paths/names, in volumes as "name:container_path" (or
  "name:container_path:ro"/bind-mount form) entries. For each entry that is a named volume (not a
  host bind-mount path), also list its bare name in top_level_volumes so it can be declared under
  a top-level "volumes:" key — bind-mount paths do NOT go in top_level_volumes. A bind mount is
  anything that is a filesystem path rather than a plain volume name: starts with "/", "./", "../",
  or "~" — a named volume's own name never contains any of those characters.
- If ports has more than one entry, you MUST set primary_port to the container_port that serves
  the main web UI (the one that gets routed through the reverse proxy) — never leave the caller to
  guess by picking the first one. If you cannot confidently identify which port is the web UI when
  there's more than one, leave primary_port null and explain the ambiguity in notes; the caller
  will refuse to proceed rather than guess. With exactly one port, set primary_port to that port.

Return ONLY valid JSON, this exact structure, nothing else:
{
  "project_name": "short slug, e.g. transmute",
  "repo_url": "https://github.com/...",
  "sufficient_context": true,
  "sources": ["https://..."],
  "image": "ghcr.io/org/name:tag",
  "ports": [{"container_port": 3313, "purpose": "web UI"}],
  "primary_port": 3313,
  "volumes": ["transmute_data:/app/data"],
  "top_level_volumes": ["transmute_data"],
  "required_env": [{"name": "...", "purpose": "..."}],
  "healthcheck_yaml": "verbatim healthcheck: block AS IT WOULD APPEAR under a service (same 4-space base indent as image/volumes/labels), lines joined with \\n (e.g. '    healthcheck:\\n      test:\\n        - CMD\\n        - ...'), or null if none",
  "needs_docker_socket": false,
  "has_own_reverse_proxy": false,
  "requires_companion_services": false,
  "has_extra_directives": false,
  "named_volumes_need_special_config": false,
  "upstream_service_yaml": "verbatim service: block as found, or null if sufficient_context is false",
  "notes": "anything a human approving this diff should know"
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


def onboard(url: str, stack_name: str, domain: str) -> dict:
    """Ask Fry to resolve a project URL into structured deployment requirements.
    Returns a dict for a human to review before any compose diff is proposed — Fry
    never writes a file and never executes anything; web_search/web_fetch (Anthropic)
    or web_search (OpenAI) are server-side tools the provider runs directly, so
    there's no client-side tool loop to manage here."""
    context = {
        "url": url,
        "requested_stack_name": stack_name,
        "requested_domain": domain,
    }

    log.info(f"Fry onboarding {url!r} as stack {stack_name!r} for domain {domain!r}")

    user_content = f"Resolve deployment requirements for this project:\n{json.dumps(context, separators=(',', ':'))}"

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
        log.error(f"Fry returned invalid JSON: {e}\nRaw:\n{text[:500]}")
        result = {
            "project_name": stack_name,
            "repo_url": None,
            "sufficient_context": False,
            "sources": [],
            "image": None,
            "ports": [],
            "primary_port": None,
            "volumes": [],
            "required_env": [],
            "top_level_volumes": [],
            "healthcheck_yaml": None,
            "needs_docker_socket": False,
            "has_own_reverse_proxy": False,
            "requires_companion_services": False,
            "has_extra_directives": False,
            "named_volumes_need_special_config": False,
            "upstream_service_yaml": None,
            "notes": "Fry's analysis could not be parsed — see planetexpress logs for raw output.",
            "_parse_error": str(e),
        }

    result.setdefault("resolved_at", datetime.now(timezone.utc).isoformat())
    log.info(
        f"Fry's resolution for {stack_name}: sufficient_context={result.get('sufficient_context')}, "
        f"needs_docker_socket={result.get('needs_docker_socket')}, "
        f"has_own_reverse_proxy={result.get('has_own_reverse_proxy')}"
    )
    return result
