# RUBRIC v3 — régua da arena (congelada dentro da geração)

Nota final 0–100. Herda da v2 (preservada em `RUBRIC-v2.md`): **todo juiz executa
antes de ler**, veto por incoerência, nota sem citação de execução real é
descartada, parte determinística de 30 pts com veto.

Mudança central da v3: **as personas deixam de ser funções de auditoria**
(Arquiteto/Cético/Produto/Segurança) e passam a ser **usuários reais**, cada
uma com uma ÁREA DE INTERESSE distinta e **nenhum projeto ditado** — o juiz
escolhe/cria a própria bancada dentro da área dele.

| Persona | Área de interesse |
|---|---|
| P1 | web / sites |
| P2 | plataforma B2B / dados |
| P3 | hardware, firmware, embarcado |
| P4 | infra / CLI / tooling |

## Por que a v3 existe

Até a gen 3, todo candidato só foi exercitado na bancada que ele mesmo
construiu — circuito fechado. Quem cria a tarefa, planta o bug e escreve o
agente que sabe aquele bug não prova generalidade nenhuma; prova que o
candidato decora o próprio teste. A v3 mede **generalidade**: o harness
funciona apontado para código que não é dele?

## Por que o juiz criar a própria bancada NÃO reintroduz o circuito fechado

O circuito fechado da gen 1 era: autor do candidato = autor da bancada = autor
do critério de sucesso. Na v3, quem cria a bancada (o juiz, na Fase 1, às
cegas) é **independente** de quem construiu cada candidato — o juiz nunca viu
nenhum candidato quando decide o que é "consertado". E a **mesma bancada
julga os 5 candidatos cegos**, então a comparação entre eles é limpa: nenhum
candidato teve vantagem de ter visto o teste antes. Circuito fechado exigiria
que o autor do candidato influenciasse a bancada — isso não acontece aqui.

## Protocolo obrigatório — duas fases separadas por mecanismo

As fases são separadas por **mecanismo de runner** (sessões/prompts
distintos), não por instrução — pedido no prompt não é mecanismo (lição da
gen 2).

### FASE 1 — bancada às cegas

O juiz **não recebe nem vê nenhum candidato**. Trabalha num espaço próprio,
fora do diretório da geração — cegueira por mecanismo, não por pedido. Cria em
`bench/` um projeto de teste real do domínio dele:

- código que **roda**, com **pelo menos um defeito verdadeiro** (não um bug
  decorativo — algo que de fato quebra um comportamento esperado)
- um **critério objetivo e automatizável** de "consertado" (comando que dá
  exit code diferente antes/depois, ou saída comparável)
- **nada de dica plantada em comentário** (patologia 2 da gen 1: stub offline
  virou o próprio gabarito, ou comentário `# BUG: should be X` denunciava a
  resposta)

Escreve `BANCADA.md` com:

1. o que é o projeto
2. por que é representativo do domínio da persona
3. o que ela espera que um harness competente consiga fazer ali
4. o critério de sucesso declarado — **antes de ver qualquer candidato**

### FASE 2 — só então recebe os candidatos

O juiz recebe os candidatos, já anonimizados, em `cases/A..E` dentro do próprio
espaço. Nunca recebe caminho do diretório da geração nem o mapa de
anonimização. Para **cada** candidato:

1. cópia limpa da bancada + cópia limpa do candidato
2. aponta o harness (candidato) para a bancada
3. roda de ponta a ponta
4. **executa antes de ler** (herdado da v2)
5. registra execução crua: comando, saída real resumida, exit code, tempo

A **mesma bancada** julga os 5 — é isso que garante comparabilidade entre
candidatos.

## Parte determinística (código mede, tem VETO) — 30 pts

Tabela intacta, herdada da v2:

| Critério | Pts | Como se mede |
|---|---|---|
| **`run.sh` executa e demonstra trabalho** | 10 | Roda do estado entregue, mostra vermelho → ação → veredito → gate. "OK" e sair vale 0 |
| **Idempotente** | 4 | Segunda execução dá o mesmo resultado e não corrompe fixture |
| **Rejeição gravada no artefato** | 6 | Existe log de proposta genuinamente pior sendo **rejeitada**, com rollback. Só aceites = 0 |
| **Fixture entregue quebrada** | 4 | O estado inicial é vermelho de verdade; verde exige trabalho do agente |
| **Trace com custo real** | 3 | `cost_usd` e tokens preenchidos por chamador, não schema zerado |
| **Eficiência** | 3 | Custo em USD e turnos até o estado final, relativo ao que entregou |

**VETO (zera a nota):** escreveu fora do próprio diretório · alegou verde sem
ter executado · **qualquer artefato publicado** (README, docstring, NOTES,
BANCADA.md) contradiz o comportamento real · citou evidência que o log não
sustenta · nenhum arquivo produzido.

Não terminar **não** é veto e não penaliza. Parar antes do prazo é desperdício
e aparece em Eficiência.

## Parte da banca — 70 pts

**MESMA lista para as 4 personas**, independentemente da bancada usada por
cada uma — é isso que torna as notas comparáveis entre P1..P4.

| Critério | Pts | O que o juiz faz DEPOIS de rodar |
|---|---|---|
| **Generalidade / transplante** | 25 | Funcionou apontado para código de terceiro, fora da bancada do próprio autor do candidato? Ou só roda na bancada que ele mesmo construiu? |
| **Trabalho útil real produzido na bancada** | 20 | O defeito declarado na Fase 1 foi de fato consertado, sob o critério de sucesso declarado **antes** de ver o candidato — não um critério inventado depois para justificar nota |
| **Verificação confiável em código que não é do autor** | 15 | O gate reprova quando deve? Tenta fazer o verde mentir: reverte a fixture, quebra o teste, apresenta uma correção falsa. Aceita falso positivo? |
| **Custo e eficiência observados na execução** | 10 | Custo em USD, turnos, tempo de relógio — relativo ao que entregou na bancada de terceiro |

Nota sem **citação do artefato** e sem **registro de execução** é descartada
(herdado da v2).

**Consolidação: mediana por critério entre as 4 personas** (igual v2).
Variância alta marca o critério como ambíguo e vira revisão da régua na
geração seguinte.

## Ficha do juiz — schema JSON obrigatório

Cada juiz entrega uma ficha por candidato, no mínimo:

```json
{
  "persona": "p1",
  "candidato": "A",
  "execucao": {
    "comando": "string — comando exato rodado",
    "saida_resumida": "string — trecho relevante da saida real",
    "exit_code": 0,
    "segundos": 12.3
  },
  "notas": {
    "generalidade_transplante": 0,
    "trabalho_util_real": 0,
    "verificacao_confiavel": 0,
    "custo_eficiencia": 0
  },
  "citacao_evidencia": {
    "generalidade_transplante": "string — trecho de log/arquivo que sustenta a nota",
    "trabalho_util_real": "string",
    "verificacao_confiavel": "string",
    "custo_eficiencia": "string"
  },
  "veredito_veto": {
    "aplicado": false,
    "motivo": "string ou null"
  }
}
```

## Registro obrigatório por geração

- ficha por persona e por candidato (schema acima)
- `BANCADA.md` por persona, escrito na Fase 1, antes de qualquer candidato
- mediana por critério, veto aplicado e motivo
- dispersão das notas finais — se as variantes empatam, a sonda não
  discrimina e prazo/dificuldade sobem
- defeitos do próprio processo de julgamento (a régua também está sob
  avaliação)

## Mudanças da v2 → v3

1. Personas deixam de auditar o mesmo artefato com papéis fixos e passam a
   usar o candidato como ferramenta em um domínio próprio.
2. Bancada de teste não é mais dada nem plantada pelo autor do candidato: o
   próprio juiz cria, às cegas, antes de ver qualquer candidato — Fase 1 e
   Fase 2 separadas por mecanismo (sessões distintas), não por instrução.
3. Critério "utilidade real" (10 pts, v2) vira dois critérios mais duros:
   generalidade/transplante (25) e trabalho útil real produzido (20) —
   estava subponderado dado que é o buraco que nenhuma geração fechou.
4. "Solidez arquitetural" e "invariantes/segurança" (25 pts somados na v2)
   saem da lista fixa — não fazem sentido fora de um projeto real; entram
   implicitamente em "verificação confiável", que agora é a persona
   avaliando se o candidato aguenta o próprio código de terceiro dela.
5. Schema de ficha JSON explicitado (v2 só descrevia campos em prosa).
