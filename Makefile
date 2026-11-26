.PHONY: run test demo check

run:
	./start.sh

test:
	python3 -m unittest test_server -v

demo:
	./scripts/demo.sh

check:
	python3 -m py_compile server.py test_server.py
