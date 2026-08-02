# channel/whatsapp

Serviço Node isolado, single-session, que faz a ponte HTTP entre um processo
Python e o WhatsApp via Baileys. Não decide nada sozinho: só entrega mensagens
recebidas no inbox (`WA_INBOX`) e envia o que `/send` mandar.

Portado do conector Baileys do CRM (`02-crm/connectors/connector-whatsapp`),
reduzido ao mínimo: sem Postgres, sem multi-tenant, sem rate limiter, sem
templates/mídia, sem sync de labels do WhatsApp Business.

## Instalar

```bash
cd channel/whatsapp
npm install
```

## Configurar (variáveis de ambiente)

| Variável | Default | Descrição |
|---|---|---|
| `WA_PORT` | `8787` | Porta HTTP (sempre em `127.0.0.1`, nunca exposta na rede) |
| `WA_ALLOWLIST` | vazio | JIDs separados por vírgula, ex. `5511999999999@s.whatsapp.net` |
| `WA_AUTH_DIR` | `./auth` | Diretório de persistência de sessão (`useMultiFileAuthState`) |
| `WA_INBOX` | `./inbox.jsonl` | Arquivo JSONL append-only com mensagens recebidas |

Se `WA_ALLOWLIST` estiver vazia o serviço sobe mas recusa todo `/send` (403) e
não grava nada no inbox — falha fechado, nunca aberto.

## Rodar e autenticar (QR)

```bash
WA_ALLOWLIST="5511999999999@s.whatsapp.net" npm start
```

Na primeira execução, sem sessão salva em `WA_AUTH_DIR`, o Baileys gera um QR.
Ele não é impresso no terminal por padrão (`printQRInTerminal: false`); leia o
campo `qr` de `GET /status` e renderize/escaneie a partir dele (ex. com
`qrcode-terminal` ou qualquer lib de QR do lado que for consumir).

Depois de escanear, a sessão fica salva em `WA_AUTH_DIR/` — reinicie o
processo e ele reconecta sozinho sem pedir QR de novo.

## Contrato HTTP

Escuta **exclusivamente em `127.0.0.1`**.

- `GET /status` → `200 {"connected": bool, "jid": string|null, "qr": string|null, "last_error": string|null}`
- `POST /send` body `{"to": "<jid>", "body": "<texto>"}`:
  - `200 {"message_id": "..."}` sucesso
  - `400 {"error": "..."}` payload inválido
  - `403 {"error": "destino fora da allowlist"}` destino não permitido
  - `503 {"error": "..."}` não conectado

## Inbound

Cada mensagem de texto recebida de um JID na allowlist (e que não seja grupo)
vira uma linha em `WA_INBOX`:

```json
{"ts": "2026-08-01T12:00:00.000Z", "from": "5511999999999@s.whatsapp.net", "body": "oi", "is_group": false, "message_id": "ABC123"}
```

Mensagens de grupos (`@g.us`) e de remetentes fora da allowlist são
descartadas silenciosamente (não gravam nada).

## Se a sessão cair

O serviço reconecta sozinho com backoff exponencial (2s → 120s, até 10
tentativas). Se o Baileys esgotar essas tentativas e ficar "preso"
desconectado, um watchdog interno tenta reconectar de novo a cada 1-60min
(backoff próprio) enquanto houver uma sessão válida em `WA_AUTH_DIR`.

Se a sessão for invalidada de verdade (logout, código 405 fora de pareamento),
o serviço limpa `WA_AUTH_DIR` automaticamente e para de tentar — `GET /status`
mostra `connected: false` e `last_error` com o motivo. Rode o processo de novo
para gerar um QR novo e reparear.

## npm install pesado/falhando neste ambiente

Se `npm install` travar ou falhar por rede/sandbox, o comando exato a rodar
manualmente num ambiente com acesso à rede é:

```bash
cd channel/whatsapp && npm install
```

O gate de segurança do lado Python (allowlist, fail-closed) é testável via
mock HTTP e não depende de uma sessão Baileys real nem de `npm install`
funcionar neste ambiente.
