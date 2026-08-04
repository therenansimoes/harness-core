import { describe, expect, it } from "vitest";

import { escapeHtml, shellMarkup } from "../src/main.js";

describe("shellMarkup", () => {
  it("monta o h1 com o título recebido", () => {
    const html = shellMarkup({ title: "Olá", lead: "resumo" });
    expect(html).toContain("<h1>Olá</h1>");
    expect(html).toContain("resumo");
  });

  it("escapa texto antes de virar innerHTML", () => {
    expect(escapeHtml('<img src=x onerror="alert(1)">')).not.toContain("<img");
  });
});
