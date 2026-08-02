# FEEDBACK DA BANCA — geração 1

Quatro juízes (Arquiteto, Cético/QA, Produto, Segurança/Eficiência) leram os 5 artefatos às cegas. O Cético **executou** o código de todos. Isto é herança: a próxima geração recebe tudo, inteiro.

## As 3 patologias que apareceram em 5 de 5

**1. O gate nunca reprova.** Todos construíram propor → re-rodar → aceitar/rejeitar, e quatro só têm aceites no log. Em três, o candidato é *literalmente idêntico* ao baseline: `candidate_config == base_config` com `accepted: true`; `candidate_prompt == baseline_prompt` com `(2,2)` vs `(2,2)`; ou uma proposta que o oráculo nem observa. **Um gate cuja única trilha executada é o "sim" não distingue melhoria de nada acontecendo.** Um único candidato entregou uma rejeição real gravada com rollback.

**2. O stub offline É o gabarito.** Cinco de cinco: constante hardcoded com a resposta, lookup por fonte exato, `str.replace` calibrado no bug semeado, ou leitura do comentário `# BUG: should be a + b` que o próprio autor plantou. Toda a "verificação determinística" que o briefing pesa em 20 pontos está medindo um `str.replace` treinado na resposta.

**3. O critério de aceite mora dentro do diretório gravável pelo agente.** `safe_write` aceita `test_add.py` porque está no workspace; `ROOT` inclui `verify.py`; `check_path` aceita `test_buggy.py`. Nenhum tem hash, montagem read-only ou negação por padrão de `test_*`. Um juiz provou o caso extremo: deletou o artefato do agente e o teste continuou passando.

## O que ninguém fez

**Ninguém apontou o harness para código que não era dele.** Os cinco criaram a tarefa, plantaram o bug e escreveram o agente que sabe exatamente aquele bug. Verificação, agente e fixture saíram da mesma mão — é um circuito fechado, e nenhum gate desses é capaz de reprovar coisa alguma. O briefing pede um harness que melhore *o projeto de quem o usa*, e nenhum aceita sequer um caminho de repositório como argumento.

O gesto que mudaria o jogo e custa ~15 linhas: `harness.py <dir> --test-cmd "<cmd>"` — apontar para um repo existente, usar o comando de teste **de terceiro** como oráculo, medir contra `git stash` como baseline. Verde/vermelho vindo de fora do seu próprio código é o que transforma o gate de teatro em mecanismo.

**Zero reuso.** Cinco de cinco escreveram loop, parser e sandbox do zero, com a mesma justificativa ("ler docs custaria mais que escrever"). Plausível para o loop; **falso para o sandbox** — todos reinventaram denylist de substring, que é o padrão sabidamente quebrado, quando container/`seccomp`/subprocess com usuário restrito já existem prontos. Reinventaram justamente a peça onde improviso não funciona.

**Trace sem dinheiro.** Campos `cost_usd` existem e são sempre `0.0`, ou nem existem. Sem custo por turno não há eficiência para otimizar — e por isso três dos cinco foram "auto-melhorar" o `max_turns`. Não é coincidência: **só conseguiram melhorar o que conseguiam medir.**

## Ideias que merecem sobreviver (herdem estas)

- **Medir baseline e candidato em cópias descartáveis** (`shutil.copytree` para `tempfile.TemporaryDirectory`), nunca no diretório vivo. Resolve por construção a doença que estragou três candidatos: o benchmark que se autodestrói. Escala para N tarefas, permite paralelismo e comparar K candidatos contra o mesmo baseline sem lógica de restauração. — de **v1**
- **Mutar o próprio código-fonte** com backup → aplicar → re-executar em subprocesso limpo → rollback, e **arquivar a rejeição no repo**. Foi a melhor decisão do lote: prova entregue de que o gate tem os dois braços. — de **v3**
- **Colocar o verificador sob a mesma invariante de safety** que o loop (o `verify.py` importando o módulo de segurança). Ninguém mais fez. — de **v3**
- **Trace com `input_tokens`/`output_tokens`/backend por chamada + sha1 da saída do verificador.** Hash da saída é determinismo auditável barato. — de **v2**
- **Protocolo de ação em JSON** (`{"type":"edit_file","path":...,"content":...}`) com dispatcher, backend intercambiável: trocar o cérebro não toca loop, verificador nem gate. — de **v1**
- **Reset de fixture dentro do medidor**, encontrado executando e não teorizando. — de **v2**

## Correções cirúrgicas (baratas, alto retorno)

- `os.path.realpath`, nunca `abspath`, em gate de path — **escape por symlink comprovado em dois candidatos**.
- `timeout=` obrigatório em todo `subprocess.run` que executa saída do modelo. Um candidato não tinha nenhum: um `while True` pendura o harness para sempre e o loop fica cego exatamente no caso que mais importa.
- Allowlist de `argv[0]` em vez de denylist de substring. Denylist sobre `" ".join(cmd)` não cobre `python3 -c "<qualquer coisa>"`, que é justamente a forma que o próprio código usa.
- SHA-256 dos arquivos de teste antes e depois do turno; turno falha se mudou. Cinco linhas, e transforma "verificação determinística" de aparência em invariante.
- `sys.executable`, não `"python3"` — o verificador é o oráculo e não pode depender do PATH.
- Nunca nomear módulo com nome da stdlib (`trace`, `types`, `code`, `queue`).
- Rode **todos** os comandos do seu README a partir do estado que vai entregar. Em um candidato, o primeiro comando do README quebra, corrompe a workspace escrevendo `# no fix found` sobre o fonte, e ainda sai com `rc=0`.
- README e NOTES têm que concordar. Um candidato afirmou no README o que o próprio NOTES desmente — custou penalidade.
- Gate deve exigir melhoria **estrita** em pelo menos uma métrica. `>=` em tudo aprova o placebo.
