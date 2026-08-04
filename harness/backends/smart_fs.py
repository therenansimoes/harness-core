"""FilesystemMiddleware com guarda de contexto na leitura e na sobrescrita.

Duas falhas do executor pequeno custam run inteiro e as duas nascem em tool de
arquivo:

1. `read_file` sem paginação em arquivo de milhares de linhas: o default da
   lib é 100 linhas, mas o modelo não sabe o tamanho do arquivo e pagina no
   escuro (ou desiste e edita no chute). A guarda devolve as primeiras 60
   linhas MAIS o tamanho real e o caminho da paginação.
2. `write_file` reescrevendo um arquivo existente com um pedaço só: o
   "conteúdo final completo" vira o trecho que o modelo lembrava, e o resto do
   arquivo evapora sem aviso. A guarda recusa encolhimento grande e manda usar
   `edit_range`.

Como o override funciona: o `FilesystemMiddleware` monta `self.tools` chamando
métodos bound (`_create_read_file_tool` etc.), então a subclasse troca a tool
sobrescrevendo o método. As duas tools daqui EMBRULHAM a original (chamam
`orig.func`/`orig.coroutine` por dentro) em vez de reimplementar a leitura —
permissão, `validate_path`, encoding base64, numeração e truncagem por token
continuam sendo problema da lib.

Pegadinha que já custou um stack duplicado: `AgentMiddleware.name` é o nome da
CLASSE e o merge de middleware é por nome. Sem a property `name` fixada em
`"FilesystemMiddleware"`, esta subclasse entra no grafo COMO OUTRO middleware,
ao lado do original — sem erro, com as duas versões de cada tool.
"""

from __future__ import annotations

from typing import Any

from deepagents.middleware.filesystem import FilesystemMiddleware, validate_path
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

# Acima disso a leitura sem paginação é sintoma, não intenção.
BIG_FILE_LINES = 2000
# Janela que a guarda devolve: o suficiente para saber o que é o arquivo.
GUARD_HEAD_LINES = 60
# Sobrescrever com menos de 70% do tamanho atual é perda de conteúdo, não edição.
SHRINK_FLOOR = 0.7

READ_FILE_DESCRIPTION = r"""Lê o conteúdo de um arquivo do workspace.

A saída vem com números de linha na margem (`  12\tdef foo():`) que NÃO existem
no arquivo. Nunca copie a numeração nem o TAB para dentro de `old_string` ou
`content`.

Sem `offset`/`limit` a leitura traz o começo do arquivo. Em arquivo com mais de
2000 linhas ela traz só as 60 primeiras e informa o tamanho total: nesse caso
use `file_outline(path)` para achar o trecho e volte com
`read_file(file_path=..., offset=<linha-1>, limit=<n>)` (`offset` é 0-indexado,
a margem é 1-indexada).

Leia antes de editar. Editar arquivo que você não leu é chute."""

WRITE_FILE_DESCRIPTION = """Escreve um arquivo INTEIRO, criando ou sobrescrevendo.

`content` é o conteúdo final completo do arquivo, não o pedaço novo. Ao
reescrever arquivo existente, preserve TUDO que a tarefa não mandou mudar.

Sobrescrever um arquivo existente com muito menos conteúdo do que ele tem é
recusado: para mudar um trecho use `edit_file` ou `edit_range`."""


class SmartReadFileSchema(BaseModel):
    """Schema do `read_file` com `offset`/`limit` opcionais de verdade.

    O schema da lib declara `default=0`/`default=100`, e o langchain preenche
    default de schema mesmo quando o campo não veio na tool call — com ele não
    existe como distinguir "o modelo pediu a página 1" de "o modelo não pensou
    em paginação", que é exatamente a distinção que a guarda precisa.
    """

    file_path: str = Field(description="Path absoluto do arquivo (no workspace virtual).")
    offset: int | None = Field(
        default=None,
        description="Linha inicial (0-indexada). Omita na primeira leitura.",
    )
    limit: int | None = Field(
        default=None,
        description="Máximo de linhas a ler. Omita na primeira leitura.",
    )


class SmartWriteFileSchema(BaseModel):
    """Schema do `write_file`. Idêntico ao da lib; declarado aqui para não
    depender de detalhe interno da versão instalada."""

    file_path: str = Field(description="Path absoluto do arquivo (no workspace virtual).")
    content: str = Field(description="Conteúdo final COMPLETO do arquivo.")


class SmartFilesystemMiddleware(FilesystemMiddleware):
    """`FilesystemMiddleware` com `read_file` paginado à força e `write_file` com
    shrink-guard. Substitui o original no stack (mesmo `.name`)."""

    @property
    def name(self) -> str:
        """Nome do middleware original, de propósito: o merge é por nome e o
        objetivo é SUBSTITUIR, não coexistir."""
        return "FilesystemMiddleware"

    # ----------------------------------------------------------------- read #

    def _create_read_file_tool(self) -> Any:
        original = super()._create_read_file_tool()
        description = self._custom_tool_descriptions.get("read_file") or READ_FILE_DESCRIPTION

        def sync_read_file(
            file_path: str,
            runtime: ToolRuntime,
            offset: int | None = None,
            limit: int | None = None,
        ) -> Any:
            guard = self._guard_notice(file_path) if offset is None and limit is None else None
            if guard is not None:
                total_lines, notice = guard
                result = original.func(
                    file_path=file_path,
                    runtime=runtime,
                    offset=0,
                    limit=min(GUARD_HEAD_LINES, total_lines),
                )
                return _append_notice(result, notice)
            return original.func(
                file_path=file_path,
                runtime=runtime,
                **_passthrough(offset, limit),
            )

        async def async_read_file(
            file_path: str,
            runtime: ToolRuntime,
            offset: int | None = None,
            limit: int | None = None,
        ) -> Any:
            guard = self._guard_notice(file_path) if offset is None and limit is None else None
            if guard is not None:
                total_lines, notice = guard
                result = await original.coroutine(
                    file_path=file_path,
                    runtime=runtime,
                    offset=0,
                    limit=min(GUARD_HEAD_LINES, total_lines),
                )
                return _append_notice(result, notice)
            return await original.coroutine(
                file_path=file_path,
                runtime=runtime,
                **_passthrough(offset, limit),
            )

        return StructuredTool.from_function(
            name="read_file",
            description=description,
            func=sync_read_file,
            coroutine=async_read_file,
            infer_schema=False,
            args_schema=SmartReadFileSchema,
        )

    def _guard_notice(self, file_path: str) -> tuple[int, str] | None:
        """`(total_lines, aviso)` se o arquivo é grande demais para ler inteiro.

        `None` quando a guarda não se aplica (arquivo pequeno, binário, path
        inválido, backend sem `total_lines`) — a guarda é fail-open: erro aqui
        vira leitura normal, nunca run derrubado.
        """
        try:
            probe = self.backend.read(validate_path(file_path), 0, 1)
            if probe.error or probe.file_data is None:
                return None
            if probe.file_data.get("encoding") != "utf-8":
                return None
            total = probe.total_lines
            if not total or total <= BIG_FILE_LINES:
                return None
            full = self.backend.read(validate_path(file_path), 0, total)
            size = len(full.file_data["content"].encode("utf-8")) if full.file_data else 0
        except Exception:  # noqa: BLE001 - guarda de contexto não derruba leitura
            return None
        notice = (
            f"\n[guarda de contexto] mostrando as primeiras {min(GUARD_HEAD_LINES, total)} linhas. "
            f"TOTAL: {total} linhas, {size / 1024:.1f} KB — use file_outline(path) e "
            f"read_file(path, offset=, limit=) para ir direto ao trecho."
        )
        return total, notice

    # ---------------------------------------------------------------- write #

    def _create_write_file_tool(self) -> Any:
        original = super()._create_write_file_tool()
        description = self._custom_tool_descriptions.get("write_file") or WRITE_FILE_DESCRIPTION

        def sync_write_file(file_path: str, content: str, runtime: ToolRuntime) -> Any:
            refusal = self._shrink_refusal(file_path, content)
            if refusal is not None:
                return ToolMessage(
                    content=refusal,
                    name="write_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            return original.func(file_path=file_path, content=content, runtime=runtime)

        async def async_write_file(file_path: str, content: str, runtime: ToolRuntime) -> Any:
            refusal = self._shrink_refusal(file_path, content)
            if refusal is not None:
                return ToolMessage(
                    content=refusal,
                    name="write_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            return await original.coroutine(file_path=file_path, content=content, runtime=runtime)

        return StructuredTool.from_function(
            name="write_file",
            description=description,
            func=sync_write_file,
            coroutine=async_write_file,
            infer_schema=False,
            args_schema=SmartWriteFileSchema,
        )

    def _shrink_refusal(self, file_path: str, content: str) -> str | None:
        """Motivo da recusa, ou `None` para deixar passar.

        Sem flag de override de propósito: a saída certa é `edit_range`, e uma
        flag `force=true` seria clicada em toda tentativa.
        """
        try:
            path = validate_path(file_path)
            probe = self.backend.read(path, 0, 1)
            if probe.error or probe.file_data is None:
                return None  # arquivo novo: nada para perder
            if probe.file_data.get("encoding") != "utf-8":
                return None
            total = probe.total_lines or 1
            full = self.backend.read(path, 0, total)
            current = full.file_data["content"] if full.file_data else ""
        except Exception:  # noqa: BLE001 - guarda nunca derruba a escrita
            return None
        if not current or len(content) >= SHRINK_FLOOR * len(current):
            return None
        lost = round((1 - len(content) / len(current)) * 100)
        return (
            f"Erro: recusado — isso apagaria ~{lost}% do arquivo "
            f"({len(current)} bytes agora, {len(content)} no content enviado). "
            f"Use edit_range para mudar só o trecho, ou confirme reescrevendo com o "
            f"conteúdo completo do arquivo."
        )


def _passthrough(offset: int | None, limit: int | None) -> dict[str, int]:
    """Só repassa o que o modelo mandou; o resto fica com o default da lib."""
    args: dict[str, int] = {}
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    return args


def _append_notice(result: Any, notice: str) -> Any:
    """Cola o aviso no fim do conteúdo textual, preservando o resto da mensagem."""
    if isinstance(result, ToolMessage) and isinstance(result.content, str):
        result.content = result.content + notice
    return result
