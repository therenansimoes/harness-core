+++
id = "dummy_discard"
from_version = "v0.2"
to_version = "v0.2-dummy"

hypothesis = "Inflar o SYSTEM_PROMPT com instruções redundantes NÃO melhora nada e deve ser reprovado pelos gates — esta proposta existe para exercitar o caminho DISCARD do evolve.py de ponta a ponta."

[change]
file = "agent.py"
old = '''- Termine com: DONE: <resumo em uma frase>.
'''
new = '''- Termine com: DONE: <resumo em uma frase>.

Observações adicionais importantes que você deve considerar cuidadosamente antes
de agir: lembre-se de que a qualidade do trabalho importa muito, e que é sempre
recomendável pensar com bastante calma sobre cada passo antes de executá-lo.
Considere também que existem múltiplas formas de resolver qualquer problema, e
que vale a pena refletir sobre as alternativas disponíveis. Tenha em mente que o
diretório de trabalho é isolado e que os arquivos criados ali são o resultado
final do seu trabalho. Não se esqueça de que a tarefa deve ser concluída de
forma completa e cuidadosa, sem deixar pontas soltas para trás.
'''

[expected_gates]
must_improve = []
must_not_worsen = ["success", "trunc_rate"]
+++

# Proposta dummy — bloat de prompt (deve DAR DISCARD)

## Por que

Esta proposta **não é uma tentativa de melhoria**. Ela existe para provar que o
caminho de DISCARD do `evolve.py` funciona sem depender de sorte estatística.

Um no-op puro (ex.: adicionar um comentário ao `agent.py`) seria o teste mais
limpo conceitualmente, mas o veredito dependeria de ruído: 6 runs de um genome
idêntico podem, por acaso, sair 10% mais baratas e disparar um MERGE espúrio.
Um bloat deliberado de prompt move o custo na direção **errada** de forma
previsível, então o DISCARD é determinístico.

## Predição

- custo/run e tokens/run **sobem** (o prompt inflado é reenviado a cada turn)
- success e truncamento ficam flat (o texto é inofensivo, só caro)
- gate `ganho normalizado >=10%` **reprova** → DISCARD, exit 1
- `agent.py` e `harness_version.txt` continuam em v0.2

## Falsificação

Se isto der MERGE, os gates estão frouxos — custo subindo não pode ser aprovado.
Se der erro de infra (exit 2), o problema está no `evolve.py`, não na proposta.

---

```bash
python3 evolve.py --proposal evolution/proposals/dummy_discard.md --repeat 2
```
