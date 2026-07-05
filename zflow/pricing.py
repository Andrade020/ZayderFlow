"""Preços por modelo e a comparação "quanto custaria no Claude Opus".

litellm não mapeia o preço de vários provedores que o Zayder usa (GLM via base
custom; às vezes DeepSeek). Então gravamos o dado-verdade — tokens in/out por
chamada — e calculamos o custo da tabela publicada de cada provedor. A mesma
tabela é usada para responder "se esses MESMOS tokens tivessem rodado no Opus,
quanto seria?" — a base do selo "economizou" da UI.

Preços em USD por 1M de tokens (input, output). Conferidos em 2026-06-27/28:
DeepSeek V4 (api-docs.deepseek.com, pós-promo), z.ai (docs.z.ai), Anthropic
(skill claude-api). Atualize aqui se um provedor mudar a tabela.
"""

from __future__ import annotations

# (input, output) por 1M de tokens
PRICES_PER_M: dict[str, tuple[float, float]] = {
    # DeepSeek V4 (linha atual do projeto)
    "deepseek-v4-pro": (0.435, 0.870),
    "deepseek-v4-flash": (0.09, 0.18),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    # Zhipu GLM (z.ai)
    "glm-5.2": (1.4, 4.4),
    "glm-5.1": (1.4, 4.4),
    "glm-5": (1.0, 3.2),
    "glm-4.7": (0.6, 2.2),
    "glm-4.6": (0.6, 2.2),
    "glm-4.5": (0.6, 2.2),
    "glm-4.5-air": (0.2, 1.1),
    # Anthropic Claude (referência de comparação)
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # alguns comuns via openrouter/gemini
    "gpt-4o-mini": (0.15, 0.60),
    "gemini-2.5-flash": (0.30, 2.50),
}

# modelo de referência do selo "economizou" (o frontier mais caro/capaz)
OPUS_IN_PER_M, OPUS_OUT_PER_M = PRICES_PER_M["claude-opus-4-8"]


def normalize_model(model: str) -> str:
    """'deepseek/deepseek-v4-pro' -> 'deepseek-v4-pro' (tira o prefixo do provider)."""
    return (model or "").split("/")[-1].strip().lower()


def cost_for(model: str, tokens_in: int, tokens_out: int) -> float:
    """Custo real estimado de uma chamada pela tabela do provedor.

    Modelo desconhecido -> 0.0 (lower bound; o selo só aparece com custo > 0).
    """
    price = PRICES_PER_M.get(normalize_model(model))
    if price is None:
        return 0.0
    pin, pout = price
    return (tokens_in * pin + tokens_out * pout) / 1_000_000


def opus_equiv_usd(tokens_in: int, tokens_out: int) -> float:
    """Quanto esses mesmos tokens custariam no Claude Opus 4.8."""
    return (tokens_in * OPUS_IN_PER_M + tokens_out * OPUS_OUT_PER_M) / 1_000_000


def usage_from(response_obj) -> tuple[int, int]:
    """Extrai (prompt_tokens, completion_tokens) de uma resposta litellm/openai."""
    usage = getattr(response_obj, "usage", None) or {}
    get = usage.get if isinstance(usage, dict) else lambda k, d=0: getattr(usage, k, d)
    return int(get("prompt_tokens", 0) or 0), int(get("completion_tokens", 0) or 0)
