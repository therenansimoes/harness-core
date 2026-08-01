# Changelog da spec

## 1.0 — 2026-08-01

Spec inicial. Define a landing page da Oficina Volt (`site/index.html` +
`site/style.css`) e os requisitos permanentes de regression. Registra como
fora de escopo a página de preços pedida pela sessão `s001`.

## 1.1 — 2026-08-01

Critérios de UI deixaram de ser 100% humanos. Estrutura, CSS aplicado, links,
console, layout (screenshot vs baseline) e responsividade em 375px passaram a
ser verificados pela suite Playwright em `ui/`. A rubrica subjetiva
(hierarquia, contraste, "parece bom") continua humana, mas só é acionada em
caso ambíguo — o dono deixou de ser gargalo de toda entrega visual.
