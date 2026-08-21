# Repository Guidelines

## Project Structure & Module Organization

This repository contains a procurement dashboard, delivery reminder ledger, SPU stock-alert board, self-operated general-merchandise board, Excel purchase-contract generator, order SKU exchange flow, dropship workbook, and a procurement assistant agent. The eight pages are routes of one Vite + React + TypeScript single-page app: sources live in `frontend/src/` (one directory per page under `pages/`, shared API client in `api/`, payload decoding in `data/`, design tokens in `styles/`), and `npm run build` emits `frontend/dist/`, which `backend/app.py` hosts. Python services live under `backend/`; maintenance, training, and workbook-generation entry points live under `scripts/`. Agent Core lives in `backend/agent/`, the pluggable demand-forecast subsystem in `backend/forecast/`, and DingTalk delivery in `backend/dingtalk/`. Local files live under `files/`: `files/config/` (buyers, products, supplier workbook), `files/templates/` (contract master), `files/data/` (logs, sqlite, snapshots, models), `files/outputs/` (generated workbooks). Fixtures are under `tests/fixtures/`, documentation under `docs/`. Treat `frontend/dist/` as a build artifact and never edit it. The retired standalone HTML pages sit in `legacy/`. Supplier master data is `files/config/供应商管理.xlsx` (not imported into MySQL; gitignored).

## Build, Test, and Development Commands

- `npm install && npm run build` checks the payload width contract, typechecks, and bundles the frontend into `frontend/dist/`.
- `npm run dev` starts Vite on `http://127.0.0.1:5177` and proxies `/api` to the Python server, which must be running too.
- `.venv/bin/python server.py` serves the built pages and APIs at `http://127.0.0.1:8777/`.
- `.venv/bin/python scripts/generate_purchase_contract.py --po-id 604264 --invoice-type special_invoice` generates one Excel contract.
- `.venv/bin/python scripts/generate_dropship_workbook.py` refreshes `files/templates/代发订单模板.xlsx` only; `--live` fills today's workbook.
- `.venv/bin/python scripts/seed_users.py` writes the buyer-name analysis report; `--live` reads the MySQL mirror; `--seed` inserts confirmed clusters into Agent SQLite.
- `.venv/bin/python scripts/run_agent_cli.py --status` prints agent, forecast, and DingTalk subsystem state; drop `--status` for an interactive debug session that bypasses HTTP.
- `.venv/bin/python scripts/run_erp_worker.py status` prints Digital Worker config; `login` / `ping` open Playwright against 聚水潭.
- `.venv/bin/python scripts/health_watch.py` GETs `/api/health` and sends a DingTalk alert on database/mirror/Stream/reminder faults; `--dry-run` prints without sending.
- `.venv/bin/python scripts/run_insole_schedule.py` runs one Douyin insole batch now (same as the 09:30–18:30 hourly job); `--status` prints the scheduler only.
- `.venv/bin/python scripts/run_spu_alerts.py` writes `files/outputs/spu/YYMMDD-鞋服SPU总表.xlsx` from the mirror; `--board baihuo` writes the 自营百货 snapshot/`YYMMDD-自营百货总表.xlsx`; `--board all` does both. `--inventory-history N` backfills unchanged styles by walking historical modified windows (the proxy does not forward `i_id`, so per-style `--sync` is blocked). Inventory, sales-out, and aftersales also run at the end of the 60s `sync_all` and must not fail the purchase/order sync.
- `.venv/bin/python scripts/run_production_plan.py --source <订货表.xlsx>` writes `files/outputs/spu/YYMM-生产计划表.xlsx`: roster comes from products tagged 重点产品, demand quantities are read from the staff ordering workbook (never generated), and the mirror supplies stock / in-transit / monthly outbound.
- `.venv/bin/python scripts/run_forecast_prep.py` writes F1 quality / backtest / k_H reports under `FORECAST_EXPORT_DIR/reports` without touching the live model directory.
- `.venv/bin/python scripts/train_forecast_model.py --csv <sales.csv> --forecaster <module:Class>` trains a forecaster and writes a versioned artifact.
- `python3 -m py_compile backend/*.py backend/*/*.py scripts/*.py server.py` performs a quick Python syntax check.

Run commands from the repository root because generator input and output paths are relative.

## Coding Style & Naming Conventions

Use four spaces for Python and follow PEP 8: `snake_case` functions and variables, `UPPER_CASE` constants, UTF-8 source, and short docstrings for transformations. Keep the existing two-space indentation in TypeScript, TSX, and CSS. Use `const` by default, `camelCase` for helpers, `PascalCase` for components, and descriptive CSS class names. Components are function components with hooks; page-level state stays in `useState` because there is no state library. Read colors from the tokens in `frontend/src/styles/base.css` through `cssVar()` instead of hard-coding hex values. Preserve Chinese business terminology and document any change to date or quantity semantics in `README.md`.

## Testing Guidelines

Before submitting, compile Python files, run the offline suites with `.venv/bin/python -m unittest tests.test_agent tests.test_identity tests.test_forecast tests.test_forecast_prep tests.test_delivery_reminders tests.test_exchange tests.test_order_source tests.test_product_images tests.test_realtime_mirror tests.test_gb_standards tests.test_contract_gb tests.test_dingtalk tests.test_codex_oauth tests.test_health_watch tests.test_payload_contract tests.test_http_auth tests.test_contracts tests.test_source_cache tests.test_ops_status tests.test_quality tests.test_supplier_master tests.test_erp tests.test_insole tests.test_dropship tests.test_spu_plan tests.test_purchase_draft`, and run `npm run build` so the frontend passes the payload-width check, `tsc --noEmit` (with `noUnusedLocals`), and bundles. `tests/` is not a package, so `unittest discover` does not work; list the modules explicitly. Live contract assertions against purchase order 604264 are skipped unless `CONTRACT_LIVE_TESTS=1`. There is no frontend test suite yet, so exercise the affected pages in a browser against a running `server.py` and check the console for errors. Contract changes must generate a sample workbook, scan formulas, and visually verify the full contract sheet. For parsing changes, add a focused fixture or assertions before relying on visual checks. New agent tools need coverage in `tests/test_agent.py`, and any L1/L2 tool must be verified through the full pending-action confirm flow rather than direct execution.

## Commit & Pull Request Guidelines

Git history is not included in this workspace, so no repository-specific convention can be inferred. Use short imperative subjects such as `Fix delivery fallback date`. Keep source and documentation changes together, and never commit `frontend/dist/` or `node_modules/`. When an Agent capability is finished, partially finished, added, or dropped, update the progress table in `docs/开发.md` (row status, group subtotal, overall percent, and the changelog). Pull requests should explain the business-rule impact, list verification performed, link the relevant issue, and include before/after screenshots for UI changes. Do not commit credentials, private supplier details, or unrelated CSV exports.
