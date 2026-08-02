# BANCADA — P3 (hardware / firmware / embarcado)

## O que é o projeto

Uma biblioteca C de **ring buffer (buffer circular)** de 8 bytes de capacidade
— o tipo de componente que aparece em praticamente todo driver de UART/SPI/I2C
em firmware embarcado, para desacoplar a ISR de recepção do loop principal
que consome os bytes.

Arquivos:
- `ringbuffer.h` — struct e assinaturas (`rb_init`, `rb_push`, `rb_pop`,
  `rb_is_full`, `rb_is_empty`)
- `ringbuffer.c` — implementação
- `test_ringbuffer.c` — suíte de teste host-based (roda em qualquer máquina
  com `cc`, sem hardware — prática padrão em firmware para testar lógica
  agnóstica de hardware)
- `Makefile` — alvo `test` compila e executa a suíte

## Por que é representativo do domínio

Ring buffers com contagem de elementos (`head`/`tail`/`count`) são a peça
mais comum e mais propensa a bug sutil de off-by-one em firmware: a
diferença entre `count > CAPACITY` e `count >= CAPACITY` na checagem de
"cheio" decide se um push além da capacidade é rejeitado com segurança ou se
sobrescreve silenciosamente um byte ainda não lido — corrupção de dados que,
em campo, se manifesta como pacotes de sensor embaralhados ou comandos
perdidos, e é notoriamente difícil de reproduzir porque só aparece sob
rajada (burst) de dados mais rápida que o consumidor.

## O que se espera de um harness competente

- Rodar `make test` e observar a falha real (não um erro de compilação, não
  crash — assertions falhando com mensagem clara).
- Diagnosticar que a causa raiz está em `rb_push()`, especificamente na
  condição de guarda contra buffer cheio.
- Corrigir a condição para que o push além da capacidade seja **rejeitado**
  (retorno `-1`), sem escrever no buffer nem corromper dados já presentes.
- Rodar `make test` de novo e confirmar todos os testes passando, sem
  quebrar nenhum teste que já passava (os 8 pushes dentro da capacidade e a
  detecção de "cheio" já funcionam corretamente hoje).
- Não há dica em comentário — a linha buggy (`if (rb->count > RB_CAPACITY)`)
  não está marcada; o harness precisa ler a lógica e comparar contra o
  contrato declarado nos comentários de `ringbuffer.h`/testes.

## Critério de sucesso (declarado antes de ver qualquer candidato)

```
make clean && make test
```

- **Estado atual (vermelho):** exit code `2`, saída contém
  `FAIL: push beyond capacity is rejected` e
  `FAIL: popped bytes match original FIFO order, uncorrupted`.
- **Consertado (verde):** exit code `0`, saída termina em
  `ALL TESTS PASSED`, e nenhum teste que antes passava passa a falhar.

Automatizável 1:1 via exit code do `make test` — sem necessidade de
julgamento humano.
