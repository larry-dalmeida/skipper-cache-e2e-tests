# Single entry point for the cache e2e stack. Run `make all` from this directory.
RUN := .runtime
VENV := $(RUN)/venv
PY := $(VENV)/bin/python
# Clean Skipper checkout to build and run, e.g. make all SKIPPER_CHECKOUT=~/src/skipper
SKIPPER_CHECKOUT ?=

.PHONY: all venv stack start load runs report urls stop chaos

all: venv stack start load

# Project-local Python with Locust; lifecycle.py launches Locust with this interpreter.
venv: $(VENV)/.installed

$(VENV)/.installed: requirements-locust.txt
	@mkdir -p $(RUN)
	python3 -m venv $(VENV)
	$(PY) -m pip install -q -r requirements-locust.txt
	@touch $@

stack:
	./start-stack.sh

# Background supervisors: each must report READY before the next starts.
# Each supervisor's PID is saved so `await` can detect a crash before READY.
start:
	@test -n "$(SKIPPER_CHECKOUT)" || { echo "Set SKIPPER_CHECKOUT=/path/to/clean/skipper"; exit 1; }
	@mkdir -p $(RUN)
	nohup $(PY) lifecycle.py forwards > $(RUN)/forwards-run.out 2>&1 & echo $$! > $(RUN)/forwards.pid
	$(PY) lifecycle.py await forwards
	nohup $(PY) lifecycle.py ui > $(RUN)/ui-run.out 2>&1 & echo $$! > $(RUN)/ui.pid
	$(PY) lifecycle.py await ui
	nohup $(PY) lifecycle.py origin > $(RUN)/origin-run.out 2>&1 & echo $$! > $(RUN)/origin.pid
	$(PY) lifecycle.py await origin
	nohup $(PY) lifecycle.py skipper $(SKIPPER_CHECKOUT) > $(RUN)/skipper-run.out 2>&1 & echo $$! > $(RUN)/skipper.pid
	$(PY) lifecycle.py await skipper
	$(PY) lifecycle.py urls

# Foreground: Ctrl-C stops Locust only. Use `make stop` to stop everything.
# SCENARIO=endless (default, web UI on :8089), smoke (2 min), or soak (30 min); timed runs exit on their own.
SCENARIO ?= endless
load:
	$(PY) lifecycle.py locust $(SCENARIO)

# One line per run under results/runs/ (scenario, requests, failures, p95).
runs:
	@$(PY) lifecycle.py runs

# Timed Locust run with scheduled faults (300 s by default). NAME: origin-stale, valkey-kill, skipper-kill.
# DURATION optionally overrides the length; fault offsets scale with it.
NAME ?=
DURATION ?=
chaos:
	@test -n "$(NAME)" || { echo "Set NAME=origin-stale|valkey-kill|skipper-kill (optional DURATION=seconds)"; exit 1; }
	$(PY) lifecycle.py chaos $(NAME) $(DURATION)

# Regenerates results/runs/<dir>/report.md; RUN is optional and defaults to the latest run.
RUN ?=
report:
	$(PY) lifecycle.py report $(RUN)

urls:
	$(PY) lifecycle.py urls

# Stops local processes and deletes workloads. PVCs (storage.yaml) and the namespace are kept.
stop: $(VENV)/.installed
	$(PY) lifecycle.py stop
	kubectl --context minikube -n cache-e2e delete deployment,service,configmap --all --ignore-not-found
