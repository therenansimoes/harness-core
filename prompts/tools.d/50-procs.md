<!--
Fragmento do manual das tools: processos de vida longa. Some do prompt quando as
tools de processo não estão montadas — descrever tool que não existe gasta turno
do modelo tentando chamá-la.
-->

## start_server

Sobe um servidor em background (dev server, API, `python -m http.server`) e
espera a porta responder.

- Argumentos: `command` (string, obrigatório), `wait_path` (string, opcional;
  default `/` — a rota que a sonda de readiness chama), `timeout` (int,
  opcional; default 30 segundos).
- Exemplo: `start_server(command="npm run dev")`
- **Não use `execute` para servidor.** `execute` é síncrono e com timeout curto:
  `npm run dev` nele pendura até o timeout e queima o orçamento do run sem
  produzir nada.
- A PORTA é escolhida pelo harness e chega ao comando como `$PORT` no ambiente.
  Não fixe 3000/8000: outro run pode estar nela, e você leria a resposta do
  servidor dele. Se o comando ignora `$PORT`, passe a porta explicitamente na
  linha (ex.: `uvicorn app:app --port $PORT`).
- A saída de sucesso traz `id=<id> porta=<n> log=<path>`. Guarde os dois: a
  porta é o argumento do `local_probe`, o id é o do `stop_server`.
- Se o processo morrer no boot, a resposta já vem com as últimas linhas do log —
  leia o erro ali em vez de tentar subir de novo igual.
- Se voltar `não respondeu em Ns`, o processo está VIVO mas mudo: veja o log com
  `read_file` no path que a saída deu antes de concluir qualquer coisa.
- Servidores que sobrarem morrem no fim do run; você não precisa limpar.

## local_probe

Faz uma requisição HTTP a um servidor que ESTA run subiu.

- Argumentos: `port` (int, obrigatório — a porta que o `start_server` devolveu),
  `path` (string, opcional; default `/`), `method` (string, opcional; default
  `GET`).
- Exemplo: `local_probe(port=54231, path="/api/health")`
- É a tool para PROVAR que a página/rota responde. Build verde não é prova de
  tela viva; 200 na rota é.
- Só `127.0.0.1` e só porta registrada por `start_server` nesta run. Porta não
  registrada volta `recusado` — não é bug e não há como contornar: suba o
  servidor pela tool.
- Cerca oposta à do `web_fetch`: lá o loopback é proibido, aqui o loopback é o
  único endereço permitido. Uma tool não substitui a outra.
- Redirect não é seguido: um `302` volta como `302`, com o corpo que veio.
- A resposta é cortada em 20000 bytes.

## stop_server

Mata um servidor subido por `start_server`, junto com os processos filhos dele.

- Argumentos: `id` (string, obrigatório — o id que o `start_server` devolveu).
- Exemplo: `stop_server(id="a1b2c3d4")`
- Use quando precisar RESUBIR o servidor depois de editar config/dependência.
  Para simples fim de tarefa não precisa: o run limpa sozinho.
