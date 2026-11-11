"""
casa_llm.py — shared provider-switchable text completion helper.

Used by Hermes (findings analysis) and Farnsworth (action planning) — both are
terse, non-streaming, tool-free JSON-out calls at the "small" tier. Amy and Fry have
their own provider branching (casa_amy.py, casa_fry.py) since their calls need reasoning
effort, streaming, and the web_search/web_fetch tools, which don't fit this
generic shape; they share `broker_client` below.

Switch providers via LLM_PROVIDER in /etc/planetexpress.env (see config.py) —
callers here never touch a provider SDK directly, so nothing in casa_hermes.py
or casa_farnsworth.py needs to change when the provider flips.

rc14.8: whenever it runs, the call goes to the missions model broker's Core channel
(loopback, an access code held only in this process), which relays it with the broker's own
key and records it next to the missions' calls: one key, one record, one spend figure.
"""

import config


def provider() -> str:
    """Which provider v3's calls use now: the model broker whenever its Core channel runs (rc14.8: one key, one record, no switch
    to set), else LLM_PROVIDER, the direct fallback while the broker is not running (missions off, its key unset)."""
    from planet_express.missions.model_broker import core_endpoint

    return "broker" if core_endpoint() is not None else config.LLM_PROVIDER


def broker_client(purpose: str):
    """An Anthropic client for the model broker's Core channel. `purpose` names the caller in the broker's record (it is read by the
    broker, never sent on to the vendor)."""
    import anthropic

    from planet_express.missions.model_broker import core_endpoint

    endpoint = core_endpoint()
    if endpoint is None:
        raise RuntimeError("the model broker's Core channel is not running "
                           "(missions.model_broker must be enabled, and its key set in Core's environment)")
    base_url, access = endpoint
    # The broker is on loopback: an outbound proxy from Core's environment (HTTP_PROXY, ALL_PROXY) must not carry this one hop, or
    # a remote proxy would be asked for 127.0.0.1 (Codex, rc14.8). The broker itself still reaches the vendor its own way.
    # An explicit transport: the SDK's default client otherwise mounts the environment's proxies itself, whatever trust_env says.
    return anthropic.Anthropic(base_url=base_url, api_key=access, default_headers={"x-pe-purpose": purpose}, max_retries=2,
                               http_client=anthropic.DefaultHttpxClient(transport=_sdk_http().HTTPTransport(), trust_env=False))


def _sdk_http():
    """The HTTP library the installed SDK's own client is built on: `httpx2` for anthropic 1.x and later, `httpx` for 0.x (the host
    ran 0.94 with no httpx2 when rc14.8 was written: Codex). A transport from the other one would be refused or fail."""
    import importlib

    import anthropic

    for cls in anthropic.DefaultHttpxClient.__mro__:
        root = (getattr(cls, "__module__", "") or "").partition(".")[0]
        if root in ("httpx2", "httpx"):
            return importlib.import_module(root)
    raise RuntimeError("the installed anthropic SDK's HTTP client is neither httpx nor httpx2")


def ask_with_web_tools(client, model: str, system_prompt: str, user_content: str, max_tokens: int, *, max_search_uses: int,
                       max_fetch_uses: int, tools_optional: bool = False, log=None):
    """Amy's and Fry's research call: streamed, adaptive thinking at high effort, with the vendor's web search and fetch. Returns
    (final message, researched): `researched` is False when the call ran without its web tools. `tools_optional` (the broker path): a
    gateway in front of the vendor may not take the vendor's server tools; then the call is made once more without them, the model is
    told research is unavailable, and the CALLER must mark its answer unresearched (review, rc14.8: the model's own say-so is not
    enough)."""
    import anthropic

    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": max_search_uses},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_fetch_uses},
    ]

    plain = {"value": False}                     # the model takes no adaptive thinking or effort: ask without them

    def run(with_tools: bool):
        content = user_content if with_tools else (
            user_content + "\n\nWeb search and fetch are not available for this request. Do not claim research: set any "
            "researched or sufficient-context field to false and list no sources.")
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            **({} if plain["value"] else {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
            **({"tools": tools} if with_tools else {}),
            messages=[{"role": "user", "content": content}],
        ) as stream:
            return stream.get_final_message()

    def attempt(with_tools: bool):
        try:
            return run(with_tools)
        except anthropic.BadRequestError as exc:
            # An older model (Sonnet 4.5, from an existing broker config) takes neither adaptive thinking nor effort: asked again
            # without them, once (review, rc14.8).
            words = str(exc).lower()
            if plain["value"] or not any(w in words for w in ("thinking", "effort", "output_config", "adaptive")):
                raise
            plain["value"] = True
            return run(with_tools)

    try:
        return attempt(True), True
    except anthropic.BadRequestError as exc:
        # Only a refusal OF THE TOOLS: a refused model, a request too large or anything else is not hidden behind a no-tools retry
        # (review, rc14.8).
        if not tools_optional or "tool" not in str(exc).lower():
            raise
        if log is not None:
            log.warning(f"The model service refused the web tools ({exc.status_code}); asking again without them")
        return attempt(False), False


def research(purpose: str, system_prompt: str, user_content: str, max_tokens: int, *, max_search_uses: int, max_fetch_uses: int,
             log=None) -> tuple[str, bool] | None:
    """Amy's and Fry's research call on the provider in use: (the answer's text, whether it was researched), or None when that
    provider is OpenAI (each caller keeps its own OpenAI request). The client choice lives here, once."""
    import anthropic

    used = provider()
    if used not in ("broker", "anthropic"):
        return None
    client = broker_client(purpose) if used == "broker" else anthropic.Anthropic(api_key=config.anthropic_api_key())
    message, researched = ask_with_web_tools(client, config.model_for("large", used), system_prompt, user_content, max_tokens,
                                             max_search_uses=max_search_uses, max_fetch_uses=max_fetch_uses,
                                             tools_optional=used == "broker", log=log)
    text = answer_text(message)
    if not text and log is not None:
        log.error(f"The {purpose} answer had no text block (stop_reason={message.stop_reason!r}, "
                  f"content block types={[b.type for b in message.content]!r}) — likely ran out of tokens mid-research")
    return text, researched


def answer_text(message) -> str:
    """The answer: the text written after the model's last tool use. With web tools the model may write a line (or an empty
    block) between searches, so the first text block is not the answer (live install on rc14.8); a cited answer can be split
    into several text blocks, so the final run of them is joined. A thinking block is not text and does not end the run."""
    run: list[str] = []
    for block in message.content:
        if block.type == "text":
            run.append(block.text or "")
        elif block.type not in ("thinking", "redacted_thinking"):
            run = []
    return "".join(run).strip()


def complete(system_prompt: str, user_content: str, max_tokens: int, tier: str = "small", purpose: str = "core") -> str:
    """Send a system+user prompt to the configured provider's small/large tier
    model, return the raw text response (caller handles JSON parsing/fallback)."""
    used = provider()
    model = config.model_for(tier, used)

    if used == "broker":
        import anthropic

        client = broker_client(purpose)
        request = {"model": model, "max_tokens": max_tokens, "system": system_prompt,
                   "messages": [{"role": "user", "content": user_content}]}
        try:
            # Terse JSON at a small output budget: low effort keeps thinking short so it never eats the answer.
            message = client.messages.create(**request, output_config={"effort": "low"})
        except anthropic.BadRequestError:
            # The broker's small model is an alias ("latest"): one that takes no effort setting (Haiku 4.5) refuses it. Asked again
            # without it (review, rc14.8).
            message = client.messages.create(**request)
        return answer_text(message)

    if used == "anthropic":
        import anthropic

        client = anthropic.Anthropic(api_key=config.anthropic_api_key())
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return answer_text(message)

    if used == "openai":
        import openai

        client = openai.OpenAI(api_key=config.openai_api_key())
        response = client.responses.create(
            model=model,
            # "none" was too weak to reliably follow explicit safety constraints (e.g.
            # Farnsworth's sudo-scope rule) — "low" costs the same per token, just better
            # instruction-following. See project_casaserver_sysadmin_agents memory, 2026-07-13.
            reasoning={"effort": "low"},
            max_output_tokens=max_tokens,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        return response.output_text.strip()

    raise RuntimeError(f"Unknown LLM_PROVIDER {used!r} — expected 'anthropic' or 'openai'.")
