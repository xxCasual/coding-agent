import ast
from pathlib import Path

from review_agent.models.context import AstSymbol, EnclosingSymbol, PythonAstSummary


def python_ast_summary_tool(file_path: str | Path) -> PythonAstSummary:
    path = Path(file_path)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = _extract_imports_from_tree(tree, source)
    classes: list[AstSymbol] = []
    functions: list[AstSymbol] = []
    signatures: list[str] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            class_symbol = AstSymbol(
                name=node.name,
                kind="class",
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                signature=_signature_for_node(source, node),
            )
            classes.append(class_symbol)
            if class_symbol.signature:
                signatures.append(class_symbol.signature)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    signature = _signature_for_node(source, child)
                    functions.append(
                        AstSymbol(
                            name=f"{node.name}.{child.name}",
                            kind="function",
                            start_line=child.lineno,
                            end_line=child.end_lineno or child.lineno,
                            signature=signature,
                        )
                    )
                    if signature:
                        signatures.append(signature)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signature = _signature_for_node(source, node)
            functions.append(
                AstSymbol(
                    name=node.name,
                    kind="function",
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    signature=signature,
                )
            )
            if signature:
                signatures.append(signature)

    return PythonAstSummary(
        file_path=path.as_posix(),
        imports=imports,
        classes=classes,
        functions=functions,
        signatures=signatures,
    )


def find_enclosing_symbol_tool(file_path: str | Path, line_no: int) -> EnclosingSymbol | None:
    summary = python_ast_summary_tool(file_path)
    symbols: list[AstSymbol] = [*summary.classes, *summary.functions]
    candidates = [
        symbol for symbol in symbols if symbol.start_line <= line_no <= symbol.end_line
    ]
    if not candidates:
        return None
    chosen = max(candidates, key=lambda symbol: symbol.start_line)
    return EnclosingSymbol(
        name=chosen.name,
        kind=chosen.kind,
        start_line=chosen.start_line,
        end_line=chosen.end_line,
        signature=chosen.signature,
    )


def extract_imports_tool(file_path: str | Path) -> list[str]:
    return python_ast_summary_tool(file_path).imports


def extract_signatures_tool(file_path: str | Path) -> list[str]:
    return python_ast_summary_tool(file_path).signatures


def _extract_imports_from_tree(tree: ast.AST, source: str) -> list[str]:
    imports: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            segment = ast.get_source_segment(source, node)
            if segment:
                imports.append(segment.strip())
    return imports


def _signature_for_node(source: str, node: ast.AST) -> str | None:
    lines = source.splitlines()
    if not hasattr(node, "lineno"):
        return None
    first_line = lines[node.lineno - 1].strip()
    if first_line.endswith(":"):
        first_line = first_line[:-1]
    return first_line
