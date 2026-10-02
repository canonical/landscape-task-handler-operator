# unit tests
.PHONY: test
test:
	uv run tox -e unit

# coverage
.PHONY: coverage
coverage: export PYTHONPATH = $(CURDIR)/lib:$(CURDIR)/src
coverage:
	uv run --group unit coverage run --source=src -m pytest -v --tb native tests/unit
	uv run --group unit coverage report
	uv run --group unit coverage xml

# TiCS expects coverage info to be in ./.cover/cobertura.xml
.PHONY: prepare-tics-analysis
prepare-tics-analysis: coverage
	mkdir -p .cover
	mv coverage.xml .cover/cobertura.xml

# integration tests
.PHONY: integration-test
integration-test:
	uv run tox -e integration

# linting
.PHONY: lint
lint:
	uv run tox -e lint

# formatting
.PHONY: format
format:
	uv run tox -e format

# packing
.PHONY: pack
pack:
	charmcraft pack
