.PHONY: test demo review serve clean

test:
	python3 -m unittest discover -s tests -v

demo:
	python3 -m bookclub.demo

review:
	python3 -m bookclub.demo --keep --review

serve:
	python3 -m bookclub.api --port 8099 --state /tmp/bookclub.json

clean:
	rm -rf .demo-state
