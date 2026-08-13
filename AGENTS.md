# Repository Guidelines

## Project Structure & Module Organization

This repository contains a procurement dashboard, delivery reminder ledger, Excel purchase-contract generator, order SKU exchange flow, and a procurement assistant agent. The five pages are routes of one Vite + React + TypeScript single-page app: sources live in `frontend/src/` (one directory per page under `pages/`, shared API client in `api/`, payload decoding in `data/`, design tokens in `styles/`), and `npm run build` emits `frontend/dist/`, which `backend/app.py` hosts. Python services live under `backend/`; maintenance, training, and workbook-generation entry points live under `scripts/`. Agent Core lives in `backend/agent/`, the pluggable demand-forecast subsystem in `backend/forecast/`, and DingTalk delivery in `backend/dingtalk/`. Source snapshots are under `data/snapshots/`, fixtures under `tests/fixtures/`, documentation under `docs/`, and the contract master under `templates/`. Treat `frontend/dist/` as a build artifact and never edit it. The retired standalone HTML pages and the offline snapshot generators sit in `legacy/` for reference only. Supplier, buyer, product-image, and invoice-price mappings live under `config/`; trained forecast artifacts land in `data/models/` and stay out of version control.

## Build, Test, and Development Commands

- `npm install && npm run build` typechecks and bundles the frontend into `frontend/dist/`.
- `npm run dev` starts Vite on `http://127.0.0.1:5177` and proxies `/api` to the Python server, which must be running too.
- `.venv/bin/python server.py` serves the built pages and APIs at `http://127.0.0.1:8777/`.
- `.venv/bin/python scripts/generate_purchase_contract.py --po-id 604264 --invoice-type special_invoice` generates one Excel contract.
- `.venv/bin/python scripts/run_agent_cli.py --status` prints agent, forecast, and DingTalk subsystem state; drop `--status` for an interactive debug session that bypasses HTTP.
- `.venv/bin/python scripts/train_forecast_model.py --csv <sales.csv> --forecaster <module:Class>` trains a forecaster and writes a versioned artifact.
- `python3 -m py_compile backend/*.py backend/*/*.py scripts/*.py server.py` performs a quick Python syntax check.

Run commands from the repository root because generator input and output paths are relative.

## Coding Style & Naming Conventions

Use four spaces for Python and follow PEP 8: `snake_case` functions and variables, `UPPER_CASE` constants, UTF-8 source, and short docstrings for transformations. Keep the existing two-space indentation in TypeScript, TSX, and CSS. Use `const` by default, `camelCase` for helpers, `PascalCase` for components, and descriptive CSS class names. Components are function components with hooks; page-level state stays in `useState` because there is no state library. Read colors from the tokens in `frontend/src/styles/base.css` through `cssVar()` instead of hard-coding hex values. Preserve Chinese business terminology and document any change to date or quantity semantics in `README.md`.

## Testing Guidelines

Before submitting, compile Python files, run the offline suites with `.venv/bin/python -m unittest tests.test_agent tests.test_forecast tests.test_delivery_reminders tests.test_exchange tests.test_dingtalk tests.test_codex_oauth tests.test_gb_standards tests.test_contract_gb`, and run `npm run build` so the frontend passes `tsc --noEmit` (with `noUnusedLocals`) and bundles. `tests/` is not a package, so `unittest discover` does not work; list the modules explicitly. `tests/test_contracts.py` is the one suite that connects to the live database and needs credentials. There is no frontend test suite yet, so exercise the affected pages in a browser against a running `server.py` and check the console for errors. Contract changes must generate a sample workbook, scan formulas, and visually verify the full contract sheet. For parsing changes, add a focused fixture or assertions before relying on visual checks. New agent tools need coverage in `tests/test_agent.py`, and any L1/L2 tool must be verified through the full pending-action confirm flow rather than direct execution.

## Commit & Pull Request Guidelines

Git history is not included in this workspace, so no repository-specific convention can be inferred. Use short imperative subjects such as `Fix delivery fallback date`. Keep source and documentation changes together, and never commit `frontend/dist/` or `node_modules/`. Pull requests should explain the business-rule impact, list verification performed, link the relevant issue, and include before/after screenshots for UI changes. Do not commit credentials, private supplier details, or unrelated CSV exports.
