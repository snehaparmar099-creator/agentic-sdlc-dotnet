.PHONY: install demo test test-service clean

install:
	pip install -r requirements.txt

demo:
	python -m agentic demo

test:
	python -m pytest
	cd service && dotnet test

clean:
	rm -rf runs .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
