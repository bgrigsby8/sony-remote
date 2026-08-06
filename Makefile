.PHONY: help setup ext test lint build publish clean

VENV    ?= venv
PYTHON  ?= $(VENV)/bin/python
PIP     ?= $(VENV)/bin/pip

help:
	@echo "sony-remote - Viam camera module for the Sony Camera Remote SDK"
	@echo
	@echo "  make setup    create the venv and install Python dependencies"
	@echo "  make ext      build the _crsdk extension (needs CRSDK_ROOT)"
	@echo "  make test     run the hardware-free test suite"
	@echo "  make lint     compile-check every source file"
	@echo "  make build    produce dist/archive.tar.gz for the registry"
	@echo "  make publish  upload the built archive to the Viam registry"
	@echo
	@echo "The test suite needs neither the SDK nor a camera - it runs the whole"
	@echo "module against the simulated body in src/binding/fake.py."

setup:
	@./setup.sh
	@$(PIP) install -qq -r requirements-dev.txt
	@echo "ready. 'make test' works now; 'make ext' additionally needs CRSDK_ROOT."

# The Camera Remote SDK is downloaded from Sony after accepting their licence
# and is deliberately not vendored (see README, 'SDK acquisition'). Everything
# except this target works without it.
ext:
ifndef CRSDK_ROOT
	@echo "CRSDK_ROOT is not set."
	@echo
	@echo "Download the Camera Remote SDK from"
	@echo "  https://support.d-imaging.sony.co.jp/app/sdk/en/index.html"
	@echo "(registration + licence acceptance required), extract it, then:"
	@echo
	@echo "    export CRSDK_ROOT=/path/to/CrSDK_vX.YY.ZZ_<platform>"
	@echo "    make ext"
	@echo
	@echo "Until then the module still builds, tests and runs with"
	@echo '`"binding": "fake"` - it just cannot talk to a real camera.'
	@exit 1
else
	$(PIP) install -qq pybind11
	CRSDK_ROOT=$(CRSDK_ROOT) $(PYTHON) native/setup.py build_ext --inplace
	@echo "built src/_crsdk*.so against $(CRSDK_ROOT)"
endif

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m compileall -q src tests native/setup.py

build:
	./build.sh

publish: build
	viam module upload --version=$(VERSION) --platform=$(PLATFORM) dist/archive.tar.gz

clean:
	rm -rf dist build src/_crsdk*.so src/**/__pycache__ src/__pycache__ tests/__pycache__ .pytest_cache
