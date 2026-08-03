---
name = "config-calibration"
kinds = ["config"]
description = "Calibração de TOML com deltas pequenos e validação"
---
## Calibração de config TOML

- Um parâmetro por vez, delta pequeno (10–20% do valor atual), nunca uma ordem de grandeza.
- Edite valores, não estrutura: preserve chaves, comentários e formato existentes.
- Após editar, confirme que o arquivo ainda parseia (tomllib) e rode o verify da unit antes de declarar pronto.
