Selado = imutável pro loop de auto-melhoria: só humano adiciona/edita unidades aqui.
A nota do exame não vale se a prova muda — mutação que toca este dir é tamper.
Unidade = subdir com `unit.toml` (id/prompt/verify_cmd); `exam.run_sealed_exam` roda todas e só aprova se todas aceitam.
`requires_real_backend = true` no `unit.toml` = unidade que o mock nunca resolve: fica FORA do exame mock (default, $0) e só entra quando `[exam]` de `config/ruler.toml` aponta backend real.
