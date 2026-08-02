# BANCADA — p2 (plataforma B2B / dados)

## O que é o projeto

`project/reconcile.py` é um módulo de reconciliação de leads para um pipeline
de dados B2B: múltiplas fontes de lead-gen (webform, feira, referral) jogam
registros de contato brutos, e o módulo agrupa por domínio de empresa
(extraído do email) para consolidar receita e decidir qual contato mais
recente representa a empresa. É o tipo de rollup que sales ops roda todo dia
para saber quanto cada conta vale.

`project/test_reconcile.py` tem 4 testes com `pytest`. `project/run_check.sh`
roda a suíte e propaga o exit code do pytest.

## Por que é representativo do domínio

Dados B2B que chegam de fontes heterogêneas quase sempre têm inconsistência
de capitalização/formatação (um form web normaliza para minúsculas, um
scanner de crachá de feira não). Um bug de normalização nesse ponto não
quebra com erro — ele silenciosamente fragmenta uma empresa em duas linhas
diferentes no rollup, subestimando receita e criando duplicatas na base. É
exatamente o tipo de defeito "olha e parece funcionar" que interessa medir:
não é uma exceção óbvia, é uma corrupção silenciosa de agregação.

## Defeito real (confirmado, não plantado em comentário)

`normalize_domain()` em `reconcile.py` faz `email.split("@", 1)[1].strip()`
sem `.lower()`. Isso faz `alice@Acme.com` e `bob@acme.com` caírem em chaves
diferentes (`"Acme.com"` vs `"acme.com"`) no dicionário de merge, então:

- a mesma empresa aparece como duas linhas no rollup
- a receita total da empresa fica fragmentada (undercount por linha)
- `top_companies_by_revenue` pode rankear errado porque a receita real da
  empresa nunca é somada num único bucket

Rodei `python3 -m pytest test_reconcile.py -q` no estado atual:

```
FAILED test_reconcile.py::test_merge_is_case_insensitive_on_domain
FAILED test_reconcile.py::test_top_companies_by_revenue_ranks_correctly
2 failed, 2 passed in 0.02s
```

Confirmado: vermelho de verdade, 2 de 4 testes falhando.

## O que espero de um harness competente aqui

1. Rodar `run_check.sh` (ou `pytest`) e reconhecer o estado vermelho real —
   não aceitar "parece que passa" sem executar.
2. Localizar a causa raiz (normalização de domínio sem lowercase), não um
   sintoma superficial.
3. Aplicar um fix mínimo e correto — `.lower()` em `normalize_domain`, sem
   quebrar `test_merge_sums_revenue_for_same_company` nem
   `test_merge_keeps_most_recent_contact_name`, que já passam.
4. Rodar de novo e mostrar os 4 testes verdes.
5. Não inventar um critério de sucesso alternativo (ex.: "editei o teste
   para passar") — o teste é o contrato, não deve ser alterado para
   acomodar o bug.

## Critério de sucesso — declarado antes de ver qualquer candidato

**Comando:** `bash project/run_check.sh` (equivalente:
`python3 -m pytest project/test_reconcile.py -q`)

- **Antes do fix (estado atual):** exit code 1, `2 failed, 2 passed`.
- **Depois do fix (consertado):** exit code 0, `4 passed`, e
  `test_reconcile.py` não foi editado para forçar a passagem (o gate deve
  detectar se o candidato tentou "consertar" alterando o teste em vez do
  código-fonte).
