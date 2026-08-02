# Fast Start — Acelerar a implementação do Harness

**Companion de:** `PLAN.md`  
**Objetivo:** sair do plano e chegar na **primeira linha verde** no `results.tsv` o mais rápido possível.

---

## 1. Princípio de velocidade

> O caminho mais rápido é um **loop chato e verde**, não um plano mais inteligente.

Não desenhar mais arquitetura.  
Não plugar satélites.  
Não portar o harness antigo.

**Ship the minimum loop.**

---

## 2. O que entregar primeiro (maior alavancagem)

Arquivos mínimos para colar e rodar:

| Arquivo | Função |
|---------|--------|
| `agent.py` | bash + system prompt + limite de turns |
| `run_task.py` | workspace → agent → verify → `results.tsv` |
| `tasks/task_01/prompt.md` | o que pedir ao agent |
| `tasks/task_01/verify.py` | exit 0 = pass, ≠0 = fail |
| `results.tsv` | header + append de cada run |
| `harness_version.txt` | `v0` |

Isso transforma “plano” em “primeira linha no TSV” em horas, não semanas.

---

## 3. Roubar estrutura, não sistemas

Copiar **padrões**, não produtos inteiros:

| Fonte | O que pegar | O que ignorar |
|-------|-------------|---------------|
| **AutoAgent** | Split directive / agent / tasks + hill-climb em score | Meta-agent overnight completo |
| **Harbor-style tasks** | `prompt` + verificador determinístico + score numérico | Suite enorme de benchmark |
| **AHE (ideia)** | Logar o que mudou + o que falhou + a decisão | Stack inteira de observability |

Uma tarde lendo layouts deles > duas semanas inventando estrutura de pastas.

---

## 4. Trilhas em paralelo (para não travar)

| Trilha | Quem | Output |
|--------|------|--------|
| **A — Código** | você + skeleton | `agent` + `run_task` + 1 task verde |
| **B — Tasks** | você | mais 2 tasks com `verify.py` estrito |
| **C — Baseline** | depois de A+B | 3× runs cada → números do v0 no TSV |
| **D — Produto** | ignorar agora | CLI, `.harness/`, marketplace |

Não comece C antes de A estar verde.  
Não pense em D até C existir.

---

## 5. Atalhos que ainda mantêm o core limpo

| Atalho | Por quê é ok no dia 1 |
|--------|------------------------|
| **Sem Docker** | pasta temp isolada basta; Docker entra no A/B sério |
| **Um modelo só** | hardcode a API que você já paga; multi-model é satélite |
| **Só tool bash** | força trabalho por artefato; mais tools via evolução depois |
| **TSV, não DB** | abre no Excel, git-friendly, zero infra |
| **Humano propõe o 1º A/B** | automatizar o proposer é etapa 4; score rules já valem na mão |

---

## 6. O que NÃO fazer se quiser velocidade

- Integrar OpenClaw / browser-use / Mem0 / vault agora  
- Construir CLI ou `harness init` antes de 1 task verde  
- Desenhar a árvore completa de `evolution/` antes do primeiro A/B  
- Portar o harness antigo (começar limpo é mais rápido que desembaraçar)  

Cada um desses é desvio de vários dias.

---

## 7. Como pedir ajuda (sob demanda)

Escolha **um**:

1. **Skeleton completo de código** — caminho mais rápido para “roda”  
2. **Spec do `harness init`** — o que vai em `.harness/` (multi-projeto)  
3. **Playbook do primeiro A/B** — o que mudar no `agent.py` e como comparar  
4. **Pack de templates de task** — 3 padrões de verify (arquivo existe, output de script, testes passam)  

---

## 8. Janela recomendada — próximas 48 horas

```
Hora 0–2     Criar harness-core, colocar PLAN.md + este FAST_START.md
Hora 2–6     Skeleton rodando, task_01 verde no results.tsv
Hora 6–12    task_02 + task_03
Hora 12–24   3× cada task → baseline v0
Próxima sessão  Um A/B manual → v0.1 keep ou discard
```

---

## 9. Checklist do “fast path”

- [ ] Pasta/repo `harness-core` limpo (sem legado no path)  
- [ ] `PLAN.md` + `FAST_START.md` no root  
- [ ] `agent.py` mínimo  
- [ ] `run_task.py`  
- [ ] `tasks/task_01/` com `prompt.md` + `verify.py`  
- [ ] `harness_version.txt` = `v0`  
- [ ] `results.tsv` com header  
- [ ] Uma run com `success=1`  
- [ ] Três tasks × três runs = baseline  
- [ ] Primeiro A/B documentado no TSV  

---

## 10. Norte

Score no centro.  
Core primeiro.  
Uma task verde vale mais que dez páginas de arquitetura.

Quando a primeira linha do `results.tsv` existir, o harness deixou de ser ideia e passou a ser sistema.
