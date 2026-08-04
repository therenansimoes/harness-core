// Vazio de propósito: a página inteira funciona sem JavaScript, e começar com
// um arquivo vazio é o que mantém isso verdade. Carregado como `type="module"`,
// então já é deferido e tem escopo próprio — nada de IIFE.
//
// Receita do único comportamento que este template quase precisa (troca de
// tema), para quando você adicionar o botão:
//
//   const html = document.documentElement;
//   const salvo = localStorage.getItem("theme");
//   if (salvo) html.dataset.theme = salvo;
//   botao.addEventListener("click", () => {
//     const claro = getComputedStyle(html).colorScheme.includes("light");
//     html.dataset.theme = claro ? "dark" : "light";
//     localStorage.setItem("theme", html.dataset.theme);
//   });
//
// Sem o botão no HTML, não adicione o listener: handler pendurado em elemento
// que não existe é erro silencioso no console de todo mundo.
