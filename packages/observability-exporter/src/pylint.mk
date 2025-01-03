# All Python modules
PYTHON_MODULES = $(wildcard ../python/*)
# All Python files
PYTHON_FILES = $(shell find ../python -name "*.py")

# get list of required packages
REQ_PKGS=$(strip $(shell xmlstarlet sel -N pkg="http://tail-f.com/ns/ncs-packages" -t  -m '//pkg:required-package' -v . ../package-meta-data.xml))
REQ_PKGS_PATH=$(addsuffix /python, $(addprefix ../../, $(REQ_PKGS)))

# define the space variable
space :=
space +=

# convert REQ_PKGS_PATH into colon separated list
EXTRA_PYTHON_PATH=$(subst $(space),:,$(REQ_PKGS_PATH))

# Add required packages to PYTHONPATH
export PYTHONPATH :=$(EXTRA_PYTHON_PATH):$(PYTHONPATH)
# This indirect invocation will use packages in an activated virtualenv.
# Directly calling pylint would not.
PYLINT = python3 $(shell which pylint) --ignore=namespaces --rcfile .pylintrc

# run pylint and check return code
#    Pylint should leave with following status code:
#   * 0 if everything went fine
#   * 1 if some fatal message issued
#   * 2 if some error message issued
#   * 4 if some warning message issued
#   * 8 if some refactor message issued
#   * 16 if some convention message issued
#   * 32 on usage error
#   status 1 to 16 will be bit-ORed so you can know which different
#   categories has been issued by analysing pylint output status code
# we use strict linting, so any type of lint will result in a failure.

# Our pylint targets, i.e. the output directories of pylint for our modules (if
# there's more than one)
PYLINT_TARGETS=$(addprefix .pylint.d/,$(addsuffix 1.stats,$(subst ../python/,,$(PYTHON_MODULES))))
# Just a conveniently named target that depends on the pylint output directories
# for all Python modules
pylint: $(PYLINT_TARGETS)

# Depending on all Python files mean we will only run pylint if any of the
# Python files are newer than the pylint report, i.e. anything has actually
# changed. Unfortunately, defining the dependencies per python module is really
# hard, so any change in any module will lead to running pylint on all of them.
PY_MODULE=$(subst .pylint.d/,,$(subst 1.stats,,$@))
.pylint.d/%1.stats: .pylintrc $(PYTHON_FILES)
	if [ "$(SKIP_LINT)" != "true" ]; then PYLINTHOME=.pylint.d $(PYLINT) --ignore=namespaces ../python/$(PY_MODULE); fi

mypy: export MYPYPATH=../python/stubs:$(EXTRA_PYTHON_PATH)
mypy: export MYPY_FORCE_COLOR=1
mypy:
	if [ "$(SKIP_LINT)" != "true" ]; then mypy --check-untyped-defs --ignore-missing-imports --show-error-codes $(PYTHON_MODULES); fi

flake8:
	if [ "$(SKIP_LINT)" != "true" ]; then flake8 --config .flake8 --format=pylint $(PYTHON_MODULES); fi

clean-lint: clean-pylint clean-mypy

clean-pylint:
	rm -fr .pylint.d

clean-mypy:
	rm -fr .mypy_cache
