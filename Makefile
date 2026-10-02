# unit tests
.PHONY: test
test:
	uv run tox -e unit

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
