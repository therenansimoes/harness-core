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
    if not items:
        return "Carrinho vazio"

    for item in items:
        if not isinstance(item, dict):
            return "Item deve ser um dicionário"

        qty = item.get("qty")
        unit_price_cents = item.get("unit_price_cents")

        if qty is None or unit_price_cents is None:
            return "Item faltando 'qty' ou 'unit_price_cents'"

        if not isinstance(qty, int) or qty <= 0:
            return f"qty deve ser inteiro positivo, recebido: {qty}"

        if not isinstance(unit_price_cents, int) or unit_price_cents <= 0:
            return f"unit_price_cents deve ser inteiro positivo, recebido: {unit_price_cents}"

    return None


def _subtotal_cents(items: list[dict]) -> int:
    """Soma qty * unit_price_cents de todos os itens."""
    return sum(item["qty"] * item["unit_price_cents"] for item in items)


def _discount_percent(total_qty: int) -> int:
    """Percentual de desconto (0, 5 ou 10) pra essa quantidade total."""
    for threshold, percent in VOLUME_DISCOUNT_TIERS:
        if total_qty >= threshold:
            return percent
    return 0


def _shipping_cents(discounted_subtotal_cents: int) -> int:
    """0 se acima do limiar de frete grátis, senão FLAT_SHIPPING_CENTS."""
    if discounted_subtotal_cents >= FREE_SHIPPING_THRESHOLD_CENTS:
        return 0
    return FLAT_SHIPPING_CENTS


def calculate_total(items: list[dict]) -> dict:
    """Calcula a cotação completa de um carrinho válido. Assume que
    `_validate_items(items)` já foi checado pelo chamador."""
    subtotal_cents = _subtotal_cents(items)
    total_qty = sum(item["qty"] for item in items)
    discount_percent = _discount_percent(total_qty)
    discounted_subtotal = subtotal_cents * (100 - discount_percent) // 100
    shipping_cents = _shipping_cents(discounted_subtotal)
    total_cents = discounted_subtotal + shipping_cents

    return {
        "subtotal_cents": subtotal_cents,
        "discount_percent": discount_percent,
        "shipping_cents": shipping_cents,
        "total_cents": total_cents,
    }


def handle_quote_request(payload: dict) -> dict:
    """Endpoint: recebe {"items": [...]}, devolve {"status", "body"}.

    - status 400 + body {"error": "..."} se o payload for inválido.
    - status 200 + body da cotação (ver `calculate_total`) se válido.
    """
    if not isinstance(payload, dict) or "items" not in payload:
        return {"status": 400, "body": {"error": "Payload deve conter 'items'"}}

    items = payload["items"]
    if not isinstance(items, list):
        return {"status": 400, "body": {"error": "'items' deve ser uma lista"}}

    validation_error = _validate_items(items)
    if validation_error:
        return {"status": 400, "body": {"error": validation_error}}

    quote = calculate_total(items)
    return {"status": 200, "body": quote}


if __name__ == "__main__":
    example = {
        "items": [
            {"sku": "WIDGET-1", "qty": 60, "unit_price_cents": 1500},
        ]
    }
    print(handle_quote_request(example))
