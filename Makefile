# BirdHeatmap Makefile
# ────────────────────────────────────────────────────────────────────────────
# Variables — override on the command line, e.g.:
#   make deploy DEPLOY_HOST=192.168.123.200
# ────────────────────────────────────────────────────────────────────────────
PYTHON     := python3
VENV       := .venv
PIP        := $(VENV)/bin/pip
PY         := $(VENV)/bin/python

DEPLOY_HOST   := 192.168.123.200
DEPLOY_USER   := jordan
DEPLOY_REMOTE := $(DEPLOY_USER)@$(DEPLOY_HOST)
REMOTE_REPO   := /tmp/birdheatmap-deploy

# ────────────────────────────────────────────────────────────────────────────
# Local development
# ────────────────────────────────────────────────────────────────────────────

.PHONY: install
install: ## Create local dev venv and install package in editable mode
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e ".[dev]"
	@echo "Venv ready.  Activate with: source $(VENV)/bin/activate"

.PHONY: update-deps
update-deps: ## Regenerate requirements.lock from pyproject.toml (run after changing dependencies)
	$(VENV)/bin/pip-compile --generate-hashes --output-file requirements.lock pyproject.toml

.PHONY: sync
sync: ## Run a one-shot sync (backfill if needed, then incremental)
	$(PY) -m birdheatmap sync

.PHONY: sync-dry
sync-dry: ## Fetch 2 pages and print raw API response without writing to DB
	$(PY) -m birdheatmap sync --dry-run

.PHONY: serve
serve: ## Start the local web server
	$(PY) -m birdheatmap serve

.PHONY: render
render: ## Render a sample PNG (requires SPECIES and YEAR env vars)
	$(PY) -m birdheatmap render \
	    --plot annual_heatmap \
	    --species "$(SPECIES)" \
	    --year $(YEAR) \
	    --out samples/render_$(YEAR).png

.PHONY: plots
plots: ## List registered plot types
	$(PY) -m birdheatmap plots

.PHONY: species
species: ## List species in the local cache
	$(PY) -m birdheatmap species

.PHONY: test
test: ## Run the test suite
	$(VENV)/bin/pytest tests/ -v

# ────────────────────────────────────────────────────────────────────────────
# Deployment
# ────────────────────────────────────────────────────────────────────────────

.PHONY: deploy-local
deploy-local: ## Install/upgrade directly on this machine (run when already on the server)
	sudo bash deploy/install.sh

.PHONY: deploy
deploy: ## tar-pipe repo to server and run install.sh over SSH (no local rsync needed)
	@echo "Deploying to $(DEPLOY_REMOTE) …"
	tar czf - \
	    --exclude='./.venv' \
	    --exclude='./.git' \
	    --exclude='./samples' \
	    --exclude='./dev_data' \
	    --exclude='./__pycache__' \
	    --exclude='*.pyc' \
	    --exclude='*.egg-info' \
	    . | ssh $(DEPLOY_REMOTE) \
	        'rm -rf $(REMOTE_REPO) && mkdir -p $(REMOTE_REPO) && tar xzf - -C $(REMOTE_REPO)'
	ssh -t $(DEPLOY_REMOTE) "sudo bash $(REMOTE_REPO)/deploy/install.sh"

# ────────────────────────────────────────────────────────────────────────────
# Housekeeping
# ────────────────────────────────────────────────────────────────────────────

.PHONY: clean
clean: ## Remove build artifacts and caches
	rm -rf $(VENV) build dist *.egg-info src/*.egg-info __pycache__
	find . -name '*.pyc' -delete
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	    awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
