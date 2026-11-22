.DEFAULT_GOAL := help

COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")

.PHONY: help up down test demo-seed smoke record-demo

help:
	@echo "Cachai — Available Make targets:"
	@echo ""
	@echo "  make up           Build and start all services in the background"
	@echo "  make down         Stop and remove running containers"
	@echo "  make test         Run the full agent-core pytest suite via uv"
	@echo "  make demo-seed    Seed deterministic demo data (in container or locally via uv)"
	@echo "  make smoke        Run the end-to-end smoke check against running services"
	@echo "  make record-demo  Record an automated browser demo walkthrough"

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

test:
	cd agent-core && PYTHONPATH=. uv run --no-project --with-requirements requirements.txt --with-requirements requirements-dev.txt pytest -v

demo-seed:
	@CID="$$($(COMPOSE) ps -q agent-core 2>/dev/null)"; \
	if [ -n "$$CID" ] && [ "$$(docker inspect -f '{{.State.Running}}' "$$CID" 2>/dev/null)" = "true" ]; then \
		$(COMPOSE) exec -T agent-core python -m app.cli demo-seed; \
	else \
		cd agent-core && PYTHONPATH=. uv run --no-project --with-requirements requirements.txt python -m app.cli demo-seed; \
	fi

smoke:
	./scripts/e2e_smoke.sh --no-up

record-demo:
	node scripts/record_demo.mjs
