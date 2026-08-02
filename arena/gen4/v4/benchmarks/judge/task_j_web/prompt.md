Você recebeu acesso a um repositório JavaScript/TypeScript (gerenciador de
estado atômico para React/Preact/Vue/Svelte). Um usuário reportou o
seguinte:

> "Tenho um componente que assina só algumas chaves específicas de uma
> store via `listenKeys`. Quando outra parte do código substitui a store
> inteira com `store.set(novoObjeto)`, meu listener dispara mesmo que
> nenhuma das chaves que eu observo tenha mudado de valor. Isso está
> causando re-renders desnecessários."

Investigue a causa raiz do problema nesta base de código e corrija. A
correção deve ser mínima e específica ao defeito relatado — não refatore
código não relacionado.

O ambiente já está pronto (node_modules/ instalado, não rode `pnpm
install`, não instale nada). Rode a suíte de testes com:

    node_modules/.bin/bnt

Garanta que ela passa localmente sem falhas antes de finalizar.

Este diretório é uma cópia sem histórico de git (só o código no commit
atual).
