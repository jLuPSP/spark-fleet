VENV ?= .venv
PYTHON ?= python3
PY := $(shell [ -x $(VENV)/bin/python ] && echo $(VENV)/bin/python || echo $(VENV)/Scripts/python.exe)

.PHONY: setup validate test render-pairs render-switched stage-vars-pairs stage-vars-switched lint check

setup:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -r requirements-ci.txt

validate:
	$(PY) scripts/validate.py --require-digests \
		--profile clusters/dgx-spark/cluster.yaml
	$(PY) scripts/validate.py --require-digests \
		--profile clusters/dgx-spark-switched/cluster.yaml

test:
	$(PY) -m pytest tests/unit -q -s

render-pairs:
	$(PY) scripts/render_kubernetes.py \
		--profile clusters/dgx-spark/cluster.yaml \
		--auth-mode prod --auth gateway/auth.prod.yml.example \
		--output build/kubernetes/dgx-spark-pairs

render-switched:
	$(PY) scripts/render_kubernetes.py \
		--profile clusters/dgx-spark-switched/cluster.yaml \
		--auth-mode prod --auth gateway/auth.prod.yml.example \
		--output build/kubernetes/dgx-spark-switched

stage-vars-pairs:
	$(PY) scripts/render_stage_vars.py \
		--profile clusters/dgx-spark/cluster.yaml \
		--output build/model-stage-pairs.yml

stage-vars-switched:
	$(PY) scripts/render_stage_vars.py \
		--profile clusters/dgx-spark-switched/cluster.yaml \
		--output build/model-stage-switched.yml

lint:
	$(VENV)/bin/ansible-lint playbooks/

check: validate test lint
