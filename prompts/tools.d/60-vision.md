<!--
Fragmento do manual das tools: olhar a tela. Some do prompt quando a tool de
visão não está montada — descrever tool que não existe gasta turno do modelo
tentando chamá-la.
-->

## view_render

Tira um screenshot da página e devolve o que aparece NA TELA.

- Argumentos: `port` (int) OU `dist_path` (string) — exatamente um dos dois; e
  `question` (string, opcional) para focar o olhar.
- Exemplos: `view_render(port=54231)` · `view_render(dist_path="dist",
  question="o menu está alinhado com o título?")`
- **200 não é prova de tela viva.** Um `<link rel="stylesheet">` apontando para
  caminho morto responde 200 na página e chega crua no navegador: o HTML do
  `read_file` está lindo e a tela está branca. Só o screenshot separa os dois.
- `port` só funciona para servidor que ESTA run subiu com `start_server` (a
  mesma cerca do `local_probe`). Porta de fora do workspace é recusada.
- `dist_path` serve o diretório em loopback numa porta efêmera: use depois do
  build, sem precisar de `start_server`.
- Se a resposta disser `tela provavelmente vazia`, o PNG saiu abaixo de 20kb:
  nada pintou. Conserte o carregamento (asset, rota, erro de JS) antes de mexer
  em estilo — ninguém julga aparência de tela branca.
- A resposta traz nota 0-10 e bullets com problemas concretos quando há modelo de
  visão na máquina. Se vier `visão indisponível`, o screenshot foi tirado e
  renderizou algo, mas ninguém descreveu o conteúdo: não invente que está bonito,
  verifique o que der por outros meios.
- Cada chamada grava um PNG novo em `.harness/shots/`. Olhe DEPOIS de mudar o
  CSS, não antes: o valor da tool é o antes/depois da sua própria mudança.
