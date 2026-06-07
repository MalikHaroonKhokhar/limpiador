.PHONY: help test test-unit test-integration test-e2e test-reproduce eval \
        test-all test-coverage test-coverage-report test-fast test-failed \
        test-specific test-e2e-check run run-sandbox dev-mock demo clean install setup verify

# ============================================================================
# Virtual Environment Setup
# ============================================================================
VENV_PATH := venv
VENV_BIN  := $(VENV_PATH)/bin
PYTHON    := $(VENV_BIN)/python
PYTEST    := $(PYTHON) -m pytest

# ============================================================================
# Run-mode config (limpiador is a CLI, not a server — no ports, just modes)
# ============================================================================
# MOCK MODE: deterministic mock LLM, temp git fixtures, no network, free.
# REAL MODE: real OpenAI + real git/github. Costs credits. E2E + evals only.
MOCK_ENV := LIMPIADOR_LLM=mock
REAL_ENV := LIMPIADOR_LLM=openai

# E2E / eval real-mode requirements (throwaway repo, never a real one)
#   OPENAI_API_KEY          - real model access
#   GITHUB_TOKEN            - auth for the throwaway repo
#   LIMPIADOR_SANDBOX_REPO  - the throwaway repo, e.g. you/limpiador-sandbox
# If any is missing, E2E skips gracefully instead of failing or touching prod.

# Load .env (git-ignored) if present, and export its credentials to recipe
# environments, so `make run` / `make eval` / `make test-e2e` pick up
# OPENAI_API_KEY, GITHUB_TOKEN, and LIMPIADOR_SANDBOX_REPO without the developer
# exporting them by hand. A missing .env is fine — mock-mode targets and the
# default `make test` never need it. (Run mode stays controlled per-target by
# MOCK_ENV / REAL_ENV, so LIMPIADOR_LLM is deliberately not exported here.)
-include .env
export OPENAI_API_KEY GITHUB_TOKEN LIMPIADOR_SANDBOX_REPO
# Export TASK too, so recipes read it from the environment as "$$TASK" rather
# than expanding $(TASK) into the shell line — a free-text task with quotes or
# newlines then can't break the recipe's shell.
export TASK

# Default target
help:
	@echo ""
	@echo "════════════════════════════════════════════════════════════════"
	@echo "🧹 limpiador — Git Maintenance Agent · Command Reference"
	@echo "════════════════════════════════════════════════════════════════"
	@echo ""
	@echo "🎯 RECOMMENDED:"
	@echo "  make test              - ⚡ Default suite: unit + mock-integration"
	@echo "                           (mock model, no network, free, CI-safe)"
	@echo "  make test-all          - Full pyramid incl. real-mode E2E + evals (costs \$$)"
	@echo ""
	@echo "🧪 Test Pyramid (cheapest → most expensive):"
	@echo "  make test-unit         - Layer 1: pure logic, mock model, no network (<1s)"
	@echo "  make test-integration  - Layer 2: full loop on mock model, temp git repos"
	@echo "  make test-e2e          - Layer 3: REAL OpenAI + REAL git/github on THROWAWAY repo"
	@echo "  make test-reproduce    - Reproduction tests: one per reported behavior (real)"
	@echo ""
	@echo "🔬 Agent Reasoning (separate concern from the test suite):"
	@echo "  make eval              - Run eval harness: agent vs seeded fixtures (real, \$$)"
	@echo ""
	@echo "📊 Coverage & Advanced:"
	@echo "  make test-coverage     - Coverage report (mock-mode suite)"
	@echo "  make test-coverage-report - Open coverage report in browser"
	@echo "  make test-fast         - Parallel execution of the mock-mode suite"
	@echo "  make test-failed       - Re-run only last failed tests"
	@echo "  make test-specific TEST=path/to/test.py - Run one test file"
	@echo ""
	@echo "🚀 Running the Agent:"
	@echo "  make run REPO=. TASK=\"...\"  - Run limpiador against a repo (REAL mode)"
	@echo "  make dev-mock REPO=. TASK=\"...\" - Run against the mock model (free, deterministic)"
	@echo "  make demo              - Run the scripted demo scenario"
	@echo ""
	@echo "🧹 Maintenance:"
	@echo "  make install           - Install dependencies into venv"
	@echo "  make setup             - Full dev environment setup"
	@echo "  make verify            - Verify config + E2E prerequisites"
	@echo "  make clean             - Remove caches, temp fixtures, trace artifacts"
	@echo ""
	@echo "💡 Pro Tips:"
	@echo "  • Run 'make test' before every commit (free, fast, deterministic)."
	@echo "  • Run 'make test-e2e' / 'make eval' deliberately — they cost credits."
	@echo "  • E2E + evals run ONLY in REAL mode; mechanics run ONLY in MOCK mode."
	@echo "  • E2E touches the THROWAWAY repo only. Never point it at a real repo."
	@echo ""
	@echo "📚 Documentation:"
	@echo "  • ARCHITECTURE.md  - full system design (the map)"
	@echo "  • .clauderules     - agent-development guardrails + run modes"
	@echo "  • CLEAN_CODE.md    - code-style contract"
	@echo "  • MEMO.md          - the one-page brief deliverable"
	@echo ""

# ============================================================================
# Test Pyramid — Layer 1 & 2 (MOCK mode: free, deterministic, CI-safe)
# ============================================================================

# Default: the fast, free, deterministic suite. This is what runs in CI and
# before every commit. No real model, no network, no credits spent.
test:
	@echo "⚡ =========================================="
	@echo "⚡ limpiador DEFAULT SUITE (unit + mock-integration)"
	@echo "⚡ =========================================="
	@echo "   Mode: MOCK · no network · no credits"
	@echo ""
	@$(MAKE) test-unit
	@echo ""
	@$(MAKE) test-integration
	@echo ""
	@echo "✅ Default suite complete."

test-unit:
	@echo "🧪 Layer 1: Unit tests (pure logic, mock model, no network)..."
	@$(MOCK_ENV) $(PYTEST) tests/unit/ -v --tb=short
	@echo "✅ Unit tests complete."

test-integration:
	@echo "🔗 Layer 2: Mock-integration tests (full loop, temp git repos)..."
	@$(MOCK_ENV) $(PYTEST) tests/integration/ -m "not e2e" -v --tb=short
	@echo "✅ Mock-integration tests complete."

# ============================================================================
# Test Pyramid — Layer 3 (REAL mode: E2E on a THROWAWAY repo, costs credits)
# ============================================================================

# Guard: verify real-mode prerequisites before spending a credit or touching
# any repo. Skips gracefully (exit 0) when unconfigured so CI stays green.
test-e2e-check:
	@if [ -z "$$OPENAI_API_KEY" ]; then \
		echo "⏭️  SKIP E2E: OPENAI_API_KEY not set."; exit 0; fi
	@if [ -z "$$GITHUB_TOKEN" ]; then \
		echo "⏭️  SKIP E2E: GITHUB_TOKEN not set."; exit 0; fi
	@if [ -z "$$LIMPIADOR_SANDBOX_REPO" ]; then \
		echo "⏭️  SKIP E2E: LIMPIADOR_SANDBOX_REPO not set (throwaway repo required)."; exit 0; fi
	@echo "✅ E2E prerequisites present. Target repo: $$LIMPIADOR_SANDBOX_REPO"

test-e2e: test-e2e-check
	@echo "🌐 =========================================="
	@echo "🌐 Layer 3: E2E (REAL OpenAI + REAL git/github)"
	@echo "🌐 =========================================="
	@if [ -z "$$LIMPIADOR_SANDBOX_REPO" ]; then \
		echo "⏭️  E2E skipped (see above)."; exit 0; fi
	@echo "⚠️  Running against THROWAWAY repo: $$LIMPIADOR_SANDBOX_REPO"
	@echo "⚠️  This spends real credits and writes to that repo. Keep count LOW."
	@echo ""
	@$(REAL_ENV) $(PYTEST) tests/integration/ -m e2e -v --tb=short
	@echo "✅ E2E tests complete."

# Reproduction tests: one per reported behavior, real model (TDD red → green).
test-reproduce: test-e2e-check
	@echo "🐛 Reproduction tests (REAL model, matches actual behavior)..."
	@if [ -z "$$OPENAI_API_KEY" ]; then \
		echo "⏭️  Reproduce skipped: OPENAI_API_KEY not set."; exit 0; fi
	@$(REAL_ENV) $(PYTEST) tests/reproduce/ -v --tb=short
	@echo "✅ Reproduction tests complete."

# ============================================================================
# Eval Harness — does the AGENT reason well? (separate concern from tests)
# ============================================================================

eval: test-e2e-check
	@echo "🔬 =========================================="
	@echo "🔬 EVAL HARNESS (agent reasoning on seeded fixtures)"
	@echo "🔬 =========================================="
	@if [ -z "$$OPENAI_API_KEY" ]; then \
		echo "⏭️  Evals skipped: OPENAI_API_KEY not set."; exit 0; fi
	@echo "   Asserts on OUTCOME (goal achieved) and TRACE (reasoned correctly)."
	@echo "   Fresh fixture checkout per case. Costs credits — keep count LOW."
	@echo ""
	@$(REAL_ENV) $(PYTHON) -m evals.harness
	@echo "✅ Eval run complete. See evals/report output."

# ============================================================================
# Full pyramid — everything, including the real-mode tiers (costs credits)
# ============================================================================

test-all:
	@echo "🎯 Running FULL pyramid: unit + integration + E2E + reproduce + evals"
	@echo "   ⚠️  The real-mode tiers spend credits and hit the throwaway repo."
	@echo ""
	@$(MAKE) test-unit
	@$(MAKE) test-integration
	@$(MAKE) test-e2e
	@$(MAKE) test-reproduce
	@$(MAKE) eval
	@echo ""
	@echo "✅ Full pyramid complete."

# ============================================================================
# Coverage & Advanced
# ============================================================================

test-coverage:
	@echo "📊 Running mock-mode suite with coverage..."
	@$(MOCK_ENV) $(PYTEST) tests/unit/ tests/integration/ -m "not e2e" \
		--cov=src/limpiador --cov-report=html --cov-report=term
	@echo ""
	@echo "📊 Coverage report generated: htmlcov/index.html"

test-coverage-report:
	@echo "📊 Opening coverage report..."
	@open htmlcov/index.html || xdg-open htmlcov/index.html 2>/dev/null || \
		echo "No coverage report found. Run: make test-coverage"

test-fast:
	@echo "⚡ Running mock-mode suite in parallel..."
	@$(MOCK_ENV) $(PYTEST) tests/unit/ tests/integration/ -m "not e2e" -n auto

test-failed:
	@echo "🔄 Re-running last failed tests..."
	@$(MOCK_ENV) $(PYTEST) --lf -v

test-specific:
	@test -n "$(TEST)" || (echo "❌ Error: TEST not specified." && \
		echo "Usage: make test-specific TEST=tests/unit/test_registry.py" && exit 1)
	@echo "🎯 Running specific test: $(TEST)"
	@$(MOCK_ENV) $(PYTEST) $(TEST) -v

# ============================================================================
# Running the Agent
# ============================================================================

run:
	@test -n "$(REPO)" || (echo "❌ Error: REPO not specified." && \
		echo "Usage: make run REPO=. TASK=\"fix the failing test\"" && exit 1)
	@test -n "$$TASK" || (echo "❌ Error: TASK not specified." && \
		echo "Usage: make run REPO=. TASK=\"fix the failing test\"" && exit 1)
	@echo "🚀 Running limpiador (REAL mode) on $(REPO)..."
	@$(REAL_ENV) $(PYTHON) -m limpiador.cli run --repo "$(REPO)" --task "$$TASK"

# The default tool-call ceiling for a sandbox run (override: MAX_CALLS=120).
# A multi-step task (branch → edit → commit → push → PR) pays a discover+load
# tool call for each capability before using it, so the ceiling needs headroom.
MAX_CALLS ?= 100

# The model the sandbox pins for *every* turn (override: MODEL=gpt-4o-mini).
# Production routing sends each turn that follows a tool result — i.e. the whole
# branch → commit → push → PR execution chain — to the cheap model, which cannot
# reliably carry that many sequential steps and bails out early. The sandbox is a
# verification harness, not the cost-optimised path, so it pins the strong model
# through the documented --model escape hatch to exercise the flow end to end.
MODEL ?= gpt-4o

# Run the agent against the THROWAWAY sandbox repo (from LIMPIADOR_SANDBOX_REPO),
# without needing REPO: it clones the sandbox into a temp dir and runs there, so
# pushes and PRs land on the throwaway, never a real repo. Real mode — costs
# credits. Usage: make run-sandbox TASK="add a test file and open a PR"
run-sandbox:
	@test -n "$$TASK" || (echo "❌ Error: TASK not specified." && \
		echo "Usage: make run-sandbox TASK=\"add a test file and open a PR\"" && exit 1)
	@test -n "$$LIMPIADOR_SANDBOX_REPO" || (echo "❌ LIMPIADOR_SANDBOX_REPO not set (the throwaway repo)." && exit 1)
	@test -n "$$GITHUB_TOKEN" || (echo "❌ GITHUB_TOKEN not set." && exit 1)
	@test -n "$$OPENAI_API_KEY" || (echo "❌ OPENAI_API_KEY not set." && exit 1)
	@echo "🧪 Running limpiador (REAL mode) against the SANDBOX: $$LIMPIADOR_SANDBOX_REPO"
	@set -e; \
	  work=$$(mktemp -d /tmp/limpiador-sandbox.XXXXXX); \
	  echo "   cloning $$LIMPIADOR_SANDBOX_REPO into $$work/repo ..."; \
	  git clone -q "https://x-access-token:$$GITHUB_TOKEN@github.com/$$LIMPIADOR_SANDBOX_REPO.git" "$$work/repo"; \
	  git -C "$$work/repo" config user.name "limpiador agent"; \
	  git -C "$$work/repo" config user.email "agent@limpiador.local"; \
	  $(REAL_ENV) GITHUB_REPOSITORY="$$LIMPIADOR_SANDBOX_REPO" \
	    $(PYTHON) -m limpiador.cli run --repo "$$work/repo" --task "$$TASK" --max-calls $(MAX_CALLS) --model $(MODEL) --trace; \
	  if printf '%s' "$$TASK" | grep -Eiq 'pull request|(^|[^[:alpha:]])pr([^[:alpha:]]|$$)'; then \
	    base=$$(git -C "$$work/repo" symbolic-ref --short refs/remotes/origin/HEAD | sed 's#^origin/##'); \
	    verified=0; \
	    for head in $$(git -C "$$work/repo" for-each-ref --format='%(refname:short)' refs/heads); do \
	      test "$$head" = "$$base" && continue; \
	      count=$$(gh pr list --repo "$$LIMPIADOR_SANDBOX_REPO" --head "$$head" --base "$$base" --state open --json number --jq length 2>/dev/null || echo 0); \
	      if test "$$count" -gt 0; then \
	        echo "   verified open PR for $$head -> $$base"; \
	        verified=1; \
	        break; \
	      fi; \
	    done; \
	    test "$$verified" = 1 || (echo "❌ Expected an open PR, but none was found on $$LIMPIADOR_SANDBOX_REPO." && exit 1); \
	  fi; \
	  echo "   (agent's checkout left at $$work/repo for inspection — rm -rf when done)"

dev-mock:
	@test -n "$(REPO)" || (echo "❌ Error: REPO not specified." && \
		echo "Usage: make dev-mock REPO=. TASK=\"...\"" && exit 1)
	@test -n "$(TASK)" || (echo "❌ Error: TASK not specified." && \
		echo "Usage: make dev-mock REPO=. TASK=\"...\"" && exit 1)
	@echo "🧪 Running limpiador (MOCK mode, free) on $(REPO)..."
	@PYTHONPATH=tests $(MOCK_ENV) $(PYTHON) -m support run --repo "$(REPO)" --task "$(TASK)"

demo:
	@echo "🎬 Running scripted demo scenario..."
	@./scripts/demo_run.sh || echo "Demo script not found at scripts/demo_run.sh"

# ============================================================================
# Maintenance / Setup
# ============================================================================

install:
	@echo "📦 Installing dependencies into venv..."
	@test -d $(VENV_PATH) || python3 -m venv $(VENV_PATH)
	@$(PYTHON) -m pip install --upgrade pip
	@$(PYTHON) -m pip install -e ".[dev]"
	@echo "✅ Dependencies installed."

setup: install
	@echo "🔧 Setting up development environment..."
	@cp .env.example .env 2>/dev/null || echo ".env already exists"
	@echo "✅ Setup complete."
	@echo ""
	@echo "Next steps:"
	@echo "  1. Edit .env (OPENAI_API_KEY, GITHUB_TOKEN, LIMPIADOR_SANDBOX_REPO)"
	@echo "  2. Run: make test        (free, mock mode)"
	@echo "  3. Run: make eval        (real mode, costs credits)"

verify:
	@echo "🔍 Verifying configuration..."
	@$(PYTHON) --version || echo "⚠️  venv not set up — run: make install"
	@echo ""
	@echo "E2E / eval prerequisites (REAL mode):"
	@if [ -n "$$OPENAI_API_KEY" ]; then echo "  ✅ OPENAI_API_KEY set"; \
		else echo "  ⏭️  OPENAI_API_KEY missing"; fi
	@if [ -n "$$GITHUB_TOKEN" ]; then echo "  ✅ GITHUB_TOKEN set"; \
		else echo "  ⏭️  GITHUB_TOKEN missing"; fi
	@if [ -n "$$LIMPIADOR_SANDBOX_REPO" ]; then echo "  ✅ LIMPIADOR_SANDBOX_REPO=$$LIMPIADOR_SANDBOX_REPO"; \
		else echo "  ⏭️  LIMPIADOR_SANDBOX_REPO missing (throwaway repo)"; fi

clean:
	@echo "🧹 Cleaning caches, temp fixtures, and trace artifacts..."
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache htmlcov .coverage 2>/dev/null || true
	@rm -rf /tmp/limpiador-fixtures-* 2>/dev/null || true
	@echo "✅ Clean complete."
