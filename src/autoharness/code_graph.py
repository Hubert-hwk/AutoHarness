"""Lightweight Python AST code graph and issue localization."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from autoharness.models import CodeLocation, FailureDiagnosis


@dataclass
class CodeNode:
    node_id: str
    path: str
    symbol: str
    kind: str
    line: int
    text: str
    calls: set[str] = field(default_factory=set)
    imports: set[str] = field(default_factory=set)


@dataclass
class IndexStats:
    files: int = 0
    symbols: int = 0
    parse_errors: int = 0


class PythonCodeGraph:
    """Index Python structure locally without a database or language server."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.nodes: dict[str, CodeNode] = {}
        self.stats = IndexStats()

    def build(self) -> IndexStats:
        if not self.root.is_dir():
            raise ValueError(f"Repository path is not a directory: {self.root}")
        self.nodes.clear()
        self.stats = IndexStats()
        for path in sorted(self.root.rglob("*.py")):
            if self._ignored(path):
                continue
            self._index_file(path)
        self.stats.symbols = len(self.nodes)
        return self.stats

    def locate(self, diagnosis: FailureDiagnosis, limit: int = 8) -> list[CodeLocation]:
        terms = [term.lower() for term in diagnosis.search_terms if len(term) >= 3]
        ranked: list[tuple[float, CodeNode, list[str]]] = []
        for node in self.nodes.values():
            haystack = f"{node.path} {node.symbol} {node.text}".lower()
            score = 0.0
            reasons: list[str] = []
            for term in terms:
                if term in node.symbol.lower():
                    score += 4.0
                    reasons.append(f"symbol matches '{term}'")
                elif term in node.path.lower():
                    score += 2.5
                    reasons.append(f"path matches '{term}'")
                elif re.search(rf"\b{re.escape(term)}\b", haystack):
                    score += 1.0
                    reasons.append(f"source matches '{term}'")
                if term in node.calls:
                    score += 1.5
                    reasons.append(f"calls '{term}'")
            if diagnosis.failure_type.value.split("_")[0] in haystack:
                score += 1.0
                reasons.append("matches diagnosed component")
            if score:
                ranked.append((score, node, list(dict.fromkeys(reasons))))

        ranked.sort(key=lambda item: (-item[0], item[1].path, item[1].line))
        return [
            CodeLocation(
                path=node.path,
                symbol=node.symbol,
                kind=node.kind,
                line=node.line,
                score=round(score, 2),
                reasons=reasons[:5],
            )
            for score, node, reasons in ranked[:limit]
        ]

    def _index_file(self, path: Path) -> None:
        relative = path.relative_to(self.root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, SyntaxError, UnicodeDecodeError):
            self.stats.parse_errors += 1
            return
        self.stats.files += 1
        module_imports = self._imports(tree)
        file_node = CodeNode(
            node_id=relative,
            path=relative,
            symbol=relative,
            kind="file",
            line=1,
            text=self._summary_text(tree, source),
            imports=module_imports,
        )
        self.nodes[file_node.node_id] = file_node
        for node in ast.walk(tree):
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            symbol = node.name
            node_id = f"{relative}:{symbol}:{node.lineno}"
            segment = ast.get_source_segment(source, node) or ""
            self.nodes[node_id] = CodeNode(
                node_id=node_id,
                path=relative,
                symbol=symbol,
                kind=kind,
                line=node.lineno,
                text=f"{ast.get_docstring(node) or ''} {segment[:2000]}",
                calls=self._calls(node),
                imports=module_imports,
            )

    def _ignored(self, path: Path) -> bool:
        ignored = {".git", ".venv", "node_modules", "dist", "build", "__pycache__"}
        return any(
            part in ignored or part.startswith(".") for part in path.relative_to(self.root).parts
        )

    @staticmethod
    def _summary_text(tree: ast.Module, source: str) -> str:
        names = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        return f"{ast.get_docstring(tree) or ''} {' '.join(names)} {source[:3000]}"

    @staticmethod
    def _calls(node: ast.AST) -> set[str]:
        calls: set[str] = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Name):
                calls.add(child.func.id.lower())
            elif isinstance(child.func, ast.Attribute):
                calls.add(child.func.attr.lower())
        return calls

    @staticmethod
    def _imports(tree: ast.Module) -> set[str]:
        imports: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        return imports
