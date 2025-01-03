#################################################################################
#
# MAKEFILE PLUGIN TO BE USED FOR FILTERING OUT YANG ANNOTATIONS UNSUPPORTED BY
# CERTAIN COMPILERS
#
# NOTE: Original of this file resides in nedcom, don't edit local copy in ned.
#
#################################################################################

NCS_VER := $(shell ($(NCS) --version))
NCS_VER_NUMERIC := $(shell (echo $(firstword $(subst _, ,$(NCS_VER))) \
	           | awk -F. '{ printf("%d%02d%02d%02d\n", $$1%100,$$2%100,$$3%100,$$4%100); }'))
NSO_FEATURES := $(shell cut -d ' ' -f 2 $(NCS_DIR)/support/nso-features.txt)
PROPERTIES_FILE := artefacts/nso-ned-capabilities.properties
SUPPORTS_CDM := YES

ifneq ($(VERBOSE),)
$(warning NCS_VER = $(NCS_VER))
$(warning NCS_VER_NUMERIC = $(NCS_VER_NUMERIC))
endif

$(PROPERTIES_FILE):
	$(SAY) CREATE $@
	mkdir -p artefacts
	rm -f $@
	echo "# Property file auto generated for NSO $(NCS_VER)" > $@
	@echo "nso-version=$(NCS_VER)" >> $@
	@echo "nso-version-numeric=$(NCS_VER_NUMERIC)" >> $@
	$(eval CAPABILITIES := NCS_VER NCS_VER_NUMERIC SUPPORTS_CDM)
.PHONY: $(PROPERTIES_FILE)

generate_capabilities:
	$(SAY) CHECK FOR NSO FEATURES AND CAPABILITIES
	@$(foreach f,$(NSO_FEATURES),\
	  $(eval CAPABILITY := $(shell echo $(f) | tr a-z\- A-Z_)) \
	  $(eval CAPABILITIES := $(CAPABILITIES) SUPPORTS_$(CAPABILITY)) \
	  $(eval SUPPORTS_$(CAPABILITY) := YES) \
	  echo "NSO HAS FEATURE $(f)"; \
	  echo "$(f)=yes" >> $(PROPERTIES_FILE); \
	  echo "supports-$(f)=yes" >> $(PROPERTIES_FILE); \
	)
	@echo "nedcom-secret-type=$(NEDCOM_SECRET_TYPE)" >> $(PROPERTIES_FILE);
.PHONY: generate_capabilities

# Prepare YANG files for NED
NEDCOM_SECRET_TYPE ?= string
tmp-yang/ypp_ned: $(call src_yang_p,$(YANG_CONFIG) $(YANG_STATS) $(YANG_OTHER))
	$(SAY) "RUNNING YANG PRE-PROCESSOR (YPP) WITH THE FOLLOWING VARIABLES:"
	$(YPP) $(foreach c,$(CAPABILITIES), --var $(c)=$($(c))) \
	    $(foreach v,$(YPP_VARS),--var $(v)=$($(v))) \
	    --from=' NEDCOM_SECRET_TYPE' --to=' $(NEDCOM_SECRET_TYPE)' \
	    'tmp-yang/*.yang'
	touch $@

# Prepare YANG files for netsim
tmp-yang/apply_ypp_netsim: $(call src_yang_p,$(YANG_CONFIG) $(YANG_STATS) $(YANG_OTHER))
	$(SAY) PREPARE YANG MODELS FOR NETSIM
	$(YPP) --var NETSIM=YES $(foreach c,$(CAPABILITIES), --var $(c)=$($(c))) \
	    --from='tailf:callpoint\s+[\w\-]+\s*(;|\{[^\}]*\})' --to='' \
	    --from='^(\s+tailf:hidden )' --to='//\1' \
	    --from='//NETSIM' --to='        ' \
	    --from='^.*import cliparser[^\}]+\}' --to='' \
	    --from='^\s+cli:json-arguments .+?;' --to='' \
	    --from='^\s+cli:[a-z0-9\-]+(([ \t]+((\"[^\"]+\")|([^\"\{]\S+))\s*)?((;)|(\s*\{[^\}]+\}))|(.*;))' --to='' \
	    $(YPP_OUTDIR) \
	    'tmp-yang/*.yang'
	touch $@

apply_ypp_ned: \
	$(PROPERTIES_FILE) \
	generate_capabilities \
	tmp-yang/ypp_ned
.PHONY: apply_ypp_ned
