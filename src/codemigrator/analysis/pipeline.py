"""Deterministic, read-only F1-F4 and PSF derivation pipeline."""

from __future__ import annotations

import json
import posixpath
import re
import tomllib
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping

from codemigrator.core import (
    ModuleBoundaryStrategy,
    ProjectModuleId,
    RepoRelativePath,
    StableErrorCode,
)

from .grammar import GrammarCache, GrammarCircuitBreaker, GrammarFailure, SyntaxNode
from .models import (
    AnalysisError,
    AnalysisResult,
    ArtifactFact,
    CallEdge,
    CoverageDerivation,
    CoverageEntry,
    DependencyEntry,
    EdgeConfidence,
    ExportSummary,
    ExternalTarget,
    ImportEdge,
    ManifestSummary,
    ModuleBoundary,
    ModuleCoverage,
    ModuleCoverageStatus,
    ModuleFact,
    ModuleRole,
    ModuleTarget,
    ReferenceSite,
    RelationEdge,
    ScriptEntry,
    SourcePosition,
    SourceRange,
    SymbolBinding,
    SymbolCoverageEdge,
    SymbolKind,
    TestConservationBaseline,
    UnknownReason,
)
from .ports import SnapshotSource
from .rules import ImportRule, SourceAnalysisDescriptor, TextRule, descriptor_pattern_matches


def _module_key(
    path: str,
    is_test: bool,
    strategy: ModuleBoundaryStrategy,
    manifest_paths: tuple[str, ...],
) -> str:
    strategy_value = _enum_value(strategy)
    if strategy_value == "MANIFEST_PER_MODULE" and manifest_paths:
        matching_roots = [
            posixpath.dirname(manifest_path) or "." for manifest_path in manifest_paths
        ]
        roots = [
            root
            for root in matching_roots
            if root == "." or path == root or path.startswith(f"{root}/")
        ]
        if roots:
            directory = max(roots, key=lambda root: len(root.encode("utf-8")))
            return f"{directory}#test" if is_test else directory
    directory = posixpath.dirname(path) or "."
    return f"{directory}#test" if is_test else directory


def _module_boundary(strategy: ModuleBoundaryStrategy) -> ModuleBoundary:
    value = getattr(strategy, "value", strategy)
    if not isinstance(value, str):
        raise ValueError(f"unsupported module boundary strategy: {strategy!r}")
    return {
        "MANIFEST_PER_MODULE": ModuleBoundary.Manifest,
        "SINGLE_MANIFEST_DIRECTORY_CONVENTION": ModuleBoundary.Directory,
        "DIRECTORY_CONVENTION": ModuleBoundary.Directory,
    }[value]


def _module_id(snapshot_oid: str, descriptor_sha256: str, key: str) -> ProjectModuleId:
    value = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"codemigrator:analysis:{snapshot_oid}:{descriptor_sha256}:{key}",
    )
    return ProjectModuleId(value)


def _source_range(path: str, text: str, start: int, end: int) -> SourceRange:
    def position(offset: int) -> SourcePosition:
        prefix = text[:offset]
        return SourcePosition(line=prefix.count("\n") + 1, column=len(prefix.rsplit("\n", 1)[-1]))

    return SourceRange(
        file_path=RepoRelativePath(path),
        start=position(start),
        end=position(end),
    )


def _rule_matches(text: str, rule: TextRule | ImportRule) -> list[re.Match[str]]:
    return list(re.finditer(rule.pattern, text, re.MULTILINE))


def _byte_offset(text: str, character_offset: int) -> int:
    return len(text[:character_offset].encode("utf-8"))


def _character_offset(text: str, byte_offset: int) -> int:
    return len(text.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore"))


def _range_from_bytes(path: str, text: str, start_byte: int, end_byte: int) -> SourceRange:
    return _source_range(
        path,
        text,
        _character_offset(text, start_byte),
        _character_offset(text, end_byte),
    )


def _tree_sitter_name(node: object, content: bytes) -> str | None:
    child_by_field_name = getattr(node, "child_by_field_name", None)
    if not callable(child_by_field_name):
        return None
    name_node = child_by_field_name("name")
    if name_node is None:
        return None
    start = getattr(name_node, "start_byte", None)
    end = getattr(name_node, "end_byte", None)
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    return content[start:end].decode("utf-8", errors="ignore") or None


def _syntax_nodes(tree: object, content: bytes) -> tuple[SyntaxNode, ...]:
    """Adapt the local immutable node shape or a tree-sitter tree to one view."""

    root = getattr(tree, "root_node", tree)
    if isinstance(root, SyntaxNode):
        nodes: list[SyntaxNode] = []

        def visit(local: SyntaxNode) -> None:
            nodes.append(local)
            for child in local.children:
                visit(child)

        visit(root)
        return tuple(nodes)

    nodes = []

    def visit_external(node: object) -> None:
        kind = getattr(node, "type", None)
        start = getattr(node, "start_byte", None)
        end = getattr(node, "end_byte", None)
        if not isinstance(kind, str) or not isinstance(start, int) or not isinstance(end, int):
            return
        nodes.append(
            SyntaxNode(
                kind=kind,
                start_byte=start,
                end_byte=end,
                name=_tree_sitter_name(node, content),
            )
        )
        children = getattr(node, "children", ())
        for child in children:
            visit_external(child)

    visit_external(root)
    return tuple(nodes)


def _default_syntax_tree(content: bytes, descriptor: SourceAnalysisDescriptor) -> SyntaxNode:
    """Provide a deterministic descriptor-backed tree when no runtime grammar is injected.

    Runtime integrations pass a tree-sitter tree.  This small adapter keeps the
    pure analysis API useful for descriptors and fixtures that do not ship a
    grammar binary; facts still flow through syntax nodes rather than discarding
    a parser result.
    """

    text = content.decode("utf-8", errors="replace")
    children: list[SyntaxNode] = []
    for rule in descriptor.export_rules:
        for match in _rule_matches(text, rule):
            try:
                name = match.group("symbol")
            except IndexError:
                continue
            children.append(
                SyntaxNode(
                    kind=rule.kind.lower(),
                    name=name,
                    start_byte=_byte_offset(text, match.start()),
                    end_byte=_byte_offset(text, match.end()),
                )
            )
    for marker in re.finditer(r"\b(?:ERROR|MISSING)\b", text):
        children.append(
            SyntaxNode(
                kind=marker.group(0),
                start_byte=_byte_offset(text, marker.start()),
                end_byte=_byte_offset(text, marker.end()),
            )
        )
    return SyntaxNode(
        kind="module",
        start_byte=0,
        end_byte=len(content),
        children=tuple(children),
    )


def _node_kind_matches(node_kind: str, rule_kind: str) -> bool:
    node_kind = node_kind.lower()
    rule_kind = rule_kind.lower()
    aliases = {
        "function": {
            "function",
            "function_definition",
            "function_declaration",
            "method_definition",
        },
        "class": {"class", "class_definition", "class_declaration"},
        "type": {"type", "type_alias", "type_definition"},
        "interface": {"interface", "interface_declaration"},
        "constant": {"constant", "const_declaration", "variable_declaration"},
    }
    return node_kind in aliases.get(rule_kind, {rule_kind})


def _ast_export_matches(
    path: str,
    text: str,
    tree: object | None,
    descriptor: SourceAnalysisDescriptor,
) -> list[tuple[str, TextRule, SourceRange]]:
    if tree is None:
        return []
    content = text.encode("utf-8")
    nodes = sorted(
        _syntax_nodes(tree, content),
        key=lambda node: (node.start_byte, node.end_byte, node.kind),
    )
    matches: list[tuple[str, TextRule, SourceRange]] = []
    for rule in descriptor.export_rules:
        for node in nodes:
            if node.name is None or not _node_kind_matches(node.kind, rule.kind):
                continue
            matches.append(
                (node.name, rule, _range_from_bytes(path, text, node.start_byte, node.end_byte))
            )
    return matches


def _tree_is_degraded(tree: object | None, content: bytes) -> bool:
    if tree is None:
        return False
    return any(
        node.kind.upper() == "ERROR" or node.kind.upper().startswith("MISSING")
        for node in _syntax_nodes(tree, content)
    )


def _range_offsets(text: str, source_range: SourceRange) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)

    def offset(position: SourcePosition) -> int:
        prefix = "".join(lines[: position.line - 1])
        return len(prefix) + position.column

    return offset(source_range.start), offset(source_range.end)


def _identifier_ranges(
    path: str,
    text: str,
    tree: object | None,
    symbol: str,
) -> list[tuple[int, SourceRange]]:
    if tree is not None:
        identifier_nodes = [
            node
            for node in _syntax_nodes(tree, text.encode("utf-8"))
            if node.name == symbol
            and node.kind.lower()
            in {"identifier", "field_identifier", "property_identifier", "type_identifier"}
        ]
        if identifier_nodes:
            return [
                (
                    _character_offset(text, node.start_byte),
                    _range_from_bytes(path, text, node.start_byte, node.end_byte),
                )
                for node in identifier_nodes
            ]
    return [
        (match.start(), _source_range(path, text, match.start(), match.end()))
        for match in re.finditer(rf"(?<!\w){re.escape(symbol)}(?!\w)", text)
    ]


def _is_call_site(text: str, source_range: SourceRange) -> bool:
    _, end = _range_offsets(text, source_range)
    return text[end:].lstrip().startswith("(")


def _symbol_kind(value: str) -> SymbolKind:
    try:
        return SymbolKind(value.upper())
    except ValueError as exc:
        raise ValueError(f"unsupported descriptor symbol kind: {value}") from exc


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _resolve_module(
    source_path: str,
    target: str,
    source_paths: set[str],
    source_modules: Mapping[str, ProjectModuleId],
    descriptor: SourceAnalysisDescriptor,
) -> ProjectModuleId | None:
    target = descriptor.aliases.get(target, target)
    if target.startswith("."):
        target = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), target))
    else:
        target = target.replace(".", "/") if "." in target else target
    if target.startswith("../") or target == "..":
        return None
    for path in sorted(source_paths):
        stem = path.rsplit(".", 1)[0] if "." in posixpath.basename(path) else path
        if target in {path, stem, f"{stem}/__init__"} or posixpath.dirname(path) == target:
            return source_modules.get(path)
    return None


def _directory_coverage(
    test_path: str,
    source_paths: set[str],
    source_modules: Mapping[str, ProjectModuleId],
) -> list[ProjectModuleId]:
    """Apply only the declared test-name convention as a coverage fallback."""

    test_stem = posixpath.basename(test_path).rsplit(".", 1)[0]
    if test_stem.startswith("test_"):
        test_stem = test_stem[5:]
    if test_stem.endswith("_test"):
        test_stem = test_stem[:-5]
    matched: set[ProjectModuleId] = set()
    for source_path in source_paths:
        source_stem = posixpath.basename(source_path).rsplit(".", 1)[0]
        if source_stem == test_stem:
            module_id = source_modules.get(source_path)
            if module_id is not None:
                matched.add(module_id)
    return sorted(matched, key=str)


def _parse_manifest(path: str, kind: str, content: bytes) -> ManifestSummary:
    if path.endswith(".json") or kind.lower().endswith("json"):
        document = json.loads(content.decode("utf-8"))
    elif path.endswith(".toml") or kind.lower().endswith("toml"):
        document = tomllib.loads(content.decode("utf-8"))
    else:
        document = {}
    if not isinstance(document, dict):
        raise ValueError("manifest root must be an object")
    project = document.get("project", document)
    if not isinstance(project, dict):
        project = {}
    raw_dependencies = project.get("dependencies", document.get("dependencies", []))
    if isinstance(raw_dependencies, dict):
        dependencies = [
            DependencyEntry(name=str(name), version=str(version))
            for name, version in raw_dependencies.items()
        ]
    elif isinstance(raw_dependencies, list):
        dependencies = [
            DependencyEntry(name=str(item)) for item in raw_dependencies if isinstance(item, str)
        ]
    else:
        dependencies = []
    raw_scripts = project.get("scripts", document.get("scripts", {}))
    scripts = (
        [
            ScriptEntry(name=str(name), command_summary=str(command))
            for name, command in raw_scripts.items()
        ]
        if isinstance(raw_scripts, dict)
        else []
    )
    entry_points = sorted(
        str(item) for item in project.get("entry-points", []) if isinstance(item, str)
    )
    return ManifestSummary(
        manifest_path=RepoRelativePath(path),
        manifest_kind=kind,
        dependencies=sorted(dependencies, key=lambda item: (item.name, item.version)),
        scripts=sorted(scripts, key=lambda item: item.name),
        entry_points=entry_points,
    )


def _error(code: StableErrorCode, message: str, path: str | None = None) -> AnalysisError:
    return AnalysisError(
        code=code,
        message=message,
        path=None if path is None else RepoRelativePath(path),
    )


def analyze_snapshot(
    snapshot: SnapshotSource,
    descriptor: SourceAnalysisDescriptor,
    *,
    parser: Callable[[bytes], object] | None = None,
) -> AnalysisResult:
    """Derive all mechanical facts from a frozen, read-only snapshot.

    The optional parser is invoked for each eligible source file so a runtime
    tree-sitter adapter can supply syntax trees without moving resource I/O
    into this package. Extraction rules remain descriptor-owned and deterministic.
    """

    files: dict[str, bytes] = {}
    texts: dict[str, str] = {}
    skipped_files: list[str] = []
    errors: list[AnalysisError] = []
    grammar_breaker = GrammarCircuitBreaker()
    grammar_cache = GrammarCache[object](max_entries=256)
    syntax_trees: dict[str, object] = {}
    for path in snapshot.paths:
        try:
            content = snapshot.read(path)
        except Exception as exc:
            errors.append(_error(StableErrorCode.ANALYSIS_INFRA_ERROR, str(exc), path))
            continue
        if path == ".git" or path.startswith(".git/"):
            continue
        if len(content) > descriptor.max_file_bytes:
            skipped_files.append(path)
            errors.append(
                _error(
                    StableErrorCode.SOURCE_FILE_TOO_LARGE,
                    f"source file exceeds {descriptor.max_file_bytes} bytes",
                    path,
                )
            )
            continue
        files[path] = content
        try:
            texts[path] = content.decode("utf-8")
        except UnicodeDecodeError:
            texts[path] = content.decode("utf-8", errors="replace")
            errors.append(_error(StableErrorCode.ANALYSIS_INFRA_ERROR, "source is not UTF-8", path))
        if not descriptor.text_fallback and descriptor.is_source_file(path):
            try:
                parser_fn: Callable[[bytes], object] = parser or (
                    lambda payload: _default_syntax_tree(payload, descriptor)
                )
                syntax_trees[path] = grammar_cache.get_or_load(
                    snapshot.snapshot_oid,
                    path,
                    descriptor.grammar_sha256 or "",
                    lambda: grammar_breaker.run(
                        descriptor.grammar_id or "generic",
                        lambda: parser_fn(content),
                    ),
                )
            except GrammarFailure as exc:
                errors.append(_error(exc.code, str(exc), path))

    source_paths = {path for path in texts if descriptor.is_source_file(path)}
    test_paths = {path for path in source_paths if descriptor.is_test_file(path)}
    source_paths -= test_paths
    path_to_module: dict[str, ProjectModuleId] = {}
    module_files: dict[ProjectModuleId, list[str]] = defaultdict(list)
    module_roles: dict[ProjectModuleId, ModuleRole] = {}
    manifests: list[ManifestSummary] = []
    for path in sorted(files):
        kind = descriptor.manifest_kind(path)
        if kind is None:
            continue
        try:
            manifests.append(_parse_manifest(path, kind, files[path]))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            tomllib.TOMLDecodeError,
            ValueError,
        ) as exc:
            errors.append(_error(StableErrorCode.ANALYSIS_INFRA_ERROR, str(exc), path))
    manifest_paths = tuple(
        path for path in sorted(files) if descriptor.manifest_kind(path) is not None
    )
    external_packages = set(descriptor.external_packages)
    for manifest in manifests:
        for dependency in manifest.dependencies:
            package_match = re.match(r"[A-Za-z0-9_.-]+", dependency.name)
            if package_match is not None:
                external_packages.add(package_match.group(0))
    for path in sorted(source_paths | test_paths):
        role = ModuleRole.Test if path in test_paths else ModuleRole.Source
        key = _module_key(
            path,
            role is ModuleRole.Test,
            descriptor.module_boundary_strategy,
            manifest_paths,
        )
        module_id = _module_id(snapshot.snapshot_oid, descriptor.descriptor_sha256, key)
        path_to_module[path] = module_id
        module_files[module_id].append(path)
        module_roles[module_id] = role

    source_modules = {path: path_to_module[path] for path in source_paths}
    exports_by_module: dict[ProjectModuleId, list[ExportSummary]] = defaultdict(list)
    export_ranges: dict[tuple[ProjectModuleId, str], list[SourceRange]] = defaultdict(list)
    if not descriptor.text_fallback:
        for path in sorted(source_paths):
            module_id = path_to_module[path]
            for symbol, export_rule, definition in _ast_export_matches(
                path, texts[path], syntax_trees.get(path), descriptor
            ):
                definition_start, definition_end = _range_offsets(texts[path], definition)
                export = ExportSummary(
                    symbol=symbol,
                    kind=_symbol_kind(export_rule.kind),
                    signature_text=texts[path][definition_start:definition_end][:4096],
                )
                exports_by_module[module_id].append(export)
                export_ranges[(module_id, symbol)].append(definition)

    imports: list[ImportEdge] = []
    for path in sorted(source_paths | test_paths):
        module_id = path_to_module[path]
        for import_rule in descriptor.import_rules:
            for match in _rule_matches(texts[path], import_rule):
                evidence = _source_range(path, texts[path], match.start(), match.end())
                try:
                    target_name = match.group(import_rule.target_group)
                except IndexError as exc:
                    errors.append(_error(StableErrorCode.ANALYSIS_INFRA_ERROR, str(exc), path))
                    continue
                target_module = (
                    None
                    if _enum_value(import_rule.confidence) == EdgeConfidence.Unknown.value
                    else _resolve_module(
                        path, target_name, source_paths, source_modules, descriptor
                    )
                )
                target: ModuleTarget | ExternalTarget | None
                if _enum_value(import_rule.confidence) == EdgeConfidence.Unknown.value:
                    target = None
                    reason = import_rule.reason
                elif target_module is not None:
                    target = ModuleTarget(module_id=target_module)
                    reason = None
                elif target_name in external_packages:
                    target = ExternalTarget(package=target_name)
                    reason = None
                else:
                    target = None
                    reason = UnknownReason.UnresolvedPath
                confidence = import_rule.confidence
                imported_symbols: tuple[str, ...] = ()
                local_symbols: tuple[tuple[str, str], ...] = ()
                if import_rule.symbol_group is not None:
                    try:
                        imported_symbol = match.group(import_rule.symbol_group)
                    except IndexError as exc:
                        errors.append(_error(StableErrorCode.ANALYSIS_INFRA_ERROR, str(exc), path))
                        continue
                    if imported_symbol not in {None, "*"}:
                        local_symbol = imported_symbol
                        local_group = import_rule.local_symbol_group
                        if local_group is None:
                            groups = match.groupdict()
                            local_symbol = (
                                groups.get("alias") or groups.get("local") or local_symbol
                            )
                        else:
                            try:
                                local_symbol = match.group(local_group)
                            except IndexError as exc:
                                errors.append(
                                    _error(StableErrorCode.ANALYSIS_INFRA_ERROR, str(exc), path)
                                )
                                continue
                        imported_symbols = (imported_symbol,)
                        local_symbols = ((imported_symbol, local_symbol),)
                if _enum_value(confidence) == EdgeConfidence.Static.value and target is None:
                    confidence = EdgeConfidence.Unknown
                    reason = reason or UnknownReason.UnresolvedPath
                imports.append(
                    ImportEdge(
                        from_module=module_id,
                        to=target,
                        confidence=confidence,
                        reason=reason,
                        evidence=evidence,
                        imported_symbols=imported_symbols,
                        local_symbols=local_symbols,
                    )
                )

    modules: list[ModuleFact] = []
    boundary = _module_boundary(descriptor.module_boundary_strategy)
    for module_id in sorted(module_files, key=str):
        paths = sorted(module_files[module_id])
        degraded = [
            path
            for path in paths
            if _tree_is_degraded(syntax_trees.get(path), files[path])
        ]
        modules.append(
            ModuleFact(
                module_id=module_id,
                file_paths=[RepoRelativePath(path) for path in paths],
                role=module_roles[module_id],
                boundary=boundary,
                exported_symbols=sorted(
                    exports_by_module[module_id], key=lambda item: (item.symbol, item.kind.value)
                ),
                capability=descriptor.capability,
                degraded_files=[RepoRelativePath(path) for path in degraded],
            )
        )

    test_module_ids = {
        module_id for module_id, role in module_roles.items() if role is ModuleRole.Test
    }
    source_module_ids = {
        module_id for module_id, role in module_roles.items() if role is ModuleRole.Source
    }
    coverage: list[CoverageEntry] = []
    for module_id in sorted(test_module_ids, key=str):
        for path in module_files[module_id]:
            tested = sorted(
                {
                    edge.to.module_id
                    for edge in imports
                    if edge.from_module == module_id
                    and _enum_value(edge.confidence) == EdgeConfidence.Static.value
                    and isinstance(edge.to, ModuleTarget)
                    and edge.to.module_id in source_module_ids
                },
                key=str,
            )
            derivation = CoverageDerivation.ImportGraph
            if not tested and descriptor.test_patterns:
                tested = _directory_coverage(path, source_paths, source_modules)
                if tested:
                    derivation = CoverageDerivation.DirectoryConvention
                else:
                    derivation = CoverageDerivation.Uncovered
            coverage.append(
                CoverageEntry(
                    test_file=RepoRelativePath(path),
                    tested_modules=tested,
                    derivation=derivation,
                )
            )

    coverage_by_source: dict[ProjectModuleId, set[ProjectModuleId]] = defaultdict(set)
    for entry in coverage:
        for module_id in entry.tested_modules:
            coverage_by_source[module_id].add(
                next(path_to_module[path] for path in test_paths if path == entry.test_file)
            )
    coverage_status = [
        ModuleCoverageStatus(
            module=module_id,
            status=(
                ModuleCoverage.Covered
                if coverage_by_source[module_id]
                else ModuleCoverage.EmptyTestSuite
                if descriptor.capability.value == "FULL" and descriptor.test_patterns
                else ModuleCoverage.Undetermined
            ),
        )
        for module_id in sorted(source_module_ids, key=str)
    ]

    conservation: list[TestConservationBaseline] = []
    for module_id in sorted(source_module_ids, key=str):
        test_files = [
            path
            for entry in coverage
            if module_id in entry.tested_modules
            for path in [entry.test_file]
        ]
        source_tests = sum(
            len(_rule_matches(texts[path], rule))
            for path in test_files
            for rule in descriptor.test_function_rules
        )
        source_assertions = sum(
            len(_rule_matches(texts[path], rule))
            for path in test_files
            for rule in descriptor.assertion_rules
        )
        source_loc = sum(
            texts[path].count("\n") + (1 if texts[path] else 0) for path in module_files[module_id]
        )
        conservation.append(
            TestConservationBaseline(
                module=module_id,
                source_tests=source_tests,
                source_assertions=source_assertions,
                source_loc=source_loc,
            )
        )

    artifacts: list[ArtifactFact] = []
    for path in sorted(files):
        for artifact_rule in descriptor.artifact_rules:
            if not descriptor_pattern_matches(artifact_rule.pattern, path):
                continue
            source_path = next(
                (
                    candidate
                    for candidate in sorted(files)
                    if artifact_rule.source_pattern
                    and descriptor_pattern_matches(artifact_rule.source_pattern, candidate)
                ),
                None,
            )
            artifacts.append(
                ArtifactFact(
                    path=RepoRelativePath(path),
                    artifact_kind=artifact_rule.artifact_kind,
                    source_path=(None if source_path is None else RepoRelativePath(source_path)),
                )
            )
            break

    bindings: list[SymbolBinding] = []
    for (module_id, symbol), ranges in sorted(
        export_ranges.items(), key=lambda item: (str(item[0][0]), item[0][1])
    ):
        export = next(item for item in exports_by_module[module_id] if item.symbol == symbol)
        ambiguous = (
            sum(
                len(items)
                for (candidate_module, candidate_symbol), items in export_ranges.items()
                if candidate_symbol == symbol
            )
            > 1
        )
        bindings.extend(
            SymbolBinding(
                symbol=symbol,
                kind=export.kind,
                definition=definition,
                signature_text=export.signature_text,
                ambiguous=ambiguous,
                module=module_id,
            )
            for definition in ranges
        )

    definitions_by_symbol: dict[str, list[SourceRange]] = defaultdict(list)
    for (_export_module_id, symbol), ranges in export_ranges.items():
        definitions_by_symbol[symbol].extend(ranges)

    reference_sites: list[ReferenceSite] = []
    for edge in imports:
        if not isinstance(edge.to, ModuleTarget):
            continue
        target_exports = {export.symbol for export in exports_by_module[edge.to.module_id]}
        symbols = edge.local_symbols or tuple((symbol, symbol) for symbol in edge.imported_symbols)
        source_path = str(edge.evidence.file_path)
        source_text = texts.get(source_path)
        if source_text is None:
            continue
        for symbol, local_symbol in symbols:
            if symbol not in target_exports:
                continue
            definitions = definitions_by_symbol[symbol]
            for start, site in _identifier_ranges(
                source_path, source_text, syntax_trees.get(source_path), local_symbol
            ):
                evidence_start, evidence_end = _range_offsets(source_text, edge.evidence)
                if evidence_start <= start < evidence_end:
                    continue
                reference_sites.append(
                    ReferenceSite(
                        symbol=symbol,
                        site=site,
                        binding=definitions[0] if len(definitions) == 1 else None,
                        ambiguous=len(definitions) != 1,
                    )
                )

    call_edges = [
        CallEdge(symbol=reference.symbol, caller=reference.site, callee=reference.binding)
        for reference in reference_sites
        if reference.binding is not None
        and not reference.ambiguous
        and _is_call_site(
            texts[str(reference.site.file_path)],
            reference.site,
        )
    ]

    symbol_coverage = [
        SymbolCoverageEdge(
            test_site=reference.site, symbol=reference.binding, ambiguous=reference.ambiguous
        )
        for reference in reference_sites
        if reference.binding is not None
        and path_to_module.get(str(reference.site.file_path)) in test_module_ids
    ]
    relation_edges = (
        [
            RelationEdge(
                kind="IMPORT",
                from_module=edge.from_module,
                to_module=edge.to.module_id,
                evidence=edge.evidence,
            )
            for edge in imports
            if isinstance(edge.to, ModuleTarget)
        ]
        + [
            RelationEdge(
                kind="COVERAGE",
                from_module=path_to_module[entry.test_file],
                to_module=tested_module,
            )
            for entry in coverage
            for tested_module in entry.tested_modules
        ]
        + [
            RelationEdge(
                kind="CONTAINS",
                to_module=module_id,
                file_path=RepoRelativePath(path),
            )
            for module_id in sorted(module_files, key=str)
            for path in sorted(module_files[module_id])
        ]
    )
    errors.sort(key=lambda item: (str(item.path or ""), item.message))
    return AnalysisResult(
        snapshot_oid=snapshot.snapshot_oid,
        descriptor_sha256=descriptor.descriptor_sha256,
        capability=descriptor.capability,
        modules=modules,
        imports=sorted(
            imports,
            key=lambda edge: (
                str(edge.from_module),
                edge.evidence.file_path,
                edge.evidence.start.line,
            ),
        ),
        coverage=sorted(coverage, key=lambda entry: str(entry.test_file)),
        coverage_status=coverage_status,
        conservation=conservation,
        manifests=sorted(manifests, key=lambda item: str(item.manifest_path)),
        artifacts=artifacts,
        symbol_bindings=sorted(
            bindings,
            key=lambda binding: (
                binding.symbol,
                str(binding.definition.file_path),
                binding.definition.start.line,
            ),
        ),
        reference_sites=sorted(
            reference_sites,
            key=lambda reference: (
                reference.symbol,
                str(reference.site.file_path),
                reference.site.start.line,
            ),
        ),
        symbol_coverage=symbol_coverage,
        call_edges=sorted(
            call_edges,
            key=lambda edge: (
                edge.symbol,
                str(edge.caller.file_path),
                edge.caller.start.line,
                edge.caller.start.column,
            ),
        ),
        relation_edges=sorted(
            relation_edges, key=lambda edge: (edge.kind, str(edge.from_module), str(edge.to_module))
        ),
        skipped_files=[RepoRelativePath(path) for path in sorted(skipped_files)],
        errors=errors,
    )


__all__ = ["analyze_snapshot"]
