import "./style.css";

/** Markup do shell da página.
 *
 * É função PURA de propósito: devolve string, não toca no documento. É isso que
 * deixa o teste rodar em Node, sem jsdom, sem browser — o `boot` abaixo é a
 * única parte que precisa de DOM, e ela não tem lógica para testar.
 */
export function shellMarkup({ title, lead }) {
  return `
    <h1>${escapeHtml(title)}</h1>
    <p class="lead">${escapeHtml(lead)}</p>
  `;
}

/** Escapa texto antes de entrar em `innerHTML`. Interpolar dado não escapado em
 *  template string é o caminho mais curto para XSS num app vanilla. */
export function escapeHtml(texto) {
  return String(texto).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}

export function boot(raiz) {
  if (!raiz) return false;
  raiz.innerHTML = shellMarkup({
    title: "Uma frase que diz o que este app faz",
    lead: "Placeholder honesto: este texto existe para você julgar ritmo e medida de leitura antes de escrever o conteúdo real.",
  });
  return true;
}

// Guarda de ambiente: o teste importa este módulo em Node, onde `document` não
// existe. Sem o `if`, o import quebra antes do primeiro assert.
if (typeof document !== "undefined") {
  boot(document.querySelector("#app"));
}
