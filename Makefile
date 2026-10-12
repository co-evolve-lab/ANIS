.PHONY: doctor plan test package

doctor:
	./bin/anis-release doctor

plan:
	./bin/anis-release plan --app example --launch example

test:
	python3 -m compileall -q anis stages scripts
	python3 -m unittest discover -s tests -p 'test_*.py' -v

package:
	python3 scripts/package_release.py
