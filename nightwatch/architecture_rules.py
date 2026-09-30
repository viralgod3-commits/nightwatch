"""Architecture boundary checks for the Nightwatch modular monolith."""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent


def _absolute_import(module: str | None, level: int, file: Path) -> str:
    if level <= 0:
        return module or ""
    relative = file.relative_to(PACKAGE_ROOT).with_suffix("")
    package_parts = ["nightwatch", *relative.parts[:-1]]
    keep = max(0, len(package_parts) - level + 1)
    prefix = package_parts[:keep]
    if module:
        prefix.extend(module.split("."))
    return ".".join(prefix)


def _tree(file: Path) -> ast.AST:
    return ast.parse(file.read_text(encoding="utf-8"), filename=str(file))


def _imports(file: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(_tree(file)):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.lineno, _absolute_import(node.module, node.level, file)))
    return found


def _forbid(file: Path, prefixes: tuple[str, ...], reason: str) -> list[str]:
    if not file.exists():
        return [f"required architecture file missing: {file.relative_to(PACKAGE_ROOT)}"]
    errors: list[str] = []
    for line, imported in _imports(file):
        if any(imported == prefix or imported.startswith(prefix + ".") for prefix in prefixes):
            errors.append(f"{file.relative_to(PACKAGE_ROOT)}:{line}: {reason}: {imported}")
    return errors


def _class_names(file: Path) -> set[str]:
    return {
        node.name
        for node in ast.walk(_tree(file))
        if isinstance(node, ast.ClassDef)
    }


def _python_files() -> list[Path]:
    return sorted(
        path for path in PACKAGE_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _retired_feature_checks() -> list[str]:
    errors: list[str] = []
    retired_paths = (
        "sector_overview.py", "app/composition.py", "leadership_cache.py",
        "contracts.py", "diagnostics.py", "chart/analysis_process.py",
        "chart/overlay.py",
        "orderbook/runtime.py", "trading/controller.py",
        "trading/rail_execution.py", "networking/core.py",
        "sound_system.py", "ui/data_tools.py", "market/microstructure.py",
    )
    for rel in retired_paths:
        if (PACKAGE_ROOT / rel).exists():
            errors.append(f"retired path must not exist: {rel}")
    for file in _python_files():
        for line, imported in _imports(file):
            if any(imported == "nightwatch." + rel[:-3].replace("/", ".") for rel in retired_paths):
                errors.append(f"{file.relative_to(PACKAGE_ROOT)}:{line}: retired import: {imported}")
    return errors


def _leadership_checks() -> list[str]:
    leadership = PACKAGE_ROOT / "leadership.py"
    if not leadership.exists():
        return ["leadership.py is required"]
    classes = _class_names(leadership)
    required = {"LeadershipTimelineWidget", "SectorOverviewWidget"}
    missing = sorted(required - classes)
    return [f"leadership.py must own {name}" for name in missing]


def _diagnostics_checks() -> list[str]:
    """Diagnostics must stay behind the command-line opt-in."""
    errors: list[str] = []
    entry_source = (PACKAGE_ROOT / "entrypoint.py").read_text(encoding="utf-8")
    if "--diagnostics" not in entry_source or "if arguments.diagnostics:" not in entry_source:
        errors.append("entrypoint.py must expose --diagnostics opt-in")
    utilities_source = (PACKAGE_ROOT / "utilities.py").read_text(encoding="utf-8")
    if "_DIAGNOSTICS: DiagnosticsHub | None = None" not in utilities_source:
        errors.append("utilities.py must initialize diagnostics on demand")
    return errors


def check() -> list[str]:
    errors: list[str] = []

    legacy_paths = [
        "features", "domain", "services", "infrastructure",
        "main_window.py", "chart.py", "market_data.py", "microstructure.py",
        "trading_ui.py", "trading_gateway.py", "orders.py", "right_rail.py",
        "shell_dialogs.py", "market_widgets.py", "networking.py", "themes.py",
        "binance_rest.py", "orderbook_v2.py",
    ]
    for rel in legacy_paths:
        if (PACKAGE_ROOT / rel).exists():
            errors.append(f"legacy path must not exist: {rel}")

    errors += _retired_feature_checks()
    errors += _leadership_checks()
    errors += _diagnostics_checks()

    contracts = PACKAGE_ROOT / "models.py"
    errors += _forbid(
        contracts,
        ("PySide6", "nightwatch.chart", "nightwatch.orderbook", "nightwatch.trading", "nightwatch.market", "nightwatch.ui", "nightwatch.networking", "nightwatch.app"),
        "models and contracts must remain framework/implementation free",
    )

    errors += _forbid(PACKAGE_ROOT / "trading" / "trading_ui.py", ("nightwatch.trading.gateway", "nightwatch.networking", "nightwatch.app", "nightwatch.orderbook.backend"), "trading UI must use ports/controller abstractions")
    errors += _forbid(PACKAGE_ROOT / "trading" / "orders.py", ("nightwatch.trading.gateway", "nightwatch.networking", "nightwatch.app", "nightwatch.ui"), "order construction and rail amendments must not import transport or shell code")

    errors += _forbid(PACKAGE_ROOT / "orderbook" / "orderbook_ui.py", ("nightwatch.orderbook.backend", "nightwatch.market.data", "nightwatch.networking", "nightwatch.trading.gateway", "nightwatch.app"), "orderbook frontend must consume contracts only")
    errors += _forbid(PACKAGE_ROOT / "orderbook" / "backend.py", ("nightwatch.orderbook.orderbook_ui", "nightwatch.ui", "nightwatch.trading", "nightwatch.networking", "nightwatch.app", "nightwatch.theme"), "orderbook backend must not depend on UI or trading code")

    errors += _forbid(PACKAGE_ROOT / "chart" / "rendering.py", ("nightwatch.chart.workspace", "nightwatch.market", "nightwatch.networking", "nightwatch.trading", "nightwatch.app"), "chart rendering must be independently replaceable")
    errors += _forbid(PACKAGE_ROOT / "chart" / "magnetic_rail.py", ("nightwatch.chart.workspace", "nightwatch.market", "nightwatch.networking", "nightwatch.trading", "nightwatch.app"), "order rail must emit UI intent without execution coupling")

    errors += _forbid(PACKAGE_ROOT / "ui" / "panels.py", ("nightwatch.chart", "nightwatch.orderbook", "nightwatch.trading", "nightwatch.market", "nightwatch.networking", "nightwatch.app"), "panel composition must stay generic")
    errors += _forbid(PACKAGE_ROOT / "theme.py", ("PySide6", "nightwatch.app", "nightwatch.market", "nightwatch.networking", "nightwatch.trading", "nightwatch.orderbook", "nightwatch.chart"), "theme service must remain presentation-token only")

    main_window = PACKAGE_ROOT / "app" / "main_window.py"
    errors += _forbid(main_window, ("nightwatch.trading.gateway", "nightwatch.market.data"), "main window must receive concrete runtime services through composition")
    return errors


def main() -> int:
    errors = check()
    if errors:
        print("Architecture boundary violations:")
        for error in errors:
            print(f" - {error}")
        return 1
    print("Architecture boundary check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
