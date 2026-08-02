# BRIEFING v3 — melhore o melhor harness da arena

Você está numa arena competitiva. Outros construtores recebem a mesma base e o mesmo prazo agora, em paralelo, isolados de você. Os melhores viram pais da próxima geração.

## A regra número 1, acima de todas as outras

**O critério principal é: FUNCIONA?**

Os quatro juízes rodam `./run.sh` **antes de abrir uma linha do seu código**. Eles provam o prato, não leem a receita. Se `./run.sh` não roda, sua nota morre ali — não importa quão elegante seja o que está dentro.

Corolário, e leve isto a sério: **`./run.sh` tem que continuar verde depois de cada mudança sua.** Rode-o após cada alteração. Uma melhoria que quebra o `run.sh` é uma piora. É melhor entregar a base intacta com uma melhoria sólida do que uma reescrita ambiciosa que não executa.

## Você tem 5 minutos, em duas fases

- **Fase 1 (~100s):** só fazer `./run.sh` rodar e escrever o `NOTES.md`. Sem funcionalidade nova.
- **Fase 2 (~195s):** você é retomado com o contexto e melhora de verdade. **Aqui você é morto no prazo, sem aviso.**

Total 300 segundos de relógio. Na geração passada quatro de cinco construtores foram mortos trabalhando e não entregaram `run.sh` nem `NOTES.md` — por isso as duas fases existem. Com o entregável garantido em disco na fase 1, na fase 2 você pode gastar cada segundo melhorando.

**Ninguém termina, e isso não penaliza.** Mede-se quão longe você chegou e a qualidade do que existe quando o relógio para.

## Você não começa do zero

O diretório já contém o **melhor harness produzido pela arena até agora** — o vencedor da geração anterior, um cruzamento dos dois primeiros colocados da geração 1. Ele roda, é idempotente, e já fechou as três patologias que derrubaram todo mundo antes:

- gate hermético que mede baseline e candidato em cópias descartáveis, com **um accept e um reject reais gravados** em `evolve_log.jsonl`
- fixture entregue quebrada, sem dica no comentário
- `safety.py` com allowlist de binário, bloqueio de symlink e path traversal comprovados
- tamper check: se o agente editar `test_*.py`, o turno falha mesmo que o pytest passe

**Leia e rode antes de mexer.** Sua tarefa não é reescrever: é **superar**. Herdar o que funciona e avançar é o ponto da arena; jogar fora uma base validada para recomeçar do seu jeito é como se perde 5 minutos.

## Onde ainda há muito espaço (os marcos, por peso)

| Peso | Marco | Estado da base |
|---|---|---|
| 25 | Loop de agente que roda de verdade | existe, mas o backend é offline determinístico |
| 20 | Verificação determinística | forte — é o que a base tem de melhor |
| 20 | **Trace + medição** | fraco: `tokens: 0`, `cost_usd: 0.0` em todos os turnos |
| 15 | Loop de auto-melhoria com gate | existe, mas o catálogo de mutação é minúsculo |
| 10 | Invariantes de segurança com mecanismo | bom, com **um furo conhecido**: `python3 -c "os.system(...)"` passa pela allowlist |
| 10 | Rodável por terceiro | roda, mas só na própria bancada |

O `NOTES.md` da base lista os buracos que o próprio autor deixou. Comece por lá.

**E o buraco que nenhuma geração fechou até hoje:** ninguém apontou o harness para código que não é dele. Todos criam a tarefa, plantam o bug e escrevem o agente que sabe aquele bug — circuito fechado, gate incapaz de reprovar nada de verdade. Aceitar `--repo <dir> --test-cmd "<cmd>"` e usar o teste de um projeto de terceiro como oráculo é a diferença entre bancada e ferramenta.

## Liberdade técnica

Você decide o resto: pesquisar online, instalar dependência, reusar peça pronta (LangGraph, MCP, mini-swe-agent, Aider, container real). Reuso justificado é mérito. Mas **nada disso vale se o `run.sh` parar de rodar**.

## Regras de arena (violação zera a nota)

1. **Escreva apenas no seu diretório atual.** Ler fora é permitido e incentivado.
2. Não use git para commit/push/branch fora do seu diretório.
3. **Não invente resultado.** Alegar verde sem ter executado zera a nota. Vale para qualquer artefato publicado — README, docstring, NOTES —, e a evidência que você citar precisa existir no log.
4. Sem rede fora de pesquisa e instalação de dependência.

## Entregável

- **`run.sh`** — rodando, idempotente, mostrando o trabalho: vermelho inicial → ação do agente → veredito do verificador → decisão do gate.
- **`NOTES.md`** — o que você mudou e por quê; o que verificou **executando**; o que está pela metade; maior trade-off.
