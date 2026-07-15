"""
casa_llm.py — shared provider-switchable text completion helper.

Used by Hermes (findings analysis) and Farnsworth (action planning) — both are
terse, non-streaming, tool-free JSON-out calls at the "small" tier. Amy has her
own provider branching in casa_amy.py since her diagnosis calls need reasoning
effort, streaming, and the web_search/web_fetch tools, which don't fit this
generic shape.

Switch providers via LLM_PROVIDER in /etc/planetexpress.env (see config.py) —
callers here never touch a provider SDK directly, so nothing in casa_hermes.py
or casa_farnsworth.py needs to change when the provider flips.
"""

import config


def complete(system_prompt: str, user_content: str, max_tokens: int, tier: str = "small") -> str:
    """Send a system+user prompt to the configured provider's small/large tier
    model, return the raw text response (caller handles JSON parsing/fallback)."""
    provider = config.LLM_PROVIDER
    model = config.model_for(tier)

    if provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic(api_key=config.anthropic_api_key())
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return message.content[0].text.strip()

    if provider == "openai":
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

    raise RuntimeError(f"Unknown LLM_PROVIDER {provider!r} — expected 'anthropic' or 'openai'.")
