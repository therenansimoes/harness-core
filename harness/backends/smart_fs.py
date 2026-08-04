"""FilesystemMiddleware com guarda de contexto na leitura e na sobrescrita.

Três falhas do executor pequeno custam run inteiro e as três nascem em tool de
arquivo:

1. `read_file` sem paginação em arquivo de milhares de linhas: o default da
   lib é 100 linhas, mas o modelo não sabe o tamanho do arquivo e pagina no
   escuro (ou desiste e edita no chute). A guarda devolve as primeiras 60
   linhas MAIS o tamanho real e o caminho da paginação.
2. `write_file` reescrevendo um arquivo existente com um pedaço só: o
   "conteúdo final completo" vira o trecho que o modelo lembrava, e o resto do
   arquivo evapora sem aviso. A guarda recusa encolhimento grande e manda usar
   `edit_range`.
3. Escrever em arquivo existente SEM ter lido a versão atual: o modelo
   reescreve pelo que lembra do turno passado e derruba o que entrou depois
   (caso real: a reescrita de uma página dropou o `<script src="app.js">`). O
   gate READ-BEFORE-WRITE exige leitura fresca — sha do que foi lido igual ao
   sha do arquivo agora — antes de qualquer escrita destrutiva.

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

import hashlib
import posixpath
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

# Registro do gate: path normalizado -> sha256 do conteúdo que o modelo LEU.
# Módulo e não instância de propósito: `edit_range`/`insert_lines`/`append_file`
# vêm de `file_tools.py`, nascem fora do middleware e precisam consultar o MESMO
# registro que o `read_file` alimenta. Entrada sobrevivente de um run anterior
# não afrouxa nada — o gate só passa quando o sha ainda casa com o arquivo, e
# sha igual significa que ninguém mexeu no arquivo desde aquela leitura.
_READS: dict[str, str] = {}

READ_GATE_UNREAD = (
    "Erro: leia o arquivo antes de reescrever (read_file); ele pode ter mudado "
    "desde teu último turno. Leia e refaça a escrita com o conteúdo atual."
)
READ_GATE_STALE = (
    "Erro: o arquivo mudou desde tua leitura; leia de novo (read_file) antes de "
    "reescrever — escrever agora apagaria o que entrou no meio."
)
NOOP_WRITE = "Nada a mudar: o content enviado é idêntico ao arquivo atual."

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
recusado: para mudar um trecho use `edit_file` ou `edit_range`.

Reescrever arquivo existente exige `read_file` DEPOIS da última mudança dele: se
você não leu, ou se o arquivo mudou desde a sua leitura, a escrita é recusada e
você lê de novo antes de tentar."""

EDIT_FILE_DESCRIPTION = """Troca um trecho exato por outro dentro de um arquivo.

O match de `old_string` é byte a byte, incluindo indentação, e precisa ser único
no arquivo (senão use `replace_all=true`). Falhou duas vezes com "String not
found"? Pare de variar o texto e use `edit_range` com o número da linha.

Editar arquivo existente exige `read_file` DEPOIS da última mudança dele: sem
leitura fresca a edição é recusada."""


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


class SmartEditFileSchema(BaseModel):
    """Schema do `edit_file`. Idêntico ao da lib; declarado aqui pelo mesmo
    motivo do `SmartWriteFileSchema`."""

    file_path: str = Field(description="Path absoluto do arquivo (no workspace virtual).")
    old_string: str = Field(description="Trecho exato a substituir (sem a numeração do read_file).")
    new_string: str = Field(description="Trecho novo.")
    replace_all: bool = Field(default=False, description="Substituir todas as ocorrências.")


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
                return self._mark_read(file_path, _append_notice(result, notice))
            return self._mark_read(
                file_path,
                original.func(
                    file_path=file_path,
                    runtime=runtime,
                    **_passthrough(offset, limit),
                ),
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
                return self._mark_read(file_path, _append_notice(result, notice))
            return self._mark_read(
                file_path,
                await original.coroutine(
                    file_path=file_path,
                    runtime=runtime,
                    **_passthrough(offset, limit),
                ),
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
        except Exception:
            return None
        notice = (
            f"\n[guarda de contexto] mostrando as primeiras {min(GUARD_HEAD_LINES, total)} linhas. "
            f"TOTAL: {total} linhas, {size / 1024:.1f} KB — use file_outline(path) e "
            f"read_file(path, offset=, limit=) para ir direto ao trecho."
        )
        return total, notice

    # ----------------------------------------------------------- read gate #

    def _mark_read(self, file_path: str, result: Any) -> Any:
        """Registra a leitura de `file_path` e devolve `result` intocado.

        O sha é do arquivo INTEIRO mesmo quando a leitura foi paginada: o que o
        gate garante é "você olhou este arquivo depois da última mudança dele",
        e quem cuida de reescrever menos do que existe é o shrink-guard.
        """
        if isinstance(result, ToolMessage) and result.status == "error":
            return result
        current = self._current(file_path)
        if current is not None:
            record_read(file_path, current)
        return result

    def _current(self, file_path: str) -> str | None:
        """Conteúdo utf-8 do arquivo agora, ou `None`.

        `None` cobre arquivo novo, binário, path inválido e backend que
        reclamou — todos os casos em que as guardas daqui saem de cena
        (fail-open: guarda nunca derruba leitura nem escrita).
        """
        try:
            path = validate_path(file_path)
            probe = self.backend.read(path, 0, 1)
            if probe.error or probe.file_data is None:
                return None
            if probe.file_data.get("encoding") != "utf-8":
                return None
            total = probe.total_lines or 1
            full = self.backend.read(path, 0, total)
            return full.file_data["content"] if full.file_data else None
        except Exception:
            return None

    # ---------------------------------------------------------------- write #

    def _create_write_file_tool(self) -> Any:
        original = super()._create_write_file_tool()
        description = self._custom_tool_descriptions.get("write_file") or WRITE_FILE_DESCRIPTION

        def sync_write_file(file_path: str, content: str, runtime: ToolRuntime) -> Any:
            verdict = self._write_verdict(file_path, content)
            if verdict is not None:
                return _tool_error("write_file", runtime, *verdict)
            return self._mark_read(
                file_path,
                original.func(file_path=file_path, content=content, runtime=runtime),
            )

        async def async_write_file(file_path: str, content: str, runtime: ToolRuntime) -> Any:
            verdict = self._write_verdict(file_path, content)
            if verdict is not None:
                return _tool_error("write_file", runtime, *verdict)
            return self._mark_read(
                file_path,
                await original.coroutine(file_path=file_path, content=content, runtime=runtime),
            )

        return StructuredTool.from_function(
            name="write_file",
            description=description,
            func=sync_write_file,
            coroutine=async_write_file,
            infer_schema=False,
            args_schema=SmartWriteFileSchema,
        )

    def _write_verdict(self, file_path: str, content: str) -> tuple[str, str] | None:
        """`(mensagem, status)` para curto-circuitar a escrita, ou `None`.

        Ordem: escrita idêntica ao disco é no-op permitido (o modelo já está
        onde queria chegar), depois o gate de leitura fresca, e só então o
        shrink-guard — pedir para ler é instrução mais útil do que discutir
        tamanho com quem escreve de memória.
        """
        current = self._current(file_path)
        if current is None:
            return None  # arquivo novo: nada para perder
        if content == current:
            record_read(file_path, current)
            return NOOP_WRITE, "success"
        gate = needs_fresh_read(file_path, current)
        if gate is not None:
            return gate, "error"
        shrink = _shrink_refusal(current, content)
        if shrink is not None:
            return shrink, "error"
        return None

    # ----------------------------------------------------------------- edit #

    def _create_edit_file_tool(self) -> Any:
        original = super()._create_edit_file_tool()
        description = self._custom_tool_descriptions.get("edit_file") or EDIT_FILE_DESCRIPTION

        def sync_edit_file(
            file_path: str,
            old_string: str,
            new_string: str,
            runtime: ToolRuntime,
            replace_all: bool = False,
        ) -> Any:
            gate = self._edit_gate(file_path)
            if gate is not None:
                return _tool_error("edit_file", runtime, gate, "error")
            return self._mark_read(
                file_path,
                original.func(
                    file_path=file_path,
                    old_string=old_string,
                    new_string=new_string,
                    runtime=runtime,
                    replace_all=replace_all,
                ),
            )

        async def async_edit_file(
            file_path: str,
            old_string: str,
            new_string: str,
            runtime: ToolRuntime,
            replace_all: bool = False,
        ) -> Any:
            gate = self._edit_gate(file_path)
            if gate is not None:
                return _tool_error("edit_file", runtime, gate, "error")
            return self._mark_read(
                file_path,
                await original.coroutine(
                    file_path=file_path,
                    old_string=old_string,
                    new_string=new_string,
                    runtime=runtime,
                    replace_all=replace_all,
                ),
            )

        return StructuredTool.from_function(
            name="edit_file",
            description=description,
            func=sync_edit_file,
            coroutine=async_edit_file,
            infer_schema=False,
            args_schema=SmartEditFileSchema,
        )

    def _edit_gate(self, file_path: str) -> str | None:
        """Gate de leitura fresca para `edit_file`. `None` deixa passar."""
        return needs_fresh_read(file_path, self._current(file_path))


def record_read(path: str, content: str) -> None:
    """Marca que a versão ATUAL (`content`) de `path` passou pelos olhos do modelo.

    Também é chamada depois de uma escrita bem-sucedida: quem acabou de escrever
    sabe o que está no arquivo, e bloquear a edição seguinte seria só atrito.
    """
    _READS[_read_key(path)] = _sha(content)


def needs_fresh_read(path: str, current: str | None = None) -> str | None:
    """Motivo para recusar a escrita em `path`, ou `None` para deixar passar.

    `current` é o conteúdo do arquivo AGORA; `None` significa arquivo novo (nada
    para perder, passa direto). É a função que `file_tools.py` importa para o
    `edit_range`/`insert_lines`/`append_file` usarem o MESMO gate das tools do
    middleware, com o mesmo registro de leituras.
    """
    if current is None:
        return None
    seen = _READS.get(_read_key(path))
    if seen is None:
        return READ_GATE_UNREAD
    if seen != _sha(current):
        return READ_GATE_STALE
    return None


def reset_reads() -> None:
    """Esquece todas as leituras. Existe para teste — em produção o registro
    morre junto com o processo do run."""
    _READS.clear()


def _read_key(path: str) -> str:
    """Chave do registro: o path como o MODELO fala, sem a barra da raiz.

    O filesystem do executor é virtual com raiz no workspace, então `/app.py`
    (tools do middleware) e `app.py` (tools de `file_tools.py`) são o mesmo
    arquivo e precisam cair na mesma chave.
    """
    return posixpath.normpath("/" + str(path).lstrip("/")).lstrip("/")


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _shrink_refusal(current: str, content: str) -> str | None:
    """Motivo da recusa por encolhimento, ou `None` para deixar passar.

    Sem flag de override de propósito: a saída certa é `edit_range`, e uma flag
    `force=true` seria clicada em toda tentativa.
    """
    if not current or len(content) >= SHRINK_FLOOR * len(current):
        return None
    lost = round((1 - len(content) / len(current)) * 100)
    return (
        f"Erro: recusado — isso apagaria ~{lost}% do arquivo "
        f"({len(current)} bytes agora, {len(content)} no content enviado). "
        f"Use edit_range para mudar só o trecho, ou confirme reescrevendo com o "
        f"conteúdo completo do arquivo."
    )


def _tool_error(name: str, runtime: ToolRuntime, message: str, status: str) -> ToolMessage:
    """Resposta curto-circuitada da tool, sem tocar no backend."""
    return ToolMessage(
        content=message,
        name=name,
        tool_call_id=runtime.tool_call_id,
        status=status,
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
