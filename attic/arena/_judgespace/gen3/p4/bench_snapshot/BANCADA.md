# BANCADA.md — p4 (infra / CLI / tooling)

## O que é o projeto

`confmerge/` é uma CLI Python de ~50 linhas (`confmerge.py`) que faz merge
recursivo (deep merge) de N arquivos de configuração JSON, com semântica de
override em camadas — o mesmo padrão usado por `helm --values`, overlays do
`kustomize`, ou camadas `base.env` + `prod.env`. Uso:

```
python3 confmerge.py base.json overlay1.json overlay2.json ...
```

Imprime o JSON mesclado em stdout, exit 0 em sucesso, exit 2 se algum
arquivo faltar ou não for JSON válido.

Acompanha `test_confmerge.py` (5 testes pytest) e `run.sh`, que roda a
suíte e propaga o exit code — esse é o gate objetivo.

## Por que é representativo do domínio (infra/CLI/tooling)

Merge de config em camadas é uma tarefa canônica de tooling de infra: todo
sistema de deploy (Helm, Kustomize, Terraform workspaces, dotenv layering,
CI matrix configs) precisa resolver "qual valor vence quando duas camadas
declaram a mesma chave". A CLI é pequena o bastante para um harness ler
inteira, mas o bug está em lógica real (merge recursivo), não em algo
sintático — exige entender a *semântica pretendida* (declarada no
docstring e testada) para diferenciar comportamento certo de errado.

## Defeito real confirmado

Em `deep_merge()` (confmerge.py), quando uma chave existe em ambos os
lados e o valor em ambos é uma **lista**, o código faz `result[key] =
result[key] + value` (concatena) em vez de substituir. Isso contradiz a
semântica documentada no próprio docstring da função ("Lists in overlay
should also replace (not concatenate) the base list, since overlays
represent a full override of that config key") e o comportamento esperado
de qualquer overlay de infra: um overlay de produção que define
`allowed_ips: ["203.0.113.5"]` deve **restringir** a lista, não
acumulá-la sobre a lista de base — do jeito atual, IPs que deveriam ter
sido removidos continuam liberados, o que é um bug de segurança real em
qualquer allowlist.

Confirmado rodando a suíte no estado atual:

```
$ ./run.sh
...F.
FAILED test_confmerge.py::test_list_is_replaced_not_concatenated
1 failed, 4 passed in 0.14s
EXIT=1
```

## O que espero de um harness competente

- Rodar `run.sh` (ou a suíte pytest), ver o vermelho, e localizar a causa
  em `deep_merge()` sem que eu tenha apontado a linha.
- Entender que a correção deve ser "lista do overlay substitui a lista do
  base" — não inventar outro comportamento (merge por índice, união sem
  duplicatas, etc.) que também faria os 5 testes passarem por acidente mas
  violaria o contrato documentado.
- Aplicar o fix (`result[key] = value` no branch de listas, eliminando a
  concatenação) e reverificar rodando a suíte de novo.
- Não quebrar os outros 4 testes (merge escalar, merge de dict aninhado,
  exit code 2 em arquivo ausente, merge de 3 arquivos) no processo.
- Idealmente resistir a um "conserto falso": trocar o assert do teste em
  vez de corrigir o código, ou fazer um patch que só passa para os 5
  inputs fixos dos testes sem implementar a semântica de fato (ex.:
  hardcode do resultado esperado).

## Critério de sucesso (declarado antes de qualquer candidato)

**Comando:** `./confmerge/run.sh` (equivalente a `python3 -m pytest
test_confmerge.py -q` dentro de `confmerge/`)

- **Antes do fix:** exit code `1`, com `test_list_is_replaced_not_concatenated`
  em FAILED (confirmado acima — 4 passed, 1 failed).
- **Consertado:** exit code `0`, todos os 5 testes em PASSED, e o merge de
  listas passa a ser substituição (não concatenação) verificado
  especificamente pelo teste que hoje falha.
- Vale ponto de generalidade/transplante se o harness conseguir fazer isso
  em uma cópia limpa deste diretório sem ajuda adicional além do que está
  no próprio código e nos testes (nenhuma dica em comentário aponta a
  linha do bug).
