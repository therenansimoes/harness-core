"""test_pricing.py — critério de aceite selado do build_j_b2b (trilha B,
SPEC-J2 design 2, B1). Copiado pro workspace só na hora do `accept.py`
verificar (mesmo mecanismo de `judges/_sealed/j_b2b/` da J1) — o agente
nunca vê este arquivo, só o `brief.md`.

Roda com `python3 -m unittest` a partir do workspace, com o `pricing.py` do
agente em `sys.path`.
"""
from __future__ import annotations

import unittest

import pricing


class TestValidacao(unittest.TestCase):
    def test_carrinho_vazio_e_invalido(self):
        resp = pricing.handle_quote_request({"items": []})
        self.assertEqual(resp["status"], 400)
        self.assertIn("error", resp["body"])

    def test_items_ausente_e_invalido(self):
        resp = pricing.handle_quote_request({})
        self.assertEqual(resp["status"], 400)

    def test_qty_zero_invalida_pedido_inteiro(self):
        resp = pricing.handle_quote_request(
            {"items": [{"sku": "A", "qty": 0, "unit_price_cents": 1000}]}
        )
        self.assertEqual(resp["status"], 400)

    def test_preco_negativo_invalida_pedido_inteiro(self):
        resp = pricing.handle_quote_request(
            {"items": [{"sku": "A", "qty": 5, "unit_price_cents": -100}]}
        )
        self.assertEqual(resp["status"], 400)


class TestCalculo(unittest.TestCase):
    def test_sem_desconto_com_frete_fixo(self):
        resp = pricing.handle_quote_request(
            {"items": [{"sku": "A", "qty": 10, "unit_price_cents": 1000}]}
        )
        self.assertEqual(resp["status"], 200)
        body = resp["body"]
        self.assertEqual(body["subtotal_cents"], 10_000)
        self.assertEqual(body["discount_percent"], 0)
        self.assertEqual(body["shipping_cents"], 2_500)
        self.assertEqual(body["total_cents"], 12_500)

    def test_desconto_5_porcento_a_partir_de_50_unidades(self):
        resp = pricing.handle_quote_request(
            {"items": [{"sku": "A", "qty": 50, "unit_price_cents": 10_000}]}
        )
        body = resp["body"]
        self.assertEqual(body["subtotal_cents"], 500_000)
        self.assertEqual(body["discount_percent"], 5)
        self.assertEqual(body["total_cents"], 477_500)  # 475000 + 2500 frete

    def test_desconto_10_porcento_e_frete_gratis_a_partir_de_100_unidades(self):
        resp = pricing.handle_quote_request(
            {"items": [{"sku": "A", "qty": 100, "unit_price_cents": 10_000}]}
        )
        body = resp["body"]
        self.assertEqual(body["discount_percent"], 10)
        self.assertEqual(body["shipping_cents"], 0)
        self.assertEqual(body["total_cents"], 900_000)

    def test_desconto_soma_quantidade_de_varios_itens(self):
        resp = pricing.handle_quote_request(
            {
                "items": [
                    {"sku": "A", "qty": 60, "unit_price_cents": 5_000},
                    {"sku": "B", "qty": 40, "unit_price_cents": 5_000},
                ]
            }
        )
        body = resp["body"]
        # 60+40 = 100 unidades -> 10%, mesmo nenhum item isolado batendo 100.
        self.assertEqual(body["discount_percent"], 10)

    def test_valores_monetarios_sao_inteiros(self):
        resp = pricing.handle_quote_request(
            {"items": [{"sku": "A", "qty": 3, "unit_price_cents": 333}]}
        )
        body = resp["body"]
        for key in ("subtotal_cents", "shipping_cents", "total_cents"):
            self.assertIsInstance(body[key], int)


if __name__ == "__main__":
    unittest.main()
