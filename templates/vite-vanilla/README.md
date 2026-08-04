# vite-vanilla

```sh
npm install && npm test    # vitest, roda em Node
npm run dev                # HMR; npm run build gera dist/
```

Sem framework de propósito. `src/main.js` separa função pura (`shellMarkup`) de
efeito no DOM (`boot`) — é o que deixa o teste rodar em Node sem jsdom.

`src/tokens.css` é a fonte de verdade de cor/espaço/tipografia (mude `--hue` e
a identidade muda); `src/style.css` importa tokens e reset e guarda só o que é
desta tela. Tema escuro segue o sistema; `<html data-theme="dark|light">` força.
