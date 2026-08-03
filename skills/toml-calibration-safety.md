---
name = "toml-calibration-safety"
kinds = ["config"]
description = "Segurança ao calibrar TOML: delta pequeno, roundtrip-check, default congelado, fail-open/fail-closed"
---
## Calibração segura de TOML

- Delta pequeno por mutação: mude UMA chave por vez, passo limitado (ex.: ±10–20% de numérico, um item de lista). Mudança grande = várias mutações pequenas, cada uma medida.
- Roundtrip-check antes de escrever: parse do texto novo com o mesmo parser do runtime; falhou o parse ou o valor lido difere do pretendido => aborte a escrita, arquivo antigo fica intacto.
- Escreva atômico: texto novo em arquivo temporário no mesmo diretório + rename por cima. Nunca edite o TOML in-place linha a linha.
- Default congelado no código: todo valor lido do TOML tem fallback hardcoded no módulo que lê. O TOML ajusta, nunca é a única fonte — apagar o arquivo não pode quebrar o boot.
- Fail-open na leitura: TOML ausente/corrompido/chave faltando => log + defaults do código, o sistema continua rodando.
- Fail-closed na escrita: qualquer dúvida (validação, tipo, chave fora do range, arquivo imutável pelo genome) => NÃO escreve. Escrita perdida é recuperável; config corrompida propaga.
- Respeite o genome: só mute paths listados como mutáveis em `config/genome.toml`; caminho fora da lista é erro, não warning.
- Registre a mutação no ledger (chave, valor antigo, valor novo, motivo) antes de considerar aplicada — sem registro não há revert.
- Valide o efeito: após aplicar, releia a config pelo caminho normal do runtime e confirme o valor novo.
