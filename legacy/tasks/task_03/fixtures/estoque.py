"""Controle de estoque simples. Tem bugs. Os testes dizem quais."""


def aplicar_movimento(estoque: dict[str, int], sku: str, delta: int) -> dict[str, int]:
    """Aplica entrada (delta>0) ou saída (delta<0). Estoque nunca fica negativo."""
    novo = dict(estoque)
    novo[sku] = novo.get(sku, 0) + delta
    return novo


def abaixo_do_minimo(estoque: dict[str, int], minimos: dict[str, int]) -> list[str]:
    """SKUs cujo saldo está abaixo do mínimo, em ordem alfabética."""
    return [sku for sku, qtd in estoque.items() if qtd < minimos.get(sku, 0)]


def valor_total(estoque: dict[str, int], precos: dict[str, float]) -> float:
    """Valor total do estoque, arredondado em 2 casas."""
    return sum(qtd * precos[sku] for sku, qtd in estoque.items())
