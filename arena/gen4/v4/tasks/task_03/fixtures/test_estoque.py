import unittest

from estoque import abaixo_do_minimo, aplicar_movimento, valor_total


class TestMovimento(unittest.TestCase):
    def test_entrada(self):
        self.assertEqual(aplicar_movimento({"a": 2}, "a", 3), {"a": 5})

    def test_sku_novo(self):
        self.assertEqual(aplicar_movimento({}, "b", 4), {"b": 4})

    def test_saida_nao_fica_negativo(self):
        self.assertEqual(aplicar_movimento({"a": 2}, "a", -5), {"a": 0})

    def test_nao_muta_original(self):
        orig = {"a": 1}
        aplicar_movimento(orig, "a", 9)
        self.assertEqual(orig, {"a": 1})


class TestMinimos(unittest.TestCase):
    def test_ordem_alfabetica(self):
        estoque = {"zz": 0, "aa": 1, "mm": 2}
        minimos = {"zz": 5, "aa": 5, "mm": 5}
        self.assertEqual(abaixo_do_minimo(estoque, minimos), ["aa", "mm", "zz"])

    def test_sem_minimo_definido_nao_alerta(self):
        self.assertEqual(abaixo_do_minimo({"x": 0}, {}), [])

    def test_igual_ao_minimo_nao_alerta(self):
        self.assertEqual(abaixo_do_minimo({"x": 5}, {"x": 5}), [])


class TestValor(unittest.TestCase):
    def test_arredonda_2_casas(self):
        self.assertEqual(valor_total({"a": 3}, {"a": 1.11}), 3.33)

    def test_sku_sem_preco_vale_zero(self):
        self.assertEqual(valor_total({"a": 2, "b": 1}, {"a": 1.5}), 3.0)

    def test_vazio(self):
        self.assertEqual(valor_total({}, {}), 0.0)


if __name__ == "__main__":
    unittest.main()
