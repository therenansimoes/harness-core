---
name = "dream-code-traceback-most-recent"
kinds = ["code"]
description = "orientação destilada: falha recorrente traceback most recent em unidades code"
---

Consolidado da memória episódica: 15 falhas de unidades kind=code com a mesma assinatura de trace.

## Assinatura recorrente

`traceback most recent call last file stdin line`

Unidades afetadas: u5_grafico_svg, u4_dark_mode, u6_sobre_e_validacao.

## Trecho representativo

```
Traceback (most recent call last): File "<stdin>", line 14, in <module> AssertionError: projetos.html sem o botao de tema id="tema"
```

## O que fazer

- Antes de declarar pronta uma unidade kind=code, verifique explicitamente a condição que produz a falha acima — ela já reincidiu 15 vezes.
- Se o trecho aponta um comando de verificação, rode-o e leia a saída real; não presuma o resultado.
- Se a causa for outra desta vez, diga qual: assinatura repetida com causa diferente é sinal de que esta orientação está errada.

> Candidata do sono, não conhecimento validado: a evidência é a contagem de episódios, e o lift ainda vai ser medido pelo ciclo.
