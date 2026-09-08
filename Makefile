.PHONY: help venv run test lint fmt check serve-tailscale hooks site

help: ## Show this help message
	@grep '## ' $(MAKEFILE_LIST) | grep -v '@grep' | sed 's/.*## //'

venv: ## Create virtual environment and install dev dependencies
	python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

run: ## Run the dispatch server
	.venv/bin/python server.py

test: ## Run pytest suite
	.venv/bin/python -m pytest

lint: ## Check code with ruff
	.venv/bin/ruff check .

fmt: ## Format code with ruff and apply fixes
	.venv/bin/ruff format . && .venv/bin/ruff check --fix .

check: lint test ## Run linter and tests

serve-tailscale: ## Serve dispatch over Tailscale
	tailscale serve --bg 8788

hooks: ## Install shell hooks for statusline and cc-state
	cp examples/statusline.sh examples/cc-state.sh ~/.claude/ && chmod +x ~/.claude/statusline.sh ~/.claude/cc-state.sh

site: ## Serve documentation site locally
	python3 -m http.server -d site 8000
