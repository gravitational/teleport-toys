PY ?= .venv/bin/python

.PHONY: setup test test-live lint
setup:
	python3 -m venv .venv
	$(PY) -m pip install -q -r tests/requirements.txt

# Hermetic: fake LLM, fake `tsh`, simulated robot, embedded Postgres, fake Redis. No cluster needed.
test:
	PYTHONDONTWRITEBYTECODE=1 $(PY) -m pytest

# Opt-in, inside a real beam (or any logged-in tsh): proves MCP, databases and the app against real Teleport.
test-live:
	BEAMS_LIVE=1 PYTHONDONTWRITEBYTECODE=1 $(PY) -m pytest tests/live -v

lint:
	@for f in $$(find . -name '*.sh'); do bash -n $$f || exit 1; done; echo "shell syntax ok"
