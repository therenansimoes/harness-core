"""pricing.py — cotação de pedidos B2B (endpoint + regra de negócio).

Domínio do brief: um carrinho de itens vira uma cotação com subtotal,
desconto por volume e frete. Só stdlib. `handle_quote_request` é o
"endpoint" (função pura, sem HTTP real — o juiz chama direto).

Formato do item: {"sku": str, "qty": int, "unit_price_cents": int}.

Formato da resposta:
  sucesso -> {"status": 200, "body": {"subtotal_cents": int,
              "discount_percent": int, "shipping_cents": int,
              "total_cents": int}}
  erro    -> {"status": 400, "body": {"error": str}}
"""

from __future__ import annotations

# regra: 5% de desconto no subtotal se a soma de qty >= 50; 10% se >= 100.
VOLUME_DISCOUNT_TIERS = [(100, 10), (50, 5)]

# regra: frete grátis se o subtotal com desconto >= 500000 centavos
# (R$5000,00); senão frete fixo de 2500 centavos (R$25,00).
FREE_SHIPPING_THRESHOLD_CENTS = 500_000
FLAT_SHIPPING_CENTS = 2_500


def _validate_items(items: list[dict]) -> str | None:
    """Devolve a mensagem de erro se `items` for inválido, ou None se ok."""
    raise NotImplementedError


def _subtotal_cents(items: list[dict]) -> int:
    """Soma qty * unit_price_cents de todos os itens."""
    raise NotImplementedError


def _discount_percent(total_qty: int) -> int:
    """Percentual de desconto (0, 5 ou 10) pra essa quantidade total."""
    raise NotImplementedError


def _shipping_cents(discounted_subtotal_cents: int) -> int:
    """0 se acima do limiar de frete grátis, senão FLAT_SHIPPING_CENTS."""
    raise NotImplementedError


def calculate_total(items: list[dict]) -> dict:
    """Calcula a cotação completa de um carrinho válido. Assume que
    `_validate_items(items)` já foi checado pelo chamador."""
    raise NotImplementedError


def handle_quote_request(payload: dict) -> dict:
    """Endpoint: recebe {"items": [...]}, devolve {"status", "body"}.

    - status 400 + body {"error": "..."} se o payload for inválido.
    - status 200 + body da cotação (ver `calculate_total`) se válido.
    """
    raise NotImplementedError


if __name__ == "__main__":
    example = {
        "items": [
            {"sku": "WIDGET-1", "qty": 60, "unit_price_cents": 1500},
        ]
    }
    print(handle_quote_request(example))
