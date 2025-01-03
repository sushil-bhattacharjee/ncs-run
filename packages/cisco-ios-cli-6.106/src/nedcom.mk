SHELL = /bin/bash
# YANG preprocessor
YPP ?= tools/ypp
JYPP ?= tools/jypp
TURBOY ?= tools/turboy

# build variable file
-include BUILD_VAR

# NED specifics
-include ned-custom.mk

# Include standard NCS examples build definitions and rules
include $(NCS_DIR)/src/ncs/build/include.ncs.mk

NED_TYPE ?= $(if $(wildcard yang/cliparser-extensions-v11.yang),cli,generic)-ned
CUSTOMIZE_SCHEMA ?= customize-schema.schypp

# Check what to build
NETSIM_BUILD                := $(if $(wildcard ../netsim/Makefile),netsim)
NETSIM_CLEAN                := $(if $(NETSIM_BUILD),netsim-clean)
APPLY_YPP_NETSIM            := $(if $(NETSIM_BUILD),$(if $(wildcard ned-yang-filter.mk),tmp-yang/apply_ypp_netsim))
JAVA_COMPILE                := $(if $(wildcard java),javac)
NAMESPACE_CLASSES           := $(if $(filter yes,$(BUILD_NAMESPACE_CLASSES)),tmp-yang/namespace-classes)
COPY_YANG_IN_JAR            ?= $(if $(filter cli-ned,$(NED_TYPE)),yes)
COPY_YANG                   := $(if $(filter yes,$(COPY_YANG_IN_JAR)),copy-yang)
APPLY_YPP_NED               := $(if $(wildcard ned-yang-filter.mk),apply_ypp_ned)
DO_PATCH_YANG_NED           := $(if $(filter yes,$(AUTOPATCH_YANG_NED)),patch-yang-ned)
DO_PATCH_YANG_NETSIM        := $(if $(filter yes,$(AUTOPATCH_YANG_NETSIM)),patch-yang-netsim)
LINK_CONFIG_YANG            := $(if $(YANG_SCOPE_FILTER_DIR), link_scoped_config_yang, link_config_yang)
LINK_EXTRA_DEVIATION        := $(if $(YANG_EXTRA_DEVIATION_FILE),link_extra_deviation)
YANG_SCOPE_FILTER_SRC       := $(if $(YANG_SCOPE_FILTER_DIR), $(shell for f in `ls $(YANG_SCOPE_FILTER_DIR)`; do echo "--read=$(YANG_SCOPE_FILTER_DIR)/$$f "; done))
YANG_SCOPE_FILTER_CMD       := $(if $(YANG_SCOPE_FILTER_DIR), $(TURBOY) --silent --plugin=cdb-data --list-yang-files $(YANG_SCOPE_FILTER_SRC) tmp-yang --yang-path=.)

# ncsc compile args
NCS_SKIP_STATISTICS         := $(if $(filter yes,$(BUILD_CONFIG_MODELS_AS_STATS)),,--ncs-skip-statistics)

# Java section
NS = namespaces
JAVA_PACKAGE = com.tailf.packages.ned.$(PACKAGE_NAME)
JDIR := $(subst .,/,$(JAVA_PACKAGE))
JFLAGS = --java-disable-prefix \
         --exclude-enums \
         --fail-on-warnings \
         --java-package $(JAVA_PACKAGE).$(NS) \
         --emit-java

# Define different sets of yang modules
YANG        := $(sort $(notdir $(wildcard yang/*.yang)))
YANG_STATS  ?= $(sort $(filter tailf-%-stats.yang,$(YANG)))
YANG_OTHER  ?= $(sort $(filter-out tailf-%-dev-meta.yang, $(filter tailf-%-meta.yang tailf-%-oper.yang tailf-%-secrets.yang tailf-%-loginscripts.yang,$(YANG))))
YANG_CONFIG ?= $(sort $(filter-out tailf-%-dev-meta.yang $(YANG_STATS) $(YANG_OTHER),$(YANG)))
YANG_CONFIG_WITHOUT_SUBMODULES := $(sort $(filter-out $(notdir $(shell grep -l belongs-to $(wildcard yang/*.yang))),$(YANG_CONFIG)))
ifneq ($(EXTRA_YANG_STATS_MODULES),)
 YANG_STATS  += $(EXTRA_YANG_STATS_MODULES)
endif
ifneq ($(shell grep -l YANG_DEVIATED ../netsim/Makefile),)
 YPP_OUTDIR  = --out-dir=../netsim/
endif
YANG_ID_LEGACY = $(wildcard *-id.yang)

# ALLOWS FOR CUSTOMIZING THE NED-ID
NED_ID_TOOL := tools/ned-id-tool.py
NED_ID_TOOL_ARG := $(strip $(if $(NED_ID_SUFFIX),-s $(NED_ID_SUFFIX)) $(if $(NED_ID_MAJOR),-M $(NED_ID_MAJOR)) $(if $(NED_ID_MINOR),-m $(NED_ID_MINOR)))

# USE BUNDLED TOOL FOR GENERATING NED-ID
NED_ID_ARG := $(shell [ -x $(NED_ID_TOOL) ] && \
		$(NED_ID_TOOL) -f package-meta-data.xml.in -p $(NED_ID_TOOL_ARG))
YANG_ID_MODULE ?= tailf-ned-id-$(word 2,$(subst :, ,$(NED_ID_ARG)))

# Conversion functions for YANG paths
src_yang_p = $(1:%.yang=yang/%.yang)
tmp_yang_p = $(1:%.yang=../%.yang)
tmp_yang_id_p = $(1:%.yang=../../%.yang)
ncsc_out_p = $(1:%.yang=ncsc-out/modules/fxs/%.fxs)

# Cleaner actions
nedcom_cleaner = rm -rf artefacts \
	         && rm -rf tmp-yang \
	         && rm -f *.fxs

# Printer
SAY = @echo $$'\n'========

# Include YANG filters
-include ned-yang-filter.mk

ifneq ($(VERBOSE),)
 $(warning YANG_CONFIG  = $(YANG_CONFIG))
 $(warning YANG_STATS   = $(YANG_STATS))
 $(warning YANG_OTHER   = $(YANG_OTHER))
endif

all_cli all_gen: all_ned

# Main recipe
all_ned: prepare-yang \
	fxs \
	$(JAVA_COMPILE) \
	$(APPLY_YPP_NETSIM) \
	$(APPLY_YPP_NETSIM_CUSTOM) \
	$(DO_PATCH_YANG_NETSIM) \
	$(NETSIM_BUILD) \
	nedcom-tidy
.PHONY: all_ned

prepare-yang: mkdirs ../package-meta-data.xml \
	$(APPLY_YPP_NED) \
	$(APPLY_YPP_CUSTOM) \
	$(DO_PATCH_YANG_NED) \
	$(COPY_YANG)
.PHONY: prepare-yang

# Same directory structure for all NED types
mkdirs:
	mkdir -p \
	    artefacts \
	    ncsc-out/modules \
	    tmp-yang \
	    ../load-dir \
	    ../private-jar \
	    ../shared-jar \
		java/src/$(JDIR)/$(NS) \
		tmp-yang/yang
	cp yang/*.yang tmp-yang
.PHONY: mkdirs

# Create package meta-data and ID module
../package-meta-data.xml: package-meta-data.xml.in
	$(SAY) CREATE $@ $(if $(YANG_ID_MODULE),and ../load-dir/$(YANG_ID_MODULE).fxs)
	rm -f $@
	@if [ -n "$(NED_ID_ARG)" ]; then \
	     $(NED_ID_TOOL) -f $< $(NED_ID_TOOL_ARG); \
	     echo "NOTE: $(YANG_ID_MODULE) implicitly created"; \
	else \
	    cp $< $@; \
	fi
	chmod +w $@
	@if [ "$(USE_ORDERED_DIFF)" = "yes" ] ; then \
		echo "NOTE: Enabling option 'ordered-diff'" ; \
		$(YPP) --from="<!-- ordered-diff -->" --to "<option><name>ordered-diff</name></option>" ../package-meta-data.xml ; \
	fi

# Copy yang modules to be included in jar
copy-yang: $(YANG_CONFIG:%.yang=tmp-yang/yang/%.yang)
	$(SAY) INCLUDING YANG MODULES IN JAR
	@mkdir -p tmp-yang/yang/dependencies
	YDEPS=`$(TURBOY) --list-deps --ncs-ned-type=$(NED_TYPE) tmp-yang/yang` && \
	for f in $$YDEPS ; do \
	  cp $(NCS_DIR)/src/ncs/yang/$$f tmp-yang/yang/dependencies ; \
	done
.PHONY: copy-yang

# Only patch with deviations for ned
patch-yang-ned: apply-schypp
	$(TURBOY) --patch --silent --yang-path=. tmp-yang
.PHONY: patch-yang-ned

apply-schypp:
	@if [ -f $(CUSTOMIZE_SCHEMA) ] ; then \
		echo "Customizing schema with directives from file $(CUSTOMIZE_SCHEMA)" ; \
		$(TURBOY) --plugin schypp --ignore-missing-paths --pragma-file=$(CUSTOMIZE_SCHEMA) --silent --yang-path=. tmp-yang ; \
	fi
.PHONY: apply-schypp

# Needs both with and without deviations for netsim
patch-yang-netsim:
	$(TURBOY) --patch --silent --yang-path=. ../netsim/
	$(TURBOY) --patch --silent --no-deviations --yang-path=. ../netsim/
.PHONY: patch-yang-netsim

tmp-yang/yang/%.yang: yang/%.yang
	@echo $*.yang >> tmp-yang/yang/yang-modules.txt && ln -s ../$*.yang tmp-yang/yang/

# FXS files
fxs: $(if $(YANG_CONFIG),tmp-yang/fxs-config) \
     $(if $(YANG_STATS),tmp-yang/fxs-stats) \
     $(if $(YANG_OTHER),$(call ncsc_out_p,$(YANG_OTHER)))
.PHONY: fxs

prepare_config_dir:
	$(SAY) CREATE $(call ncsc_out_p,$(YANG_CONFIG))
	@rm -rf tmp-yang/config
	@mkdir -p tmp-yang/config
.PHONY: prepare-fxs-config

link_extra_deviation:
	@(cd tmp-yang/config; ln -s $(YANG_EXTRA_DEVIATION_FILE) .)
.PHONY: link_extra_deviation

link_config_yang:
	$(SAY) "LINKING YANG CONFIG FILES"
	@ln -s $(call tmp_yang_p,$(YANG_CONFIG)) $(call tmp_yang_id_p,$(YANG_ID_LEGACY)) tmp-yang/config
.PHONY: link-config-yang

link_scoped_config_yang:
	$(SAY) "LINKING YANG CONFIG FILES WITHIN SPECIFIED SCOPE"
	@for f in `$(YANG_SCOPE_FILTER_CMD)`; do (cd tmp-yang/config; ln -s ../$$f .); done
	@(cd tmp-yang/config; ln ../tailf-internal-rpcs.yang .)
.PHONY: link_scoped_config_yang

# YANG config, compiled as bundle
tmp-yang/fxs-config: $(call src_yang_p,$(YANG_CONFIG)) prepare_config_dir $(LINK_CONFIG_YANG) $(LINK_EXTRA_DEVIATION) $(LINK_NED_CUSTOM)
	$(NCSC) --ncs-compile-bundle tmp-yang/config \
	     $(NCS_SKIP_STATISTICS) \
	     --ncs-device-dir ncsc-out \
	     --ncs-device-type $(NED_TYPE) \
	     --yangpath tmp-yang/config \
	     --yangpath ncsc-out/modules/yang \
	     $(NED_ID_ARG) \
	     $(EXTRA_NCSC_ARGS)
	cp ncsc-out/modules/fxs/*.fxs ../load-dir
	touch $@

# YANG stats, compiled as bundle
tmp-yang/fxs-stats: $(call src_yang_p,$(YANG_STATS))
	$(SAY) CREATE $(call ncsc_out_p,$(YANG_STATS))
	rm -rf tmp-yang/stats
	mkdir -p tmp-yang/stats
	ln -s $(call tmp_yang_p,$(YANG_STATS)) tmp-yang/stats
	$(NCSC) --ncs-compile-bundle tmp-yang/stats \
	     --ncs-skip-config \
	     --ncs-skip-template \
	     --ncs-device-dir ncsc-out \
	     --ncs-device-type $(NED_TYPE) \
	     --yangpath tmp-yang/stats \
	     --yangpath ncsc-out/modules/yang \
	     $(NED_ID_ARG) \
	     $(EXTRA_NCSC_ARGS)
	cp $(call ncsc_out_p,$(YANG_STATS)) ../load-dir
	touch $@

# Other YANG files, compiled as modules
$(call ncsc_out_p,$(YANG_OTHER)): ncsc-out/modules/fxs/%.fxs: yang/%.yang
	$(SAY) CREATE $@
	@if [ "$(filter %-meta.yang, $<)" != "" -a -f $(subst -meta,-dev-meta, $<) ] ; then \
		$(NCSC) -c tmp-yang/$*.yang -o $@ \
			--deviation $(subst -meta,-dev-meta, $<) \
			--yangpath tmp-yang \
			--yangpath ncsc-out/modules/yang ; \
	else \
		$(NCSC) -c tmp-yang/$*.yang -o $@ \
			--yangpath tmp-yang \
			--yangpath ncsc-out/modules/yang ; \
	fi
	cp $@ ../load-dir

# Java stuff
javac: $(NAMESPACE_CLASSES)
	$(SAY) COMPILE JAVA
	cd java && ant -q -Dpackage.name=$(PACKAGE_NAME) -Dpackage.dir=$(JDIR) all
.PHONY: javac

# Namespace classes
tmp-yang/namespace-classes: $(call src_yang_p,$(YANG_CONFIG_WITHOUT_SUBMODULES) $(YANG_STATS))
	$(SAY) CREATE NAMESPACE CLASSES
	@$(MAKE) $(patsubst %.yang,java/src/$(JDIR)/$(NS)/%.java,$(YANG_CONFIG_WITHOUT_SUBMODULES) $(YANG_STATS))
	touch $@

java/src/$(JDIR)/$(NS)/%.java:
	$(NCSC) $(JFLAGS) $@ ncsc-out/modules/fxs/$*.fxs

# Netsim
netsim:
	$(SAY) MAKE netsim
	$(eval NPROC := $(shell nproc || sysctl -n hw.ncpu || echo 8))
	$(MAKE) -j $$(($(NPROC)>8?7:$(NPROC)-1)) -C ../netsim all
.PHONY: netsim

# Cleanup stuff
clean: nedcom-clean $(NETSIM_CLEAN)
	$(SAY) MAKE CLEAN
	rm -rf ncsc-out/* ../load-dir/*
	rm -f ../package-meta-data.xml
	rm -f ../private-jar/$(PACKAGE_NAME).jar
	rm -f ../shared-jar/$(PACKAGE_NAME)-ns.jar
	rm -f java/src/$(JDIR)/$(NS)/*.java
	$(if $(wildcard java),cd java && ant clean)
.PHONY: clean

netsim-clean:
	$(MAKE) -C ../netsim clean
.PHONY: netsim-clean

nedcom-clean:
	$(nedcom_cleaner)
.PHONY: nedcom-clean

nedcom-tidy: $(NED_CUSTOM_TIDY)
	$(SAY) MAKE TIDY
	@if [ "$(KEEP_FXS)" = "" -a "$$(whoami)" = "jenkins" ] ; then \
	    $(nedcom_cleaner) ; \
	fi
.PHONY: nedcom-tidy

# Add build variables in a file
build_var:
	$(SAY) CREATE BUILD_VAR file
	@rm -f BUILD_VAR
	@echo "BUILD_NAMESPACE_CLASSES ?= $(BUILD_NAMESPACE_CLASSES)" > BUILD_VAR
	@echo "NEDCOM_SECRET_TYPE ?= $(NEDCOM_SECRET_TYPE)" >> BUILD_VAR
	@echo "NED_ID_SUFFIX ?= $(NED_ID_SUFFIX)" >> BUILD_VAR
	@echo "NED_ID_MAJOR ?= $(NED_ID_MAJOR)" >> BUILD_VAR
	@echo "NED_ID_MINOR ?= $(NED_ID_MINOR)" >> BUILD_VAR
.PHONY: build_var
