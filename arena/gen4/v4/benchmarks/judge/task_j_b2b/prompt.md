Você recebeu acesso a um repositório Python (biblioteca de parsing/validação
de dados bancários). Um cliente reportou o seguinte:

> "Algumas contas alemãs com dígito verificador calculado pelo método 11
> estão sendo rejeitadas como inválidas, mesmo sendo contas reais que o
> banco aceita. Um exemplo que reproduz: conta `1000000060`, sort code
> (Bankleitzahl) que usa o método de checksum `11`."

Investigue a causa raiz do problema nesta base de código e corrija. A
correção deve ser mínima e específica ao defeito relatado — não refatore
código não relacionado.

O ambiente Python já está pronto (não crie venv, não instale nada). Rode a
suíte de testes do projeto com:

    PYTHONPATH=. /Users/renansimoes/projects/harness-core/judges/_env/j_b2b/bin/python -m pytest

Garanta que ela passa localmente sem falhas antes de finalizar.

Este diretório é uma cópia sem histórico de git (só o código no commit
atual).
