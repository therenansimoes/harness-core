Você recebeu acesso a um repositório Python (biblioteca de parsing/validação
de dados bancários). Um cliente reportou o seguinte:

> "Algumas contas alemãs com dígito verificador calculado pelo método 11
> estão sendo rejeitadas como inválidas, mesmo sendo contas reais que o
> banco aceita. Um exemplo que reproduz: conta `1000000060`, sort code
> (Bankleitzahl) que usa o método de checksum `11`."

Investigue a causa raiz do problema nesta base de código e corrija. A
correção deve ser mínima e específica ao defeito relatado — não refatore
código não relacionado.

Ao terminar, garanta que o ambiente Python tem as dependências do projeto
instaladas (o projeto usa `pyproject.toml`; instale em modo editável com o
extra `pydantic`, mais `pytest`) e que a suíte de testes do projeto roda
localmente sem falhas antes de finalizar.

Este diretório é uma cópia sem histórico de git (só o código no commit
atual). Se a instalação em modo editável reclamar de não conseguir
determinar a versão do pacote via VCS, defina
`SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0` no ambiente antes de instalar.
