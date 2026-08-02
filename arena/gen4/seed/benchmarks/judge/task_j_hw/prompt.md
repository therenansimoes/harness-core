Você recebeu acesso a um repositório C (parser JSON minimalista, sem
alocação dinâmica, pensado pra embarcados). Um usuário reportou o seguinte:

> "Compilei a lib com `JSMN_PARENT_LINKS` habilitado (preciso navegar pra
> cima na árvore de tokens). Reparei que um JSON malformado — um `}` que
> fecha sem nenhum `{` correspondente no nível raiz — é aceito como válido
> em vez de retornar erro de parse. Sem `JSMN_PARENT_LINKS` o mesmo JSON é
> corretamente rejeitado."

Investigue a causa raiz do problema nesta base de código e corrija. A
correção deve ser mínima e específica ao defeito relatado — não refatore
código não relacionado.

O ambiente já tem `cc` e `make` disponíveis, não precisa instalar nada.
Rode:

    make test_links

Garanta que compila e passa (PASSED sem FAILED) localmente antes de
finalizar.

Este diretório é uma cópia sem histórico de git (só o código no commit
atual).
